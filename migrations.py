"""Версионированные миграции схемы.

Счётчик версии — `PRAGMA user_version`. Каждая миграция применяется в
собственной транзакции `BEGIN IMMEDIATE`, вместе с ней в той же транзакции
поднимается версия и проверяются пост-условия: версия и данные едут вместе
или не едут вовсе. Прерванная миграция откатывается целиком и повторяется
при следующем старте — состояния «версия новая, данных нет» не существует.

Три правила, на которых держится безопасность:

1. Версия поднимается внутри транзакции миграции.
2. Пост-условия проверяются там же: пересборка таблицы сверяет число строк
   до и после и откатывается, если они разошлись.
3. База новее кода — бот отказывается стартовать. Откатить релиз, не
   откатив схему, нельзя молча.

Перед первой миграцией вызывается before_migration() — туда main.py вешает
неизменяемый снимок базы.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

# --------------------------------------------------------------- v1: базис
#
# Схема бота на момент начала работ. Применяется к пустому файлу как
# создание с нуля, к базе прежней версии — как дотягивание (колонка
# series_reset_min, пересборка заказов под акции и лесенку, перенос
# акции «3=4» из настроек в таблицу акций), к текущей боевой базе —
# как пустая операция с последующим клеймом версии.

ORDERS_TABLE_V1 = """
CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id         INTEGER NOT NULL REFERENCES users(id),
    owner_id         INTEGER REFERENCES users(id),
    kind             TEXT    NOT NULL CHECK (kind IN ('standard', 'group', 'promo')),
    headsets         INTEGER,
    minutes          INTEGER,
    promo_id         INTEGER REFERENCES promos(id),
    promo_name       TEXT,
    base_price       REAL    NOT NULL,
    discount_percent REAL    NOT NULL DEFAULT 0,
    price            REAL    NOT NULL,
    admin_percent    REAL    NOT NULL,
    admin_share      REAL    NOT NULL,
    series_pos       INTEGER,
    owner_percent    REAL    NOT NULL DEFAULT 0,
    owner_share      REAL    NOT NULL DEFAULT 0,
    admin_payout_id  INTEGER REFERENCES payouts(id),
    owner_payout_id  INTEGER REFERENCES payouts(id),
    cancelled_at     TEXT,
    created_at       TEXT    NOT NULL
)
"""

ORDERS_INDEXES_V1 = [
    "CREATE INDEX IF NOT EXISTS idx_orders_admin_unpaid ON orders(admin_id)"
    " WHERE admin_payout_id IS NULL AND cancelled_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_orders_owner_unpaid ON orders(owner_id)"
    " WHERE owner_payout_id IS NULL AND cancelled_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_orders_admin_created ON orders(admin_id, created_at)"
    " WHERE cancelled_at IS NULL",
]

SCHEMA_V1 = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        role             TEXT    NOT NULL CHECK (role IN ('owner', 'admin')),
        handle           TEXT    NOT NULL,
        name             TEXT    NOT NULL,
        contact          TEXT    NOT NULL DEFAULT '',
        password_hash    TEXT    NOT NULL,
        percent          REAL    NOT NULL,
        owner_id         INTEGER REFERENCES users(id),
        series_reset_min INTEGER,
        is_active        INTEGER NOT NULL DEFAULT 1,
        deleted_at       TEXT,
        created_at       TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_handle ON users(handle)",
    "CREATE INDEX IF NOT EXISTS idx_users_owner ON users(owner_id)",
    """
    CREATE TABLE IF NOT EXISTS user_chats (
        chat_id    INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_chats_user ON user_chats(user_id)",
    """
    CREATE TABLE IF NOT EXISTS payouts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL REFERENCES users(id),
        amount       REAL    NOT NULL,
        orders_count INTEGER NOT NULL,
        created_at   TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_payouts_user ON payouts(user_id)",
    """
    CREATE TABLE IF NOT EXISTS promos (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        price       REAL    NOT NULL,
        archived_at TEXT,
        created_at  TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bonuses (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id     INTEGER NOT NULL REFERENCES users(id),
        amount       REAL    NOT NULL,
        comment      TEXT    NOT NULL DEFAULT '',
        payout_id    INTEGER REFERENCES payouts(id),
        cancelled_at TEXT,
        created_at   TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bonuses_admin_unpaid ON bonuses(admin_id)"
    " WHERE payout_id IS NULL AND cancelled_at IS NULL",
    ORDERS_TABLE_V1,
    *ORDERS_INDEXES_V1,
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS windows (chat_id INTEGER PRIMARY KEY,"
    " message_id INTEGER NOT NULL)",
]

# Колонки заказов, общие для старой и новой схемы, — для переноса данных
_ORDERS_COMMON_COLS = (
    "id, admin_id, owner_id, kind, headsets, minutes, base_price,"
    " discount_percent, price, admin_percent, admin_share, owner_percent,"
    " owner_share, admin_payout_id, owner_payout_id, cancelled_at, created_at"
)


async def _columns(conn, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _scalar(conn, sql: str, params=()):
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return row[0] if row else None


async def _apply_v1(conn) -> None:
    for statement in SCHEMA_V1:
        await conn.execute(statement)

    if "series_reset_min" not in await _columns(conn, "users"):
        await conn.execute("ALTER TABLE users ADD COLUMN series_reset_min INTEGER")

    if "promo_id" not in await _columns(conn, "orders"):
        # Пересборка таблицы заказов: CHECK по kind пополнился 'promo',
        # добавились promo_id, promo_name и series_pos. SQLite не меняет
        # CHECK через ALTER, поэтому таблица создаётся заново.
        before = await _scalar(conn, "SELECT COUNT(*) FROM orders")
        await conn.execute("ALTER TABLE orders RENAME TO orders_legacy")
        await conn.execute(ORDERS_TABLE_V1)
        await conn.execute(
            f"INSERT INTO orders ({_ORDERS_COMMON_COLS})"
            f" SELECT {_ORDERS_COMMON_COLS} FROM orders_legacy"
        )
        after = await _scalar(conn, "SELECT COUNT(*) FROM orders")
        if after != before:
            raise RuntimeError(
                f"Пересборка заказов потеряла строки: было {before}, стало {after}"
            )
        # DROP уносит индексы прежней таблицы — создаём их заново
        await conn.execute("DROP TABLE orders_legacy")
        for statement in ORDERS_INDEXES_V1:
            await conn.execute(statement)
        # Акция «3=4» была настройкой — переносится в таблицу акций,
        # если была включена; её прежние ключи настроек более не нужны
        cur = await conn.execute(
            "SELECT key, value FROM settings"
            " WHERE key IN ('group_enabled', 'price_group')"
        )
        legacy = {row[0]: row[1] for row in await cur.fetchall()}
        if legacy.get("group_enabled", 1):
            from utils import utcnow_iso
            await conn.execute(
                "INSERT INTO promos (name, price, created_at) VALUES (?, ?, ?)",
                ("3=4", legacy.get("price_group", 600), utcnow_iso()),
            )
        await conn.execute(
            "DELETE FROM settings WHERE key IN ('group_enabled', 'price_group')"
        )


async def _verify_v1(conn) -> None:
    for table in ("users", "user_chats", "orders", "promos", "bonuses",
                  "payouts", "settings"):
        if await _scalar(
            conn, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ) != 1:
            raise RuntimeError(f"После миграции v1 нет таблицы {table}")
    if {"promo_id", "promo_name", "series_pos"} - await _columns(conn, "orders"):
        raise RuntimeError("После миграции v1 в orders нет колонок акций и серии")


# ---------------------------------------------------- v2: боевая надёжность
#
# Идемпотентность заказов, журнал действий, очередь доставки, окно чата
# с содержимым, состояние сценариев, список супер-админов, уникальность
# логинов и имён акций на уровне базы.

SCHEMA_V2 = [
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        at            TEXT    NOT NULL,
        actor_kind    TEXT    NOT NULL,
        actor_tg_id   INTEGER,
        actor_user_id INTEGER,
        action        TEXT    NOT NULL,
        entity        TEXT    NOT NULL,
        entity_id     INTEGER,
        before_json   TEXT,
        after_json    TEXT,
        request_id    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_tg_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at)",
    """
    CREATE TABLE IF NOT EXISTS outbox (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id         INTEGER NOT NULL,
        kind            TEXT    NOT NULL,
        payload         TEXT    NOT NULL,
        dedup_key       TEXT    UNIQUE,
        status          TEXT    NOT NULL DEFAULT 'pending',
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT    NOT NULL,
        last_error      TEXT,
        created_at      TEXT    NOT NULL,
        sent_message_id INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status, next_attempt_at, id)",
    """
    CREATE TABLE IF NOT EXISTS chat_windows (
        chat_id     INTEGER PRIMARY KEY,
        message_id  INTEGER NOT NULL,
        text        TEXT    NOT NULL DEFAULT '',
        markup_json TEXT,
        is_rich     INTEGER NOT NULL DEFAULT 0,
        updated_at  TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS super_admins (
        tg_id      INTEGER PRIMARY KEY,
        label      TEXT    NOT NULL DEFAULT '',
        added_by   INTEGER,
        added_at   TEXT    NOT NULL,
        removed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fsm_state (
        bot_id     INTEGER NOT NULL,
        chat_id    INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        thread_id  INTEGER NOT NULL DEFAULT 0,
        business   TEXT    NOT NULL DEFAULT '',
        destiny    TEXT    NOT NULL DEFAULT 'default',
        state      TEXT,
        data       TEXT    NOT NULL DEFAULT '{}',
        updated_at TEXT    NOT NULL,
        PRIMARY KEY (bot_id, chat_id, user_id, thread_id, business, destiny)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fsm_updated ON fsm_state(updated_at)",
]


async def _apply_v2(conn) -> None:
    for statement in SCHEMA_V2:
        await conn.execute(statement)

    order_cols = await _columns(conn, "orders")
    # UNIQUE нельзя добавить через ALTER — колонка плюс уникальный индекс;
    # NULL в SQLite не конфликтуют, поэтому исторические заказы без токена
    # индексу не мешают.
    if "client_token" not in order_cols:
        await conn.execute("ALTER TABLE orders ADD COLUMN client_token TEXT")
    if "quoted_at" not in order_cols:
        await conn.execute("ALTER TABLE orders ADD COLUMN quoted_at TEXT")
    if "owner_suspended" not in order_cols:
        await conn.execute(
            "ALTER TABLE orders ADD COLUMN owner_suspended INTEGER NOT NULL DEFAULT 0"
        )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_token"
        " ON orders(client_token) WHERE client_token IS NOT NULL"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)"
    )

    if "name_folded" not in await _columns(conn, "promos"):
        await conn.execute("ALTER TABLE promos ADD COLUMN name_folded TEXT")
    cur = await conn.execute("SELECT id, name FROM promos")
    for row in await cur.fetchall():
        await conn.execute("UPDATE promos SET name_folded = ? WHERE id = ?",
                           (str(row[1]).casefold(), row[0]))

    # Уникальность, которую до сих пор держал только код, становится
    # свойством базы. Дубликаты — повод остановиться и позвать человека,
    # а не тихо что-то переписать.
    dupes = await _duplicates(
        conn,
        "SELECT name_folded FROM promos WHERE archived_at IS NULL"
        " GROUP BY name_folded HAVING COUNT(*) > 1",
    )
    if dupes:
        raise RuntimeError(
            "Одинаковые названия действующих акций: " + ", ".join(dupes)
            + ". Удалите лишние акции в боте и запустите обновление снова"
        )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_promos_active_name"
        " ON promos(name_folded) WHERE archived_at IS NULL"
    )

    dupes = await _duplicates(
        conn,
        "SELECT handle FROM users WHERE is_active = 1 AND deleted_at IS NULL"
        " GROUP BY handle HAVING COUNT(*) > 1",
    )
    if dupes:
        raise RuntimeError(
            "Один логин у нескольких действующих учётных записей: "
            + ", ".join(dupes)
            + ". Приостановите лишние записи и запустите обновление снова"
        )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_active_handle"
        " ON users(handle) WHERE is_active = 1 AND deleted_at IS NULL"
    )

    # Прежняя таблица окон хранила только message_id. Переносим указатели
    # с пустым текстом: такое окно не воспроизводится, а удаляется при
    # первом же обращении — стороннего интерактивного сообщения в чате
    # после обновления не остаётся.
    if await _scalar(
        conn, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='windows'"
    ):
        from utils import utcnow_iso
        await conn.execute(
            "INSERT INTO chat_windows (chat_id, message_id, text, markup_json,"
            " is_rich, updated_at)"
            " SELECT chat_id, message_id, '', NULL, 0, ? FROM windows"
            " WHERE chat_id NOT IN (SELECT chat_id FROM chat_windows)",
            (utcnow_iso(),),
        )
        await conn.execute("DROP TABLE windows")


async def _duplicates(conn, sql: str) -> list[str]:
    cur = await conn.execute(sql)
    return [str(row[0]) for row in await cur.fetchall()]


async def _verify_v2(conn) -> None:
    for table in ("audit_log", "outbox", "chat_windows", "super_admins", "fsm_state"):
        if await _scalar(
            conn, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ) != 1:
            raise RuntimeError(f"После миграции v2 нет таблицы {table}")
    if {"client_token", "quoted_at", "owner_suspended"} - await _columns(conn, "orders"):
        raise RuntimeError("После миграции v2 в orders нет новых колонок")
    if await _scalar(
        conn, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='windows'"
    ):
        raise RuntimeError("После миграции v2 осталась прежняя таблица windows")


# ------------------------------------------- v3: граница серии в заказе
#
# Место в лесенке считается от момента последнего сброса 12-часовой серии.
# Этот момент выводился из **текущей** настройки администратора, поэтому
# смена времени сбросов задним числом перекраивала прошлые серии: заказы,
# оформленные с разными ступенями, оказывались в одной серии, и проверка
# инвариантов объявляла нарушением то, чего не было. Копия базы такой
# проверки не проходит и не публикуется — одна безобидная настройка
# останавливала резервное копирование.
#
# Граница серии — такой же факт момента оформления, как вознаграждение и
# доля владельца, и хранится там же, в заказе. Заказы прежних версий
# границы не имеют: восстановить её нечем, и проверка их пропускает.

SCHEMA_V3 = [
    "CREATE INDEX IF NOT EXISTS idx_orders_series"
    " ON orders(admin_id, series_since)",
]


async def _apply_v3(conn) -> None:
    if "series_since" not in await _columns(conn, "orders"):
        await conn.execute("ALTER TABLE orders ADD COLUMN series_since TEXT")
    for statement in SCHEMA_V3:
        await conn.execute(statement)


async def _verify_v3(conn) -> None:
    if "series_since" not in await _columns(conn, "orders"):
        raise RuntimeError("После миграции v3 в orders нет колонки series_since")


# --------------------------------- v4: бесплатные 15 минут как тип заказа
#
# «15 минут бесплатно» — не заказ с нулевой ценой, а отдельный тип: он не
# занимает ступень лесенки, и опознавать его приходится во всех отчётах,
# выгрузках и проверках. Опознание по совпадению цены с нулём было бы
# догадкой — тип записан в самом заказе.
#
# CHECK по `kind` SQLite на месте не меняет, поэтому таблица заказов
# пересобирается тем же порядком, что в v1, со сверкой числа строк до и
# после. Все колонки переносятся явно: молчаливая потеря `client_token`
# сняла бы защиту от повторного оформления.
#
# Здесь же уходят настройки прежнего ПК-бонуса: понятие удалено целиком,
# а строка в settings, которую никто не читает, — приглашение к ошибке.

ORDERS_TABLE_V4 = """
CREATE TABLE orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id         INTEGER NOT NULL REFERENCES users(id),
    owner_id         INTEGER REFERENCES users(id),
    kind             TEXT    NOT NULL
        CHECK (kind IN ('standard', 'group', 'promo', 'free15')),
    headsets         INTEGER,
    minutes          INTEGER,
    promo_id         INTEGER REFERENCES promos(id),
    promo_name       TEXT,
    base_price       REAL    NOT NULL,
    discount_percent REAL    NOT NULL DEFAULT 0,
    price            REAL    NOT NULL,
    admin_percent    REAL    NOT NULL,
    admin_share      REAL    NOT NULL,
    series_pos       INTEGER,
    series_since     TEXT,
    owner_percent    REAL    NOT NULL DEFAULT 0,
    owner_share      REAL    NOT NULL DEFAULT 0,
    owner_suspended  INTEGER NOT NULL DEFAULT 0,
    admin_payout_id  INTEGER REFERENCES payouts(id),
    owner_payout_id  INTEGER REFERENCES payouts(id),
    client_token     TEXT,
    quoted_at        TEXT,
    cancelled_at     TEXT,
    created_at       TEXT    NOT NULL
)
"""

# Индексы заказов уносит вместе с прежней таблицей — все до одного
# создаются заново, включая уникальный по client_token (идемпотентность
# оформления) и индекс серии из v3.
ORDERS_INDEXES_V4 = [
    *ORDERS_INDEXES_V1,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_token"
    " ON orders(client_token) WHERE client_token IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)",
    *SCHEMA_V3,
]

_ORDERS_COLS_V4 = (
    "id, admin_id, owner_id, kind, headsets, minutes, promo_id, promo_name,"
    " base_price, discount_percent, price, admin_percent, admin_share,"
    " series_pos, series_since, owner_percent, owner_share, owner_suspended,"
    " admin_payout_id, owner_payout_id, client_token, quoted_at, cancelled_at,"
    " created_at"
)

_ORDERS_COLS_V4_SET = frozenset(_ORDERS_COLS_V4.replace(" ", "").split(","))

OBSOLETE_SETTINGS_V4 = ("pc_bonus_enabled", "pc_bonus_points")


async def _orders_sql(conn) -> str:
    return await _scalar(
        conn, "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'"
    ) or ""


async def _apply_v4(conn) -> None:
    if "free15" not in await _orders_sql(conn):
        before = await _scalar(conn, "SELECT COUNT(*) FROM orders")
        # Число строк не поймало бы потерю колонки: перенос идёт по
        # списку имён, и колонка, которой в списке нет, исчезла бы молча
        lost = await _columns(conn, "orders") - _ORDERS_COLS_V4_SET
        if lost:
            raise RuntimeError(
                f"Пересборка заказов потеряла бы колонки: {sorted(lost)}."
                f" Перенесите их вручную и запустите обновление снова"
            )
        await conn.execute("ALTER TABLE orders RENAME TO orders_legacy")
        await conn.execute(ORDERS_TABLE_V4)
        await conn.execute(
            f"INSERT INTO orders ({_ORDERS_COLS_V4})"
            f" SELECT {_ORDERS_COLS_V4} FROM orders_legacy"
        )
        after = await _scalar(conn, "SELECT COUNT(*) FROM orders")
        if after != before:
            raise RuntimeError(
                f"Пересборка заказов потеряла строки: было {before}, стало {after}"
            )
        # DROP уносит индексы прежней таблицы — создаём их заново
        await conn.execute("DROP TABLE orders_legacy")
        for statement in ORDERS_INDEXES_V4:
            await conn.execute(statement)
    await conn.execute(
        "DELETE FROM settings WHERE key IN (?, ?)", OBSOLETE_SETTINGS_V4
    )


async def _verify_v4(conn) -> None:
    if "free15" not in await _orders_sql(conn):
        raise RuntimeError("После миграции v4 заказы не принимают тип free15")
    missing = _ORDERS_COLS_V4_SET - await _columns(conn, "orders")
    if missing:
        raise RuntimeError(f"После миграции v4 в orders нет колонок: {missing}")
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='orders'")
    indexes = {row[0] for row in await cur.fetchall()}
    if "idx_orders_client_token" not in indexes:
        raise RuntimeError(
            "После миграции v4 нет уникального индекса по client_token:"
            " повторное оформление создало бы второй заказ")
    if await _scalar(
        conn, "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
              " AND name='orders_legacy'"
    ):
        raise RuntimeError("После миграции v4 осталась прежняя таблица заказов")
    if await _scalar(conn, "SELECT COUNT(*) FROM settings WHERE key IN (?, ?)",
                     OBSOLETE_SETTINGS_V4):
        raise RuntimeError("После миграции v4 остались настройки ПК-бонуса")


# ------------------------------- v5: у акции больше нет денежной величины
#
# Акция — приз колеса фортуны, то есть текстовый признак заказа: какой
# приз клиенту достался. Денежной величины у неё нет и не должно быть,
# иначе рано или поздно кто-нибудь снова вычтет её из цены сеанса. Колонка
# `promos.price` уходит из схемы — «оставить и не читать» здесь хуже, чем
# убрать: невидимое поле переживает любую договорённость.
#
# Заказы не трогаются вовсе. Цены, доли и остатки, записанные когда
# угодно, остаются ровно такими, как записаны, — включая исторические
# заказы прежнего типа `promo`, у которых цена когда-то бралась из акции.
# Снимок названия (`orders.promo_name`) — всё, что связывает заказ с
# акцией, и он остаётся на месте.
#
# Таблица пересобирается, а не правится ALTER-ом: DROP COLUMN появился
# только в SQLite 3.35, а версию на сервере знать заранее нельзя.
# Порядок действий выбран так, чтобы не переименовывать саму `promos`:
# при переименовании SQLite переписывает ссылки на старое имя в других
# таблицах, и `orders.promo_id REFERENCES promos(id)` стал бы ссылаться
# на временную таблицу. Поэтому новая таблица создаётся под своим именем
# и получает имя `promos` последней, когда ссылаться на неё уже некому.

PROMOS_TABLE_V5 = """
CREATE TABLE promos_v5 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    name_folded TEXT,
    archived_at TEXT,
    created_at  TEXT    NOT NULL
)
"""

PROMOS_INDEX_V5 = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_promos_active_name"
    " ON promos(name_folded) WHERE archived_at IS NULL"
)

_PROMOS_COLS_V5 = "id, name, name_folded, archived_at, created_at"


async def _apply_v5(conn) -> None:
    if "price" not in await _columns(conn, "promos"):
        return
    before = await _scalar(conn, "SELECT COUNT(*) FROM promos")
    await conn.execute(PROMOS_TABLE_V5)
    await conn.execute(
        f"INSERT INTO promos_v5 ({_PROMOS_COLS_V5})"
        f" SELECT {_PROMOS_COLS_V5} FROM promos"
    )
    after = await _scalar(conn, "SELECT COUNT(*) FROM promos_v5")
    if after != before:
        raise RuntimeError(
            f"Пересборка акций потеряла строки: было {before}, стало {after}"
        )
    await conn.execute("DROP TABLE promos")
    await conn.execute("ALTER TABLE promos_v5 RENAME TO promos")
    await conn.execute(PROMOS_INDEX_V5)


async def _verify_v5(conn) -> None:
    columns = await _columns(conn, "promos")
    if "price" in columns:
        raise RuntimeError("После миграции v5 у акций осталась денежная величина")
    if set(_PROMOS_COLS_V5.replace(" ", "").split(",")) - columns:
        raise RuntimeError(f"После миграции v5 в promos нет колонок: {columns}")
    if await _scalar(
        conn, "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
              " AND name='promos_v5'"
    ):
        raise RuntimeError("После миграции v5 осталась временная таблица акций")
    if not await _scalar(
        conn, "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
              " AND name='idx_promos_active_name'"
    ):
        raise RuntimeError(
            "После миграции v5 нет уникального индекса по названию акции")
    # Заказы обязаны пережить пересборку нетронутыми — и данными, и связью
    orders_sql = await _orders_sql(conn)
    if "REFERENCES promos(id)" not in orders_sql:
        raise RuntimeError(
            "После миграции v5 заказы ссылаются не на таблицу акций:"
            " переименование увело связь на временную таблицу")


# ------------------------------------------------------------------ Раннер

@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[object], Awaitable[None]]
    verify: Callable[[object], Awaitable[None]] | None = None
    rebuilds_tables: bool = False


MIGRATIONS: list[Migration] = [
    Migration(1, "базовая схема", _apply_v1, _verify_v1, rebuilds_tables=True),
    Migration(2, "надёжность: журнал, очередь, окно, состояние", _apply_v2, _verify_v2),
    Migration(3, "граница серии хранится в заказе", _apply_v3, _verify_v3),
    Migration(4, "бесплатные 15 минут — отдельный тип заказа", _apply_v4,
              _verify_v4, rebuilds_tables=True),
    Migration(5, "у акции нет денежной величины", _apply_v5, _verify_v5,
              rebuilds_tables=True),
]

LATEST_VERSION = max(m.version for m in MIGRATIONS)


async def pending_migrations(conn) -> list[Migration]:
    current = await _scalar(conn, "PRAGMA user_version")
    if current > LATEST_VERSION:
        raise RuntimeError(
            f"База данных новее программы: версия схемы {current}, "
            f"программа знает {LATEST_VERSION}. Запустите прежнюю версию бота "
            f"или восстановите базу из копии, снятой до обновления"
        )
    return [m for m in MIGRATIONS if m.version > current]


async def apply_migrations(conn, *, before_migration=None) -> list[Migration]:
    """Применяет недостающие миграции по одной, каждую — атомарно."""
    pending = await pending_migrations(conn)
    if not pending:
        return []
    if before_migration is not None:
        await before_migration(pending)
    for migration in pending:
        log.info("Миграция v%s: %s", migration.version, migration.name)
        # PRAGMA foreign_keys внутри транзакции не действует — выключаем до BEGIN
        if migration.rebuilds_tables:
            await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await migration.apply(conn)
            if migration.verify is not None:
                await migration.verify(conn)
            await conn.execute(f"PRAGMA user_version = {int(migration.version)}")
        except BaseException:
            await conn.execute("ROLLBACK")
            await conn.execute("PRAGMA foreign_keys = ON")
            log.error("Миграция v%s не применена, база не изменена", migration.version)
            raise
        await conn.execute("COMMIT")
        await conn.execute("PRAGMA foreign_keys = ON")
        broken = await _duplicates(conn, "PRAGMA foreign_key_check")
        if broken:
            raise RuntimeError(
                f"После миграции v{migration.version} нарушены связи таблиц: {broken}"
            )
    log.info("Схема базы данных приведена к версии %s", LATEST_VERSION)
    return pending
