"""Слой работы с SQLite: соединение, транзакции и репозитории.

Доступ к данным — ровно двумя путями:

    rows = await db.fetchall("SELECT ...", params)      # чтение, без блокировки

    async with db.write() as tx:                        # BEGIN IMMEDIATE
        order = await db.create_order(tx, ...)
        await db.audit(tx, ...)
        await db.enqueue_record(tx, ...)
    # COMMIT здесь; любое исключение — ROLLBACK, ничего частичного

Все изменяющие методы обязаны получить `tx` и никогда не коммитят сами.
Записать что-либо мимо транзакции невозможно: `tx` больше взять неоткуда,
а без него метод не вызывается. Отсюда следуют три свойства, ради которых
всё это сделано: заказ, его запись в журнал действий и его уведомления
попадают в базу вместе или никак; место в лесенке считается под тем же
write-локом, что и вставка заказа; повторная отправка формы не создаёт
второй заказ (UNIQUE по client_token).

Схема:
  users        — владельцы и администраторы клуба (логин = handle, пароль
                 хэширован). Логин закреплён только за действующим
                 пользователем, после приостановки или удаления
                 высвобождается; удаление мягкое (deleted_at).
  user_chats   — чаты кабинета: у пользователя может быть несколько
                 устройств. PRIMARY KEY по chat_id — «один чат, один
                 пользователь», на этом держится авторизация.
  orders       — заказы VR-сеансов; цена и все начисления заморожены в
                 момент оформления. client_token делает оформление
                 идемпотентным. Выбранная акция принадлежит заказу
                 (promo_id + снимок promo_name).
  promos       — акции: призы колеса фортуны, у которых есть только имя.
                 Мягкое удаление archived_at; name_folded + частичный
                 UNIQUE обеспечивают уникальность имени среди действующих
                 с учётом кириллицы. Денежной величины у акции нет —
                 в деньгах заказа она не участвует ничем.
  bonuses      — бонусы и удержания администраторам.
  payouts      — выплаты; получатель — владелец или администратор.
  settings     — числовые настройки; отсутствующий ключ = значение
                 из SETTINGS_DEFAULTS.
  super_admins — рабочий список супер-админов; ADMIN_IDS из .env остаётся
                 механизмом восстановления доступа и не удаляется из бота.
  audit_log    — журнал всех денежных и административных изменений,
                 только на добавление, пишется в той же транзакции.
  outbox       — очередь долговечной доставки Записей.
  chat_windows — единственное интерактивное сообщение чата вместе с его
                 содержимым: этого достаточно, чтобы переставить окно под
                 новую Запись без участия хендлера.
  fsm_state    — состояние сценариев aiogram, переживающее перезапуск.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiosqlite

from migrations import apply_migrations
from pricing import KIND_FREE15, ladder_amount, split
from utils import utcnow_iso

log = logging.getLogger(__name__)

# Доля по умолчанию для новых владельцев; вознаграждение администратора
# считается лесенкой серии, поэтому его percent всегда 0 (историческое поле).
DEFAULT_PERCENTS: dict[str, float] = {"owner": 30, "admin": 0}

SETTINGS_DEFAULTS: dict[str, float] = {
    "price_1_15": 200,
    "price_1_30": 300,
    "price_1_60": 500,
    "price_2_15": 350,
    "price_2_30": 500,
    "price_2_60": 800,
    "discount_enabled": 1,
    "discount_percent": 20,
    "discount_days": 0b0011111,       # маска дней недели: Пн–Пт
    "discount_start_min": 10 * 60,    # начало скидки — 10:00
    "discount_end_min": 16 * 60,      # конец скидки — 16:00
    "free15_enabled": 1,              # доступны ли бесплатные 15 минут
}

# --------------------------------------------- Остаток VR Heaven: одно место
#
# Итог сводки и её построчная расшифровка обязаны считаться одинаково,
# иначе столбец «Остаток VR Heaven» и подвал под ним расходятся. Оба
# запроса собираются из этих двух кусков и другого источника не имеют.

# Остаток VR Heaven по одному заказу: цена за вычетом тех долей, которые
# получателям достанутся. Доля остаётся у VR Heaven ровно в одном случае —
# она не выплачена, а получателя больше нет (SPEC §4).
VR_SHARE_EXPR = (
    "o.price"
    " - CASE WHEN o.admin_payout_id IS NOT NULL OR a.deleted_at IS NULL"
    "        THEN o.admin_share ELSE 0 END"
    " - CASE WHEN o.owner_id IS NULL THEN 0"
    "        WHEN o.owner_payout_id IS NOT NULL OR w.deleted_at IS NULL"
    "        THEN o.owner_share ELSE 0 END"
)

# Заказы текущего периода сводки: неотменённые, у которых открыта хотя бы
# одна доля. Ровно те, что видны в таблицах сводки.
VR_OPEN_ORDERS_FROM = (
    "FROM orders o"
    " JOIN users a ON a.id = o.admin_id"
    " LEFT JOIN users w ON w.id = o.owner_id"
    " WHERE o.cancelled_at IS NULL"
    " AND (o.admin_payout_id IS NULL"
    "  OR (o.owner_id IS NOT NULL AND o.owner_payout_id IS NULL))"
)


@dataclass(frozen=True)
class Actor:
    """Кто выполнил действие — для журнала действий."""
    kind: str                     # superadmin | staff | system
    tg_id: int | None = None
    user_id: int | None = None

    @staticmethod
    def system() -> "Actor":
        return Actor(kind="system")

    @staticmethod
    def superadmin(tg_id: int) -> "Actor":
        return Actor(kind="superadmin", tg_id=tg_id)

    @staticmethod
    def staff(user_row, tg_id: int | None = None) -> "Actor":
        return Actor(kind="staff", tg_id=tg_id, user_id=user_row["id"])


@dataclass(frozen=True)
class Cancellation:
    """Чем кончилась отмена заказа.

    В логическом значении — «отмена состоялась», поэтому `if cancelled:`
    читается ровно как прежде. Долг здесь для того, чтобы о нём сказали
    вслух: сумма, которую администратор уже получил за отменённый заказ,
    записана удержанием, и уведомления обязаны её назвать.
    """
    done: bool
    debt: float = 0.0             # выплаченное вознаграждение, ставшее удержанием
    bonus_id: int | None = None

    def __bool__(self) -> bool:
        return self.done


class Tx:
    """Открытая транзакция. Единственный способ что-либо записать."""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def execute(self, sql: str, params=()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def fetchone(self, sql: str, params=()):
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params=()):
        cur = await self.conn.execute(sql, params)
        return await cur.fetchall()


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._lock_owner = None

    # ------------------------------------------------------- Соединение

    async def init(self, *, before_migration=None) -> None:
        """Открывает базу и доводит её до актуальной версии схемы.

        before_migration(pending) вызывается один раз перед применением
        миграций — сюда main.py вешает неизменяемый снимок базы.
        """
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.execute("PRAGMA busy_timeout = 5000")
        await self.conn.execute("PRAGMA synchronous = FULL")
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await apply_migrations(self.conn, before_migration=before_migration)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def schema_version(self) -> int:
        row = await self.fetchone("PRAGMA user_version")
        return row[0]

    # ------------------------------------------------------- Чтение/запись

    async def fetchone(self, sql: str, params=()):
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params=()):
        cur = await self.conn.execute(sql, params)
        return await cur.fetchall()

    @asynccontextmanager
    async def write(self):
        """BEGIN IMMEDIATE ... COMMIT/ROLLBACK под общим write-локом.

        Лок держится на всю транзакцию, а не на отдельные операторы:
        именно поэтому чтение и следующая за ним запись атомарны.
        Вложенность запрещена — BEGIN внутри BEGIN в SQLite молча
        не делает ничего и создал бы ложное чувство атомарности.
        """
        task = asyncio.current_task()
        if self._lock_owner is not None and self._lock_owner is task:
            raise RuntimeError("Вложенная транзакция запрещена")
        await self._write_lock.acquire()
        self._lock_owner = task
        try:
            await self.conn.execute("BEGIN IMMEDIATE")
            tx = Tx(self.conn)
            try:
                yield tx
            except BaseException:
                try:
                    await self.conn.execute("ROLLBACK")
                except Exception:            # соединение уже без транзакции
                    log.exception("ROLLBACK не выполнен")
                raise
            await self.conn.execute("COMMIT")
        finally:
            self._lock_owner = None
            self._write_lock.release()

    # -------------------------------------------------- Журнал действий

    async def audit(self, tx: Tx, actor: Actor, action: str, entity: str,
                    entity_id: int | None = None, *,
                    before: dict | None = None, after: dict | None = None,
                    request_id: str | None = None) -> int:
        cur = await tx.execute(
            "INSERT INTO audit_log (at, actor_kind, actor_tg_id, actor_user_id,"
            " action, entity, entity_id, before_json, after_json, request_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (utcnow_iso(), actor.kind, actor.tg_id, actor.user_id, action,
             entity, entity_id,
             json.dumps(before, ensure_ascii=False) if before is not None else None,
             json.dumps(after, ensure_ascii=False) if after is not None else None,
             request_id),
        )
        return cur.lastrowid

    async def audit_for_entity(self, entity: str, entity_id: int,
                               limit: int = 5) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM audit_log WHERE entity = ? AND entity_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (entity, entity_id, limit),
        )

    async def audit_since(self, since_iso: str) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM audit_log WHERE at >= ? ORDER BY id", (since_iso,)
        )

    # ---------------------------------------------------- Очередь Записей

    async def enqueue_record(self, tx: Tx, *, chat_id: int, kind: str,
                             text: str, dedup_key: str | None = None,
                             document: dict | None = None) -> int | None:
        """Ставит Запись в очередь доставки внутри бизнес-транзакции.

        Запись и изменение, которое её породило, коммитятся вместе:
        откатился заказ — уведомлений нет; записался — уведомления
        гарантированно будут доставлены (с повторами через рестарты).
        Возвращает None, если такая Запись уже стоит в очереди.

        Текст Записи хранится уже собранным, поэтому нативных таблиц
        здесь не бывает: пересобрать их в запасной моноширинный вид при
        отказе Telegram было бы нечем, а отправить `<table>` обычным
        сообщением нельзя — разметку такого тега Telegram не понимает.
        Отчёты кладутся в очередь моноширинными (`to_html(rich=False)`).
        """
        payload = json.dumps({"text": text, "document": document},
                             ensure_ascii=False)
        cur = await tx.execute(
            "INSERT INTO outbox (chat_id, kind, payload, dedup_key, status,"
            " attempts, next_attempt_at, created_at)"
            " VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)"
            " ON CONFLICT(dedup_key) DO NOTHING",
            (chat_id, kind, payload, dedup_key, utcnow_iso(), utcnow_iso()),
        )
        return cur.lastrowid if cur.rowcount else None

    async def due_records(self, now_iso: str, limit: int = 25) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM outbox WHERE status = 'pending' AND next_attempt_at <= ?"
            " ORDER BY id LIMIT ?",
            (now_iso, limit),
        )

    async def mark_record_sent(self, tx: Tx, record_id: int, message_id: int) -> None:
        await tx.execute(
            "UPDATE outbox SET status = 'sent', sent_message_id = ?,"
            " attempts = attempts + 1, last_error = NULL WHERE id = ?",
            (message_id, record_id),
        )

    async def mark_record_retry(self, tx: Tx, record_id: int,
                                next_attempt_at: str, error: str) -> None:
        await tx.execute(
            "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?,"
            " last_error = ? WHERE id = ?",
            (next_attempt_at, error[:500], record_id),
        )

    async def mark_record_final(self, tx: Tx, record_id: int, status: str,
                                error: str) -> None:
        await tx.execute(
            "UPDATE outbox SET status = ?, attempts = attempts + 1,"
            " last_error = ? WHERE id = ?",
            (status, error[:500], record_id),
        )

    async def outbox_stats(self) -> aiosqlite.Row:
        return await self.fetchone(
            "SELECT"
            " COALESCE(SUM(status = 'pending'), 0) AS pending,"
            " COALESCE(SUM(status = 'failed'), 0) AS failed,"
            " COALESCE(SUM(status = 'dropped'), 0) AS dropped,"
            " COALESCE(SUM(status = 'sent'), 0) AS sent"
            " FROM outbox"
        )

    async def purge_sent_records(self, tx: Tx, before_iso: str) -> int:
        """Доставленные Записи в очереди больше не нужны — сами сообщения
        в чатах остаются навсегда, здесь чистится только журнал доставки."""
        cur = await tx.execute(
            "DELETE FROM outbox WHERE status = 'sent' AND created_at < ?",
            (before_iso,),
        )
        return cur.rowcount

    # -------------------------------------------------------- Пользователи

    async def create_user(
        self, tx: Tx, role: str, handle: str, name: str, contact: str,
        password_hash: str, percent: float | None = None,
        owner_id: int | None = None,
    ) -> int:
        if percent is None:
            percent = DEFAULT_PERCENTS[role]
        cur = await tx.execute(
            "INSERT INTO users (role, handle, name, contact, password_hash,"
            " percent, owner_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (role, handle, name, contact, password_hash, percent, owner_id,
             utcnow_iso()),
        )
        return cur.lastrowid

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        """Пользователь по id, включая удалённых (нужно для истории)."""
        return await self.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))

    async def get_user_by_handle(self, handle: str) -> aiosqlite.Row | None:
        """Действующий пользователь по логину — для входа в кабинет."""
        return await self.fetchone(
            "SELECT * FROM users WHERE handle = ?"
            " AND is_active = 1 AND deleted_at IS NULL",
            (handle,),
        )

    async def get_suspended_by_handle(self, handle: str) -> aiosqlite.Row | None:
        """Приостановленная запись с таким логином — чтобы отличить
        приостановку от опечатки в логине."""
        return await self.fetchone(
            "SELECT * FROM users WHERE handle = ?"
            " AND is_active = 0 AND deleted_at IS NULL ORDER BY id DESC",
            (handle,),
        )

    async def handle_available(self, handle: str, exclude_id: int | None = None) -> bool:
        """Свободен ли логин: занят только действующим пользователем."""
        sql = ("SELECT 1 FROM users WHERE handle = ?"
               " AND is_active = 1 AND deleted_at IS NULL")
        params: list = [handle]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        return await self.fetchone(sql, params) is None

    async def get_user_by_chat(self, chat_id: int) -> aiosqlite.Row | None:
        """Пользователь по привязанному чату; удалённые теряют доступ."""
        return await self.fetchone(
            "SELECT u.* FROM users u"
            " JOIN user_chats c ON c.user_id = u.id"
            " WHERE c.chat_id = ? AND u.deleted_at IS NULL",
            (chat_id,),
        )

    async def list_users(self, role: str, active_only: bool = False) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM users WHERE role = ? AND deleted_at IS NULL"
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY handle"
        return await self.fetchall(sql, (role,))

    async def admins_of_owner(self, owner_id: int) -> list[aiosqlite.Row]:
        """Не удалённые администраторы, прикреплённые к владельцу."""
        return await self.fetchall(
            "SELECT * FROM users WHERE role = 'admin' AND owner_id = ?"
            " AND deleted_at IS NULL ORDER BY handle",
            (owner_id,),
        )

    async def set_user_active(self, tx: Tx, user_id: int, is_active: bool) -> None:
        await tx.execute("UPDATE users SET is_active = ? WHERE id = ?",
                         (int(is_active), user_id))

    async def set_user_password(self, tx: Tx, user_id: int, password_hash: str) -> None:
        await tx.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (password_hash, user_id))

    async def set_user_percent(self, tx: Tx, user_id: int, percent: float) -> None:
        await tx.execute("UPDATE users SET percent = ? WHERE id = ?",
                         (percent, user_id))

    async def set_user_series_reset(self, tx: Tx, user_id: int,
                                    reset_min: int | None) -> None:
        """Индивидуальное время сбросов серии; None — вернуть умолчание."""
        await tx.execute("UPDATE users SET series_reset_min = ? WHERE id = ?",
                         (reset_min, user_id))

    async def set_admin_owner(self, tx: Tx, admin_id: int,
                              owner_id: int | None) -> None:
        """Перекрепляет администратора к владельцу; действует на новые заказы."""
        await tx.execute("UPDATE users SET owner_id = ? WHERE id = ?",
                         (owner_id, admin_id))

    async def delete_user(self, tx: Tx, user_id: int) -> None:
        """Мягкое удаление: логин высвобождается, все чаты кабинета
        отвязываются — доступ закрывается на каждом устройстве."""
        await tx.execute(
            "UPDATE users SET deleted_at = ?, is_active = 0 WHERE id = ?",
            (utcnow_iso(), user_id),
        )
        await tx.execute("DELETE FROM user_chats WHERE user_id = ?", (user_id,))

    async def bind_chat(self, tx: Tx, user_id: int, chat_id: int) -> None:
        """Привязывает чат к пользователю. Один чат — один пользователь
        (повторный вход переносит только этот чат); чатов может быть много."""
        await tx.execute(
            "INSERT INTO user_chats (chat_id, user_id, created_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET user_id = excluded.user_id,"
            " created_at = excluded.created_at",
            (chat_id, user_id, utcnow_iso()),
        )

    async def unbind_chat(self, tx: Tx, chat_id: int) -> int:
        """Отвязывает один чат — например, когда пользователь заблокировал
        бота и Telegram отвечает «чат недоступен»."""
        cur = await tx.execute("DELETE FROM user_chats WHERE chat_id = ?", (chat_id,))
        return cur.rowcount

    async def chats_for_user(self, user_id: int) -> list[int]:
        """Чаты кабинета пользователя — получатели Записей."""
        rows = await self.fetchall(
            "SELECT chat_id FROM user_chats WHERE user_id = ?"
            " ORDER BY created_at, chat_id",
            (user_id,),
        )
        return [row["chat_id"] for row in rows]

    async def unbind_user_chats(self, tx: Tx, user_id: int) -> None:
        """Отвязывает все чаты пользователя: устройства теряют кабинет
        до повторного входа (используется при смене пароля)."""
        await tx.execute("DELETE FROM user_chats WHERE user_id = ?", (user_id,))

    # ------------------------------------------------------- Супер-админы

    async def list_super_admins(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM super_admins WHERE removed_at IS NULL ORDER BY added_at, tg_id"
        )

    async def add_super_admin(self, tx: Tx, tg_id: int, label: str,
                              added_by: int | None) -> None:
        await tx.execute(
            "INSERT INTO super_admins (tg_id, label, added_by, added_at, removed_at)"
            " VALUES (?, ?, ?, ?, NULL)"
            " ON CONFLICT(tg_id) DO UPDATE SET removed_at = NULL,"
            " label = excluded.label, added_by = excluded.added_by,"
            " added_at = excluded.added_at",
            (tg_id, label, added_by, utcnow_iso()),
        )

    async def remove_super_admin(self, tx: Tx, tg_id: int) -> bool:
        cur = await tx.execute(
            "UPDATE super_admins SET removed_at = ?"
            " WHERE tg_id = ? AND removed_at IS NULL",
            (utcnow_iso(), tg_id),
        )
        return cur.rowcount > 0

    # -------------------------------------------------------------- Заказы

    async def create_order(
        self, tx: Tx, *, client_token: str, admin_id: int, admin_percent: float,
        owner_id: int | None, owner_percent: float, owner_suspended: bool,
        kind: str, headsets: int | None, minutes: int | None,
        promo_id: int | None, promo_name: str | None,
        base_price: float, discount_percent: float, price: float,
        quoted_at: str, series_since_iso: str,
    ) -> tuple[aiosqlite.Row, bool]:
        """Записывает заказ идемпотентно; возвращает (заказ, создан ли он).

        Место в лесенке считается здесь же, под write-локом транзакции:
        два устройства одной учётной записи получают ступени 1 и 2, а не
        1 и 1. Повторная отправка того же client_token (двойное нажатие,
        повтор callback, восстановление сценария после перезапуска) не
        создаёт второй заказ — возвращается уже записанный.

        Граница серии (`series_since`) записывается в заказ вместе с местом
        в ней: место без границы нельзя проверить, а выводить границу из
        текущей настройки нельзя — смена времени сбросов перекроила бы
        прошлые серии задним числом.

        Бесплатные 15 минут места в серии не занимают: вознаграждения за
        такой заказ нет, а занятая ступень подняла бы вознаграждение за
        следующий заказ. Поэтому у него нет ни места, ни границы серии, и
        в счёт мест он не входит — ни своим, ни чужим.
        """
        row = await tx.fetchone(
            "SELECT * FROM orders WHERE client_token = ?", (client_token,)
        )
        if row is not None:
            return await self.get_order(order_id=row["id"], tx=tx), False

        if kind == KIND_FREE15:
            series_pos, series_since, admin_amount = None, None, 0.0
        else:
            counted = await tx.fetchone(
                "SELECT COUNT(*) AS n FROM orders WHERE admin_id = ?"
                " AND cancelled_at IS NULL AND kind <> ? AND created_at >= ?",
                (admin_id, KIND_FREE15, series_since_iso),
            )
            series_pos = counted["n"] + 1
            series_since = series_since_iso
            admin_amount = ladder_amount(series_pos)
        admin_share, owner_share, _ = split(
            price, admin_amount, 0 if owner_suspended else owner_percent
        )
        cur = await tx.execute(
            "INSERT INTO orders (client_token, admin_id, owner_id, kind, headsets,"
            " minutes, promo_id, promo_name, base_price, discount_percent, price,"
            " admin_percent, admin_share, series_pos, series_since, owner_percent,"
            " owner_share, owner_suspended, quoted_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            # предикат обязателен: индекс частичный (NULL-токены исторических
            # заказов в него не входят)
            " ON CONFLICT(client_token) WHERE client_token IS NOT NULL DO NOTHING",
            (client_token, admin_id, owner_id, kind, headsets, minutes,
             promo_id, promo_name, base_price, discount_percent, price,
             admin_percent, admin_share, series_pos, series_since,
             0 if owner_suspended else owner_percent, owner_share,
             int(owner_suspended), quoted_at, utcnow_iso()),
        )
        if not cur.rowcount:                       # гонка: токен уже записан
            row = await tx.fetchone(
                "SELECT id FROM orders WHERE client_token = ?", (client_token,)
            )
            return await self.get_order(order_id=row["id"], tx=tx), False
        return await self.get_order(order_id=cur.lastrowid, tx=tx), True

    _ORDER_SELECT = (
        "SELECT o.*, a.handle AS admin_handle, a.name AS admin_name,"
        " a.contact AS admin_contact, w.handle AS owner_handle"
        " FROM orders o"
        " JOIN users a ON a.id = o.admin_id"
        " LEFT JOIN users w ON w.id = o.owner_id"
    )

    async def get_order(self, order_id: int, tx: Tx | None = None) -> aiosqlite.Row | None:
        source = tx or self
        return await source.fetchone(
            f"{self._ORDER_SELECT} WHERE o.id = ?", (order_id,)
        )

    async def cancel_order(self, tx: Tx, order_id: int, *,
                           for_self: bool) -> "Cancellation":
        """Мягкая отмена: заказ исключается из расчётов, но остаётся в истории.

        Условный UPDATE: двойное нажатие и параллельная отмена дают один
        эффект. Самоотмена дополнительно проверяет выплаты в самом UPDATE —
        выплата, пришедшая в тот же миг, не может проскочить мимо запрета.
        VR Heaven отменяет и выплаченный заказ: это надзорное действие.

        Выплаченное вознаграждение отменой не возвращается само: деньги у
        администратора на руках, а заказ уходит из всех расчётов. Прежде
        эта сумма просто исчезала из учёта. Теперь она становится
        удержанием — тем самым отрицательным бонусом, которым VR Heaven
        и так правит расчёты (SPEC §3а): попадает в текущий период
        администратора, вычитается из ближайшей выплаты и, пока не
        выплачена, отменяется обычной кнопкой. Проведённая выплата при
        этом не переписывается (SPEC §5).

        Состояние заказа перечитывается **после** условного UPDATE и в
        той же транзакции: выплата, прошедшая между чтением экрана и
        нажатием, попадёт в долг, а не мимо него.
        """
        sql = ("UPDATE orders SET cancelled_at = ?"
               " WHERE id = ? AND cancelled_at IS NULL")
        if for_self:
            sql += " AND admin_payout_id IS NULL AND owner_payout_id IS NULL"
        cur = await tx.execute(sql, (utcnow_iso(), order_id))
        if not cur.rowcount:
            return Cancellation(done=False)
        order = await tx.fetchone(
            "SELECT admin_id, admin_share, admin_payout_id FROM orders"
            " WHERE id = ?", (order_id,))
        debt = round(order["admin_share"], 2)
        if order["admin_payout_id"] is None or not debt:
            return Cancellation(done=True)
        bonus_id = await self.create_bonus(
            tx, order["admin_id"], -debt,
            f"Возврат вознаграждения за отменённый заказ №{order_id}")
        return Cancellation(done=True, debt=debt, bonus_id=bonus_id)

    async def count_series_orders(self, admin_id: int, since_iso: str) -> int:
        """Неотменённые заказы администратора с начала его текущей серии.

        Считается ровно то же, что считает `create_order`, выдавая место
        в лесенке: бесплатные 15 минут в серию не входят.
        """
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM orders WHERE admin_id = ?"
            " AND cancelled_at IS NULL AND kind <> ? AND created_at >= ?",
            (admin_id, KIND_FREE15, since_iso),
        )
        return row["n"]

    async def last_orders(self, limit: int = 8,
                          before_id: int | None = None) -> list[aiosqlite.Row]:
        """Страница неотменённых заказов для экрана отмены VR Heaven."""
        sql = f"{self._ORDER_SELECT} WHERE o.cancelled_at IS NULL"
        params: list = []
        if before_id:
            sql += " AND o.id < ?"
            params.append(before_id)
        sql += " ORDER BY o.id DESC LIMIT ?"
        params.append(limit)
        return await self.fetchall(sql, params)

    async def admin_recent_orders(self, admin_id: int, since_iso: str,
                                  limit: int = 20) -> list[aiosqlite.Row]:
        """Неотменённые заказы администратора с момента since — экран
        отмены собственного заказа (окно отмены задаётся вызывающим)."""
        return await self.fetchall(
            "SELECT * FROM orders WHERE admin_id = ? AND cancelled_at IS NULL"
            " AND created_at >= ? ORDER BY id DESC LIMIT ?",
            (admin_id, since_iso, limit),
        )

    async def admin_unpaid_orders(self, admin_id: int) -> list[aiosqlite.Row]:
        """Текущий период администратора: заказы с невыплаченной его долей."""
        return await self.fetchall(
            "SELECT * FROM orders WHERE admin_id = ?"
            " AND admin_payout_id IS NULL AND cancelled_at IS NULL ORDER BY id",
            (admin_id,),
        )

    async def admin_unpaid_total(self, admin_id: int) -> aiosqlite.Row:
        """Текущий период администратора: заказы, оборот, доля по заказам,
        невыплаченные бонусы и итог к выплате (доля + бонусы).

        Строки считаются, а не только суммируются: период из настоящих
        строк обязан закрываться и тогда, когда его итог — ноль."""
        return await self.fetchone(
            "SELECT COUNT(*) AS orders_count,"
            " COALESCE(SUM(price), 0) AS turnover,"
            " COALESCE(SUM(admin_share), 0) AS share_sum,"
            " (SELECT COALESCE(SUM(amount), 0) FROM bonuses"
            "   WHERE admin_id = ? AND payout_id IS NULL AND cancelled_at IS NULL)"
            "   AS bonus_sum,"
            " (SELECT COUNT(*) FROM bonuses"
            "   WHERE admin_id = ? AND payout_id IS NULL AND cancelled_at IS NULL)"
            "   AS bonus_count,"
            " COALESCE(SUM(admin_share), 0)"
            " + (SELECT COALESCE(SUM(amount), 0) FROM bonuses"
            "     WHERE admin_id = ? AND payout_id IS NULL AND cancelled_at IS NULL)"
            "   AS due_sum"
            " FROM orders WHERE admin_id = ?"
            " AND admin_payout_id IS NULL AND cancelled_at IS NULL",
            (admin_id, admin_id, admin_id, admin_id),
        )

    async def owner_unpaid_orders(self, owner_id: int) -> list[aiosqlite.Row]:
        """Текущий период владельца: заказы его админов с невыплаченной долей."""
        return await self.fetchall(
            "SELECT o.*, a.handle AS admin_handle FROM orders o"
            " JOIN users a ON a.id = o.admin_id"
            " WHERE o.owner_id = ?"
            " AND o.owner_payout_id IS NULL AND o.cancelled_at IS NULL ORDER BY o.id",
            (owner_id,),
        )

    async def owner_unpaid_total(self, owner_id: int) -> aiosqlite.Row:
        return await self.fetchone(
            "SELECT COUNT(*) AS orders_count,"
            " COALESCE(SUM(price), 0) AS turnover,"
            " COALESCE(SUM(owner_share), 0) AS share_sum,"
            " 0 AS bonus_sum,"
            " 0 AS bonus_count,"            # бонусы бывают только у администратора
            " COALESCE(SUM(owner_share), 0) AS due_sum"
            " FROM orders WHERE owner_id = ?"
            " AND owner_payout_id IS NULL AND cancelled_at IS NULL",
            (owner_id,),
        )

    @staticmethod
    def is_payable(total) -> bool:
        """Есть ли что закрывать выплатой по агрегату текущего периода.

        Судим по открытым строкам, а не по сумме. Нулевой итог
        выплате не подлежит (SPEC §4), но ноль бывает разного
        происхождения. Не начислено ничего — строк нет, и платить нечего.
        Начислено и ровно столько же удержано — строки настоящие. Заказ,
        оформленный при приостановленном владельце, несёт долю 0 и тоже
        настоящий: владелец обязан его закрыть, иначе заказ остаётся
        открытым навсегда, тянется в каждый следующий период и вечно
        числится в остатке VR Heaven.

        Отрицательный итог не выплачивается никогда: удержание, которое
        больше начисленного, переносится в следующий период.
        """
        if round(total["due_sum"], 2) < 0:
            return False
        return bool(total["orders_count"] or total["bonus_count"])

    async def unpaid_total(self, user: aiosqlite.Row) -> aiosqlite.Row:
        """Текущий период получателя — агрегат по его невыплаченной доле
        (у администратора итог due_sum включает невыплаченные бонусы)."""
        if user["role"] == "admin":
            return await self.admin_unpaid_total(user["id"])
        return await self.owner_unpaid_total(user["id"])

    async def owners_unpaid_summary(self) -> list[aiosqlite.Row]:
        """Сводка текущего периода по каждому не удалённому владельцу."""
        return await self.fetchall(
            "SELECT u.id, u.handle, u.name, u.is_active,"
            " COUNT(o.id) AS orders_count,"
            " COALESCE(SUM(o.price), 0) AS turnover,"
            " COALESCE(SUM(o.owner_share), 0) AS share_sum,"
            " 0 AS bonus_sum,"
            " 0 AS bonus_count,"
            " COALESCE(SUM(o.owner_share), 0) AS due_sum"
            " FROM users u"
            " LEFT JOIN orders o ON o.owner_id = u.id"
            "   AND o.owner_payout_id IS NULL AND o.cancelled_at IS NULL"
            " WHERE u.role = 'owner' AND u.deleted_at IS NULL"
            " GROUP BY u.id ORDER BY u.handle"
        )

    async def admins_unpaid_summary(self) -> list[aiosqlite.Row]:
        """Сводка текущего периода по каждому не удалённому администратору;
        due_sum — доля по заказам вместе с невыплаченными бонусами."""
        return await self.fetchall(
            "SELECT u.id, u.handle, u.name, u.is_active,"
            " COUNT(o.id) AS orders_count,"
            " COALESCE(SUM(o.price), 0) AS turnover,"
            " COALESCE(SUM(o.admin_share), 0) AS share_sum,"
            " (SELECT COALESCE(SUM(b.amount), 0) FROM bonuses b"
            "   WHERE b.admin_id = u.id AND b.payout_id IS NULL"
            "   AND b.cancelled_at IS NULL) AS bonus_sum,"
            " (SELECT COUNT(*) FROM bonuses b"
            "   WHERE b.admin_id = u.id AND b.payout_id IS NULL"
            "   AND b.cancelled_at IS NULL) AS bonus_count,"
            " COALESCE(SUM(o.admin_share), 0)"
            " + (SELECT COALESCE(SUM(b.amount), 0) FROM bonuses b"
            "     WHERE b.admin_id = u.id AND b.payout_id IS NULL"
            "     AND b.cancelled_at IS NULL) AS due_sum"
            " FROM users u"
            " LEFT JOIN orders o ON o.admin_id = u.id"
            "   AND o.admin_payout_id IS NULL AND o.cancelled_at IS NULL"
            " WHERE u.role = 'admin' AND u.deleted_at IS NULL"
            " GROUP BY u.id ORDER BY u.handle"
        )

    async def vrheaven_unpaid_total(self) -> aiosqlite.Row:
        """Остаток VR Heaven по заказам текущего периода сводки.

        Заказ открыт, пока не выплачена доля хотя бы одного получателя —
        ровно те заказы, что видны в таблицах сводки. Удаление получателя
        заказ не закрывает: его доля никуда не денется, просто достанется
        VR Heaven, и она обязана остаться видимой (SPEC §4).

        Доля уходит из остатка, если она **уже выплачена** или **будет
        выплачена** (получатель не удалён). У VR Heaven она остаётся ровно
        в одном случае: доля не выплачена, а получателя больше нет.
        Условие по `deleted_at` без оглядки на выплату приписывало
        VR Heaven деньги, которые он уже отдал.
        """
        return await self.fetchone(
            "SELECT COUNT(*) AS orders_count,"
            " COALESCE(SUM(o.price), 0) AS turnover,"
            f" COALESCE(SUM({VR_SHARE_EXPR}), 0) AS share_sum"
            f" {VR_OPEN_ORDERS_FROM}"
        )

    async def vrheaven_unpaid_orders(self) -> list[aiosqlite.Row]:
        """Те же заказы построчно: цена и остаток VR Heaven по каждому.

        Строки берутся тем же выражением и тем же условием, что и итог
        (`VR_SHARE_EXPR`, `VR_OPEN_ORDERS_FROM`), — иначе столбец
        «Остаток VR Heaven» в таблице и «Остаток VR Heaven» в подвале
        разошлись бы при первой же правке одного из двух запросов.
        Сумма столбца равна `share_sum` итога до вычета бонусов: бонус
        не принадлежит ни одному заказу.
        """
        return await self.fetchall(
            "SELECT o.id, o.price, a.handle AS admin_handle,"
            f" {VR_SHARE_EXPR} AS vr_share"
            f" {VR_OPEN_ORDERS_FROM}"
            " ORDER BY o.id"
        )

    async def owner_admins_summary(self, owner_id: int) -> list[aiosqlite.Row]:
        """Администраторы владельца с агрегатами его текущего периода."""
        return await self.fetchall(
            "SELECT u.id, u.handle, u.name, u.is_active,"
            " COUNT(o.id) AS orders_count,"
            " COALESCE(SUM(o.price), 0) AS turnover,"
            " COALESCE(SUM(o.owner_share), 0) AS share_sum"
            " FROM users u"
            " LEFT JOIN orders o ON o.admin_id = u.id AND o.owner_id = ?"
            "   AND o.owner_payout_id IS NULL AND o.cancelled_at IS NULL"
            " WHERE u.role = 'admin' AND u.owner_id = ? AND u.deleted_at IS NULL"
            " GROUP BY u.id ORDER BY u.handle",
            (owner_id, owner_id),
        )

    # --------------------------------------------------------------- Акции

    async def list_promos(self, include_archived: bool = False) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM promos"
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        sql += " ORDER BY id"
        return await self.fetchall(sql)

    async def get_promo(self, promo_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM promos WHERE id = ?", (promo_id,))

    async def create_promo(self, tx: Tx, name: str) -> int:
        """Заводит акцию. У акции есть только имя: приз — это текст."""
        cur = await tx.execute(
            "INSERT INTO promos (name, name_folded, created_at) VALUES (?, ?, ?)",
            (name, name.casefold(), utcnow_iso()),
        )
        return cur.lastrowid

    async def promo_name_taken(self, name: str) -> bool:
        """Есть ли действующая акция с таким именем.

        Сравнение по casefold(): SQLite lower() сворачивает только ASCII,
        поэтому «День рождения» и «день рождения» для него разные строки.
        То же условие закреплено частичным UNIQUE-индексом в схеме.
        """
        row = await self.fetchone(
            "SELECT 1 FROM promos WHERE archived_at IS NULL AND name_folded = ?",
            (name.casefold(),),
        )
        return row is not None

    async def archive_promo(self, tx: Tx, promo_id: int) -> bool:
        """Мягкое удаление акции: кнопка у администраторов исчезает,
        история заказов сохраняется. False — акция уже удалена."""
        cur = await tx.execute(
            "UPDATE promos SET archived_at = ?"
            " WHERE id = ? AND archived_at IS NULL",
            (utcnow_iso(), promo_id),
        )
        return cur.rowcount > 0

    # -------------------------------------------------------------- Бонусы

    async def create_bonus(self, tx: Tx, admin_id: int, amount: float,
                           comment: str) -> int:
        cur = await tx.execute(
            "INSERT INTO bonuses (admin_id, amount, comment, created_at)"
            " VALUES (?, ?, ?, ?)",
            (admin_id, amount, comment, utcnow_iso()),
        )
        return cur.lastrowid

    async def get_bonus(self, bonus_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT b.*, u.handle AS admin_handle FROM bonuses b"
            " JOIN users u ON u.id = b.admin_id WHERE b.id = ?",
            (bonus_id,),
        )

    async def cancel_bonus(self, tx: Tx, bonus_id: int) -> bool:
        """Отмена невыплаченного бонуса; выплаченный не корректируется.
        False — бонус уже отменён, выплачен или не существует."""
        cur = await tx.execute(
            "UPDATE bonuses SET cancelled_at = ?"
            " WHERE id = ? AND cancelled_at IS NULL AND payout_id IS NULL",
            (utcnow_iso(), bonus_id),
        )
        return cur.rowcount > 0

    async def admin_unpaid_bonuses(self, admin_id: int) -> list[aiosqlite.Row]:
        """Невыплаченные бонусы администратора — его текущий период."""
        return await self.fetchall(
            "SELECT * FROM bonuses WHERE admin_id = ?"
            " AND payout_id IS NULL AND cancelled_at IS NULL ORDER BY id",
            (admin_id,),
        )

    # ------------------------------------------------------------- Выплаты

    async def create_payout(self, tx: Tx,
                            user: aiosqlite.Row) -> tuple[int, float, int, float] | None:
        """Закрывает текущий период получателя одной транзакцией.

        Доли администратора и владельца по одному заказу закрываются
        независимо; администратору вместе с заказами закрываются его
        бонусы. Строки помечаются условным UPDATE ровно один раз, сумма
        считается по фактически закрытым строкам, а не по предварительному
        агрегату — двойное нажатие и гонка двух супер-админов дают ровно
        одну выплату. Возвращает (id, сумма, число заказов, сумма бонусов)
        или None, если платить нечего.

        Итог меньше нуля выплатой не бывает (SPEC §4): удержание, которое
        больше начисленного, переносится в следующий период. Правило живёт
        здесь, а не только на экране подтверждения: экран мог быть собран
        до удержания, пришедшего секунду назад, и любой другой вызывающий
        обязан получить ту же защиту. Отрицательный итог откатывает
        пометки — строки остаются открытыми и ждут периода, в котором
        итог станет неотрицательным.

        Ноль при закрытых строках — законная выплата: начислено и ровно
        столько же удержано, строки настоящие, и период обязан
        закрываться, иначе он тянется вечно.
        """
        if user["role"] == "admin":
            user_col, payout_col, share_col = "admin_id", "admin_payout_id", "admin_share"
        else:
            user_col, payout_col, share_col = "owner_id", "owner_payout_id", "owner_share"
        cur = await tx.execute(
            "INSERT INTO payouts (user_id, amount, orders_count, created_at)"
            " VALUES (?, 0, 0, ?)",
            (user["id"], utcnow_iso()),
        )
        payout_id = cur.lastrowid
        cur = await tx.execute(
            f"UPDATE orders SET {payout_col} = ? WHERE {user_col} = ?"
            f" AND {payout_col} IS NULL AND cancelled_at IS NULL",
            (payout_id, user["id"]),
        )
        closed_orders = cur.rowcount
        closed_bonuses = 0
        if user["role"] == "admin":
            cur = await tx.execute(
                "UPDATE bonuses SET payout_id = ? WHERE admin_id = ?"
                " AND payout_id IS NULL AND cancelled_at IS NULL",
                (payout_id, user["id"]),
            )
            closed_bonuses = cur.rowcount
        if closed_orders == 0 and closed_bonuses == 0:
            await self._undo_payout(tx, payout_id, payout_col)
            return None
        closed = await tx.fetchone(
            f"SELECT COUNT(*) AS orders_count, COALESCE(SUM({share_col}), 0) AS total"
            f" FROM orders WHERE {payout_col} = ?",
            (payout_id,),
        )
        bonus_total = 0.0
        if user["role"] == "admin":
            row = await tx.fetchone(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM bonuses"
                " WHERE payout_id = ?",
                (payout_id,),
            )
            bonus_total = round(row["total"], 2)
        amount = round(closed["total"] + bonus_total, 2)
        if amount < 0:
            await self._undo_payout(tx, payout_id, payout_col)
            return None
        await tx.execute(
            "UPDATE payouts SET amount = ?, orders_count = ? WHERE id = ?",
            (amount, closed["orders_count"], payout_id),
        )
        return payout_id, amount, closed["orders_count"], bonus_total

    async def _undo_payout(self, tx: Tx, payout_id: int, payout_col: str) -> None:
        """Снимает пометки выплаты, которой не будет, и убирает её саму.

        Отпускаются ровно те строки, которые пометила эта выплата: уже
        выплаченного не касается, следа в базе не остаётся.
        """
        await tx.execute(
            f"UPDATE orders SET {payout_col} = NULL WHERE {payout_col} = ?",
            (payout_id,),
        )
        await tx.execute(
            "UPDATE bonuses SET payout_id = NULL WHERE payout_id = ?", (payout_id,)
        )
        await tx.execute("DELETE FROM payouts WHERE id = ?", (payout_id,))

    async def payouts_for_user(self, user_id: int, limit: int = 30) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM payouts WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    # ----------------------------------------------------------- Настройки

    async def get_settings(self) -> dict[str, float]:
        """Текущие настройки: значения из БД поверх значений по умолчанию."""
        rows = await self.fetchall("SELECT key, value FROM settings")
        stored = {row["key"]: row["value"] for row in rows}
        return {**SETTINGS_DEFAULTS, **stored}

    async def set_setting(self, tx: Tx, key: str, value: float) -> None:
        await tx.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------ Окно чата

    async def get_window(self, chat_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM chat_windows WHERE chat_id = ?", (chat_id,)
        )

    async def save_window(self, tx: Tx, chat_id: int, message_id: int, *,
                          text: str, markup_json: str | None, rich: bool) -> None:
        await tx.execute(
            "INSERT INTO chat_windows (chat_id, message_id, text, markup_json,"
            " is_rich, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET message_id = excluded.message_id,"
            " text = excluded.text, markup_json = excluded.markup_json,"
            " is_rich = excluded.is_rich, updated_at = excluded.updated_at",
            (chat_id, message_id, text, markup_json, int(rich), utcnow_iso()),
        )

    async def drop_window(self, tx: Tx, chat_id: int) -> None:
        await tx.execute("DELETE FROM chat_windows WHERE chat_id = ?", (chat_id,))

    async def prune_orphan_windows(self, tx: Tx, *, keep_chat_ids: set[int],
                                   before_iso: str) -> int:
        """Убирает окна чатов, за которыми не стоит ни учётная запись, ни
        супер-админ: иначе таблица растёт от каждого случайного /start."""
        rows = await tx.fetchall(
            "SELECT chat_id FROM chat_windows WHERE updated_at < ?"
            " AND chat_id NOT IN (SELECT chat_id FROM user_chats)",
            (before_iso,),
        )
        stale = [r["chat_id"] for r in rows if r["chat_id"] not in keep_chat_ids]
        for chat_id in stale:
            await tx.execute("DELETE FROM chat_windows WHERE chat_id = ?", (chat_id,))
        return len(stale)

    # ------------------------------------------------------------- Экспорт

    async def export_users(self, owner_id: int | None = None) -> list[aiosqlite.Row]:
        """Учётные записи; owner_id ограничивает выборку администраторами
        этого владельца (экспорт владельца)."""
        sql = (
            "SELECT u.id, u.role, u.handle, u.name, u.contact, u.percent,"
            " u.series_reset_min, u.is_active, u.deleted_at, u.created_at,"
            " w.handle AS owner_handle,"
            " (SELECT COUNT(*) FROM user_chats c WHERE c.user_id = u.id)"
            "   AS chats_count"
            " FROM users u LEFT JOIN users w ON w.id = u.owner_id"
        )
        params: tuple = ()
        if owner_id is not None:
            sql += " WHERE u.role = 'admin' AND u.owner_id = ?"
            params = (owner_id,)
        sql += " ORDER BY u.id"
        return await self.fetchall(sql, params)

    async def export_orders(self, owner_id: int | None = None) -> list[aiosqlite.Row]:
        """Заказы; owner_id ограничивает выборку заказами этого владельца."""
        sql = (
            "SELECT o.*, a.handle AS admin_handle, w.handle AS owner_handle"
            " FROM orders o"
            " JOIN users a ON a.id = o.admin_id"
            " LEFT JOIN users w ON w.id = o.owner_id"
        )
        params: tuple = ()
        if owner_id is not None:
            sql += " WHERE o.owner_id = ?"
            params = (owner_id,)
        sql += " ORDER BY o.id"
        return await self.fetchall(sql, params)

    async def export_payouts(self, user_id: int | None = None) -> list[aiosqlite.Row]:
        """Выплаты; user_id ограничивает выборку выплатами получателя."""
        sql = (
            "SELECT y.id, u.handle, u.role, y.amount, y.orders_count, y.created_at"
            " FROM payouts y JOIN users u ON u.id = y.user_id"
        )
        params: tuple = ()
        if user_id is not None:
            sql += " WHERE y.user_id = ?"
            params = (user_id,)
        sql += " ORDER BY y.id"
        return await self.fetchall(sql, params)

    async def export_bonuses(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT b.*, u.handle AS admin_handle"
            " FROM bonuses b JOIN users u ON u.id = b.admin_id ORDER BY b.id"
        )

    async def export_audit(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT l.*, u.handle AS actor_handle FROM audit_log l"
            " LEFT JOIN users u ON u.id = l.actor_user_id ORDER BY l.id"
        )

    async def export_owner_audit(self, owner_id: int) -> list[aiosqlite.Row]:
        """Журнал действий, касающихся владельца: его заказы, его учётная
        запись, его администраторы и его выплаты. Чужого — ничего."""
        return await self.fetchall(
            "SELECT l.*, u.handle AS actor_handle FROM audit_log l"
            " LEFT JOIN users u ON u.id = l.actor_user_id"
            " WHERE (l.entity = 'order' AND l.entity_id IN"
            "         (SELECT id FROM orders WHERE owner_id = ?))"
            "    OR (l.entity = 'user' AND (l.entity_id = ?"
            "         OR l.entity_id IN (SELECT id FROM users WHERE owner_id = ?)))"
            "    OR (l.entity = 'payout' AND l.entity_id IN"
            "         (SELECT id FROM payouts WHERE user_id = ?))"
            " ORDER BY l.id",
            (owner_id, owner_id, owner_id, owner_id),
        )
