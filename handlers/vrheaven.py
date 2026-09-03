"""Панель VR Heaven: учётные записи, акции, бонусы, выплаты, настройки,
доступ, экспорт и отмена заказов.

Каждое изменение денег или доступа пишется в журнал действий в той же
транзакции, что и само изменение: не записанного действия не бывает.
Уведомления ставятся в очередь там же — если транзакция откатится,
никто не получит сообщение о том, чего не произошло.
"""

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

import backup as bk
import errors
import export as xp
import keyboards as kb
import notify
import reports
from config import Config
from db import Actor, Database
from errors import STALE_BUTTON, cb_int, cb_ints, cb_tail
from handlers.common import (
    SUPPORT_LINE,
    event_message,
    guide_bytes,
    guide_caption,
    in_panel_topic,
)
from markup import (
    COMMENT_MAX,
    CONTACT_MAX,
    HANDLE_MAX,
    NAME_MAX,
    PROMO_NAME_MAX,
    h,
    join,
    lines,
)
from messaging import Messenger, document_payload
from pricing import (
    FREE15_LABEL,
    WEEKDAY_NAMES,
    fmt_days,
    normalize_reset,
    order_row_label,
    toggle_day,
)
from roster import is_bootstrap, is_super_admin, super_admin_ids
from utils import (
    fmt_dt,
    fmt_money,
    fmt_percent,
    fmt_reset_times,
    fmt_signed_money,
    gen_password,
    hash_password_async,
    parse_amount,
    parse_percent,
    parse_series_reset,
    parse_time_range,
    utcnow_iso,
)

log = logging.getLogger(__name__)

HANDLE_RE = re.compile(r"[a-z0-9_-]{2,32}")

# Префикс callback-данных -> роль, которой управляет раздел
PREFIX_ROLES = {"ow": "owner", "ad": "admin"}
ADD_TITLES = {"ow": "Новый владелец", "ad": "Новый администратор"}

SETTING_TITLES = {
    "price_1_15": "Цена · 1 шлем · 15 мин",
    "price_1_30": "Цена · 1 шлем · 30 мин",
    "price_1_60": "Цена · 1 шлем · 60 мин",
    "price_2_15": "Цена · 2 шлема · 15 мин",
    "price_2_30": "Цена · 2 шлема · 30 мин",
    "price_2_60": "Цена · 2 шлема · 60 мин",
    "discount_percent": "Размер скидки",
    "discount_time": "Время скидки",
}
# Экран, на который возвращает «Отмена» ввода настройки
SETTING_SCREENS = {"discount_percent": "st:disc", "discount_time": "st:disc"}
# Переключатели и экран каждого: список допустимых ключей выводится отсюда,
# чтобы переключатель без своего экрана нельзя было завести по недосмотру
TOGGLE_SCREENS = {"discount_enabled": "st:disc", "free15_enabled": "st:free"}
TOGGLE_KEYS = frozenset(TOGGLE_SCREENS)


class SuperAdminFilter(Filter):
    """Действующий список: ADMIN_IDS из окружения плюс добавленные в боте."""

    async def __call__(self, event: TelegramObject, db: Database,
                       config: Config) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and await is_super_admin(db, config, user.id)


def panel_chat(event: TelegramObject, config: Config) -> bool:
    """Где открывается панель: личный чат и настроенная тема группы.

    Группа не задана — остаются одни личные чаты, как было. Задана —
    три супер-админа делят одно Окно в одной теме и удаляют личные чаты
    с ботом; личный чат при этом продолжает работать запасным входом.
    """
    chat = getattr(event_message(event), "chat", None)
    if chat is None:
        return False
    return chat.type == "private" or in_panel_topic(event, config)


router = Router(name="vrheaven")
router.message.filter(panel_chat, SuperAdminFilter())
router.callback_query.filter(panel_chat, SuperAdminFilter())


class AddOwnerSG(StatesGroup):
    handle = State()
    name = State()
    contact = State()


class AddAdminSG(StatesGroup):
    handle = State()
    name = State()
    contact = State()
    owner = State()


class EditPercentSG(StatesGroup):
    value = State()


class EditSettingSG(StatesGroup):
    value = State()


class AddPromoSG(StatesGroup):
    name = State()


class AddBonusSG(StatesGroup):
    amount = State()
    comment = State()


class EditResetSG(StatesGroup):
    value = State()


class FindOrderSG(StatesGroup):
    number = State()


class AddSuperAdminSG(StatesGroup):
    tg_id = State()


MENU_TEXT = join("<b>VR Heaven · Учёт VR-сеансов</b>\nПанель VR Heaven",
                 "Выберите раздел")


def _actor(cb_or_message) -> Actor:
    return Actor.superadmin(cb_or_message.from_user.id)


async def _window(cb: CallbackQuery, ui: Messenger, content, markup=None) -> None:
    await ui.window(cb.message.chat.id, content, markup,
                    source_message_id=cb.message.message_id)


# ------------------------------------------------------------ Меню и отмена

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, ui: Messenger) -> None:
    await state.clear()
    await ui.drop_user_message(message)
    # fresh=True: /start обязан оставить в чате видимое сообщение — см.
    # Messenger.window. Правка прежнего Окна проходит и в очищенном чате,
    # где человеку её уже не увидеть
    await ui.window(message.chat.id, MENU_TEXT, kb.vrheaven_menu_kb(), fresh=True)


@router.message(Command("system"))
async def cmd_system(message: Message, db: Database, ui: Messenger,
                     config: Config) -> None:
    """Состояние бота по требованию — вместо сводки, приходившей в 09:00.

    Ежедневная сводка приходила, когда её никто не ждал, и молчала,
    когда её ждали. Здесь то же самое приходит по команде: Записью,
    которая остаётся в чате, а не Окном, которое сотрётся следующим
    экраном.
    """
    await ui.drop_user_message(message)
    report = await reports.system_report(
        db, config.tz, errors=errors.COUNTERS["errors"],
        backups=bk.list_backups(config.backup_dir))
    async with db.write() as tx:
        await notify.to_chat(db, tx, message.chat.id, report.to_html(rich=False),
                             kind="system")
    ui.wake()


@router.callback_query(F.data == "am")
async def cb_menu(cb: CallbackQuery, state: FSMContext, ui: Messenger) -> None:
    await state.clear()
    await _window(cb, ui, MENU_TEXT, kb.vrheaven_menu_kb())
    await cb.answer()


@router.callback_query(F.data == kb.CANCEL_CB)
async def cb_cancel(cb: CallbackQuery, state: FSMContext, ui: Messenger) -> None:
    await state.clear()
    await _window(cb, ui, MENU_TEXT, kb.vrheaven_menu_kb())
    await cb.answer("Действие отменено")


@router.callback_query(F.data == "guide")
async def guide(cb: CallbackQuery, db: Database, ui: Messenger,
                config: Config) -> None:
    _, filename, data = guide_bytes(config, "superadmin")
    async with db.write() as tx:
        await notify.to_chat(db, tx, cb.message.chat.id, guide_caption("superadmin"),
                             kind="guide", document=document_payload(filename, data))
    ui.wake()
    await cb.answer("Инструкция отправлена в чат")


# ------------------------------------------------------------ Отменить заказ

async def _cancel_list_screen(cb: CallbackQuery, db: Database, ui: Messenger,
                              config: Config, before_id: int | None) -> None:
    orders = await db.last_orders(kb.PAGE_SIZE + 1, before_id)
    has_next = len(orders) > kb.PAGE_SIZE
    orders = orders[:kb.PAGE_SIZE]
    if not orders:
        await _window(cb, ui, join("<b>Отмена заказа</b>", "Заказов пока нет"),
                      kb.to_vrheaven_menu_kb())
        return
    await _window(
        cb, ui,
        join("<b>Отмена заказа</b>",
             "Выберите заказ или найдите его по номеру. VR Heaven отменяет "
             "заказ любой давности"),
        kb.orders_pick_kb(
            orders, config.tz,
            prev_cb="ac:list" if before_id else None,
            next_cb=f"ac:page:{orders[-1]['id']}" if has_next else None,
        ),
    )


@router.callback_query(F.data == "ac:list")
async def order_cancel_list(cb: CallbackQuery, state: FSMContext, db: Database,
                            ui: Messenger, config: Config) -> None:
    await state.clear()
    await _cancel_list_screen(cb, db, ui, config, None)
    await cb.answer()


@router.callback_query(F.data.startswith("ac:page:"))
async def order_cancel_page(cb: CallbackQuery, state: FSMContext, db: Database,
                            ui: Messenger, config: Config) -> None:
    await state.clear()
    await _cancel_list_screen(cb, db, ui, config, cb_int(cb.data))
    await cb.answer()


@router.callback_query(F.data == "ac:find")
async def order_find_ask(cb: CallbackQuery, state: FSMContext, ui: Messenger) -> None:
    await state.set_state(FindOrderSG.number)
    await _window(cb, ui,
                  join("<b>Поиск заказа</b>",
                       "Введите номер заказа — например 412"),
                  kb.cancel_kb("ac:list"))
    await cb.answer()


@router.message(FindOrderSG.number, F.text)
async def order_find(message: Message, state: FSMContext, db: Database,
                     ui: Messenger, config: Config) -> None:
    await ui.drop_user_message(message)
    raw = message.text.strip().lstrip("№# ")
    order = None
    if raw.isdigit() and len(raw) <= 15:
        order = await db.get_order(int(raw))
    if not order:
        await ui.window(message.chat.id,
                        join("<b>Поиск заказа</b>",
                             "Заказ с таким номером не найден. Введите номер ещё раз"),
                        kb.cancel_kb("ac:list"))
        return
    await state.clear()
    await ui.window(message.chat.id, _order_card_text(order, config),
                    kb.confirm_kb(f"ac:ok:{order['id']}", "ac:list")
                    if order["cancelled_at"] is None else kb.to_vrheaven_menu_kb())


def _paid_warning(order) -> str:
    """Что по заказу уже выплачено и что с этим станет при отмене.

    Проведённые выплаты не пересчитываются (SPEC §5), но заканчивается это
    для сторон по-разному: выплаченное вознаграждение возвращается
    удержанием, выплаченная доля владельца остаётся у владельца. Названы
    обе половины — молчание об одной читалось бы как обещание вернуть обе.
    Сумм здесь нет: они стоят строкой выше, и повторять их незачем.
    """
    if order["admin_payout_id"] is None and order["owner_payout_id"] is None:
        return ""
    said = ["Внимание: доля по заказу уже входила в проведённую выплату"]
    if order["admin_payout_id"] is not None and order["admin_share"]:
        said.append("Вознаграждение вернётся удержанием из ближайшей "
                    "выплаты администратору")
    if order["owner_payout_id"] is not None and order["owner_share"]:
        said.append("Выплаченная доля владельца не пересчитывается")
    return lines(*said)


def _order_card_text(order, config: Config) -> str:
    """Карточка заказа перед отменой: факты, последствие, действие.

    Предупреждение о выплаченной доле стоит между фактами и строкой о
    том, что произойдёт: это условие решения, а не примечание под ним.
    """
    warn = _paid_warning(order)
    if order["cancelled_at"] is not None:
        warn = h("Заказ уже отменён {}",
                 fmt_dt(order["cancelled_at"], config.tz))
    return join(
        h("<b>Заказ №{}</b>", order["id"]),
        h("Администратор: {}\nВладелец: {}\nЗаказ: {} · {}\nДата: {}",
          order["admin_handle"], order["owner_handle"] or "—",
          order_row_label(order), fmt_money(order["price"]),
          fmt_dt(order["created_at"], config.tz)),
        # Три доли — три строки: деление заказа читают по вертикали, а
        # не выискивают суммы между точками в одной строке
        h("Вознаграждение: {}\nДоля владельца: {}\nОстаток VR Heaven: {}",
          fmt_money(order["admin_share"]), fmt_money(order["owner_share"]),
          fmt_money(round(order["price"] - order["admin_share"]
                          - order["owner_share"], 2))),
        warn,
        "Заказ будет исключён из расчёта долей" if order["cancelled_at"] is None else "",
    )


@router.callback_query(F.data.startswith("ac:o:"))
async def order_cancel_pick(cb: CallbackQuery, db: Database, ui: Messenger,
                            config: Config) -> None:
    order_id = cb_int(cb.data)
    order = await db.get_order(order_id) if order_id else None
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if order["cancelled_at"] is not None:
        await cb.answer("Заказ уже отменён", show_alert=True)
        return
    await _window(cb, ui, _order_card_text(order, config),
                  kb.confirm_kb(f"ac:ok:{order['id']}", "ac:list"))
    await cb.answer()


@router.callback_query(F.data.startswith("ac:ok:"))
async def order_cancel_confirm(cb: CallbackQuery, db: Database, ui: Messenger,
                               config: Config) -> None:
    order_id = cb_int(cb.data)
    order = await db.get_order(order_id) if order_id else None
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    cancelled_by = h("Заказ отменил VR Heaven\n{}", SUPPORT_LINE)
    date = fmt_dt(order["created_at"], config.tz, "%d.%m.%Y")
    notes: list[str] = []
    debt_line = ""
    async with db.write() as tx:
        done = await db.cancel_order(tx, order_id, for_self=False)
        if done:
            # Долг называется вслух везде, где говорят об отмене: молчание
            # о нём — то же самое исчезновение денег из учёта, только на
            # экране вместо базы
            if done.debt:
                debt_line = h(
                    "Вознаграждение {} за этот заказ уже было выплачено — оно "
                    "записано удержанием №{} и вычтется из ближайшей выплаты",
                    fmt_money(done.debt), done.bonus_id)
            await db.audit(tx, _actor(cb), "order.cancel", "order", order_id,
                           before={"cancelled": False},
                           after={"cancelled": True, "by": "vrheaven",
                                  "price": order["price"], "debt": done.debt,
                                  "bonus": done.bonus_id})
            admin = await db.get_user(order["admin_id"])
            if admin and admin["deleted_at"] is None:
                total = await db.admin_unpaid_total(admin["id"])
                # Снять можно только то, что ещё не выплачено: про уже
                # выплаченное вознаграждение говорит debt_line, и второй
                # строки о той же сумме быть не должно. У бесплатного
                # сеанса вознаграждения не было вовсе — снимать нечего
                removed = "" if done.debt or not order["admin_share"] else h(
                    "\nВознаграждение {} снято", fmt_money(order["admin_share"]))
                count = await notify.to_user(
                    db, tx, admin,
                    join("<b>Заказ отменён</b>",
                         h("Ваш заказ №{} от {} исключён из расчёта долей",
                           order_id, date)
                         + removed
                         + h("\nК выплате: {}",
                             fmt_money(total["due_sum"])),
                         debt_line,
                         cancelled_by),
                    kind="order_cancelled", dedup=f"cancel:{order_id}:admin")
                notes.append(notify.devices_note(count, "Администратор"))
            if order["owner_id"]:
                owner = await db.get_user(order["owner_id"])
                if owner and owner["deleted_at"] is None:
                    total = await db.owner_unpaid_total(owner["id"])
                    if not order["owner_share"]:
                        removed = ""      # доли по этому заказу не было вовсе
                    elif order["owner_payout_id"] is not None:
                        # Снять уже выплаченное нельзя, и владелец обязан
                        # услышать это словами, а не догадываться по цифре
                        removed = "\nВыплаченная доля не пересчитывается"
                    else:
                        removed = h("\nВаша доля {} снята",
                                    fmt_money(order["owner_share"]))
                    count = await notify.to_user(
                        db, tx, owner,
                        join("<b>Заказ отменён</b>",
                             h("Заказ №{} от {} исключён из расчёта долей",
                               order_id, date)
                             + removed
                             + h("\nК выплате: {}",
                                 fmt_money(total["share_sum"])),
                             cancelled_by),
                        kind="order_cancelled", dedup=f"cancel:{order_id}:owner")
                    notes.append(notify.devices_note(count, "Владелец"))
            await notify.to_super_admins(
                db, tx, config,
                join(h("<b>Заказ №{} отменён</b>", order_id),
                     h("Администратор: {}\nВладелец: {}\nЗаказ: {} · {}",
                       order["admin_handle"], order["owner_handle"] or "—",
                       order_row_label(order), fmt_money(order["price"])),
                     debt_line,
                     cancelled_by),
                kind="order_cancelled", dedup=f"cancel:{order_id}:vr",
                exclude_tg_id=cb.from_user.id)
    ui.wake()
    if not done:
        await _window(cb, ui, join(h("<b>Заказ №{}</b>", order_id),
                                   "Заказ уже был отменён"),
                      kb.to_vrheaven_menu_kb())
        await cb.answer()
        return
    await _window(
        cb, ui,
        join(h("<b>Заказ №{} отменён</b>", order_id),
             h("Сумма {} исключена из расчёта долей", fmt_money(order["price"])),
             debt_line,
             "\n".join(n for n in notes if n)),
        kb.to_vrheaven_menu_kb())
    await cb.answer("Заказ отменён")


# --------------------------------------------- Владельцы и администраторы

async def _user_card_text(db: Database, user) -> str:
    total = await db.unpaid_total(user)
    chats = await db.chats_for_user(user["id"])
    status = "действующий" if user["is_active"] else "приостановлен"
    cabinet = f"подключён · устройств: {len(chats)}" if chats else "не подключён"
    head = h("<b>{}</b> · {}\nИмя: {}\nКонтакт: {}",
             user["handle"], kb.ROLE_LABELS[user["role"]], user["name"],
             user["contact"] or "—")
    if user["role"] == "admin":
        owner_name = "—"
        if user["owner_id"]:
            owner = await db.get_user(user["owner_id"])
            if owner and owner["deleted_at"] is None:
                owner_name = owner["handle"]
        reset_note = "" if user["series_reset_min"] is None else " · индивидуально"
        head += h("\nВладелец: {}\nСбросы серии: {}{}", owner_name,
                  fmt_reset_times(normalize_reset(user["series_reset_min"])),
                  reset_note)
    else:
        head += h("\nДоля: {}", fmt_percent(user["percent"]))
    head += h("\nСтатус: {}\nКабинет: {}", status, cabinet)
    if user["role"] == "admin":
        # Бонусы уже сидят в due_sum, поэтому они — уточнение к итогу,
        # а не слагаемое после числа заказов, как читалось прежде
        bonus_line = ""
        if total["bonus_sum"]:
            bonus_line = h("\nв том числе бонусы: {}",
                           fmt_signed_money(total["bonus_sum"]))
        head += h("\nЗаказов в периоде: {}", total["orders_count"])
        head += h("\nК выплате: {}", fmt_money(total["due_sum"])) + bonus_line
    else:
        admins = await db.admins_of_owner(user["id"])
        names = ", ".join(a["handle"] for a in admins) if admins else "нет"
        head += h("\nАдминистраторы: {}", names)
        head += h("\nЗаказов в периоде: {}\nК выплате: {}",
                  total["orders_count"], fmt_money(total["share_sum"]))
    return head


async def _render_user_card(ui: Messenger, db: Database, chat_id: int, user,
                            prefix: str, head: str = "",
                            source_message_id: int | None = None) -> None:
    bonus_count = 0
    if user["role"] == "admin":
        bonus_count = len(await db.admin_unpaid_bonuses(user["id"]))
    text = join(head, await _user_card_text(db, user))
    await ui.window(chat_id, text, kb.user_card_kb(user, prefix, bonus_count),
                    source_message_id=source_message_id)


async def _get_managed_user(cb: CallbackQuery, db: Database):
    """Пользователь из callback-данных '{prefix}:{action}:{id}'.

    Роль обязана соответствовать префиксу раздела — устаревшие кнопки
    (пользователь удалён или id перезанят) дают алерт и None.
    """
    prefix = cb.data.split(":", 1)[0]
    user_id = cb_int(cb.data)
    user = await db.get_user(user_id) if user_id else None
    if not user or user["deleted_at"] is not None or user["role"] != PREFIX_ROLES[prefix]:
        await cb.answer(f"{kb.ROLE_LABELS[PREFIX_ROLES[prefix]].capitalize()} не найден",
                        show_alert=True)
        return None
    return user


def _section_menu_kb(prefix: str):
    return kb.owners_menu_kb() if prefix == "ow" else kb.admins_menu_kb()


@router.callback_query(F.data == "ow:menu")
async def owners_menu(cb: CallbackQuery, state: FSMContext, ui: Messenger) -> None:
    await state.clear()
    await _window(cb, ui,
                  join("<b>Владельцы</b>",
                       "Владельцы ПК-клубов получают долю с заказов своих "
                       "администраторов"),
                  kb.owners_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "ad:menu")
async def admins_menu(cb: CallbackQuery, state: FSMContext, ui: Messenger) -> None:
    await state.clear()
    await _window(cb, ui,
                  join("<b>Администраторы</b>",
                       "Администраторы клубов оформляют заказы и получают "
                       "вознаграждение по лесенке своей 12-часовой серии"),
                  kb.admins_menu_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("ow:list"))
@router.callback_query(F.data.startswith("ad:list"))
async def users_list(cb: CallbackQuery, state: FSMContext, db: Database,
                     ui: Messenger) -> None:
    await state.clear()
    prefix = cb.data.split(":", 1)[0]
    page = cb_int(cb.data) if cb.data.count(":") >= 2 else 0
    page = max(page or 0, 0)
    role = PREFIX_ROLES[prefix]
    users = await db.list_users(role)
    title = "Владельцы" if role == "owner" else "Администраторы"
    if not users:
        await _window(cb, ui, join(f"<b>{title}</b>", "Пока никто не подключён"),
                      _section_menu_kb(prefix))
        await cb.answer()
        return
    start = page * kb.PAGE_SIZE
    chunk = users[start:start + kb.PAGE_SIZE]
    if not chunk:
        page, chunk = 0, users[:kb.PAGE_SIZE]
    await _window(cb, ui,
                  join(f"<b>{title}</b>",
                       h("Всего: {}\nСтраница: {}", len(users), page + 1),
                       "Выберите учётную запись"),
                  kb.users_list_kb(chunk, prefix, page=page,
                                   has_next=start + kb.PAGE_SIZE < len(users)))
    await cb.answer()


@router.callback_query(F.data.startswith("ow:card:"))
@router.callback_query(F.data.startswith("ad:card:"))
async def user_card(cb: CallbackQuery, state: FSMContext, db: Database,
                    ui: Messenger) -> None:
    await state.clear()
    user = await _get_managed_user(cb, db)
    if not user:
        return
    await _render_user_card(ui, db, cb.message.chat.id, user,
                            cb.data.split(":", 1)[0],
                            source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data.startswith("ow:log:"))
@router.callback_query(F.data.startswith("ad:log:"))
async def user_log(cb: CallbackQuery, db: Database, ui: Messenger,
                   config: Config) -> None:
    """Последние действия по учётной записи — ответ на вопрос «кто это сделал»
    без выгрузки CSV."""
    user = await _get_managed_user(cb, db)
    if not user:
        return
    prefix = cb.data.split(":", 1)[0]
    rows = await db.audit_for_entity("user", user["id"], limit=8)
    if not rows:
        body = "Действий пока не было"
    else:
        body = "\n".join(
            h("{} · {} · {}", fmt_dt(r["at"], config.tz, "%d.%m %H:%M"),
              xp.ACTION_LABELS.get(r["action"], r["action"]),
              "VR Heaven" if r["actor_kind"] == "superadmin"
              else ("кабинет" if r["actor_kind"] == "staff" else "система"))
            for r in rows)
    await _window(cb, ui,
                  join(h("<b>История действий · {}</b>", user["handle"]), body),
                  kb.back_kb(f"{prefix}:card:{user['id']}", "К карточке"))
    await cb.answer()


@router.callback_query(F.data.startswith("ow:toggle:"))
@router.callback_query(F.data.startswith("ad:toggle:"))
async def user_toggle(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    user = await _get_managed_user(cb, db)
    if not user:
        return
    if not user["is_active"]:
        # Активация возможна, только если логин не занят другим действующим
        if not await db.handle_available(user["handle"], exclude_id=user["id"]):
            await cb.answer(
                "Логин закреплён за другой действующей учётной записью. "
                "Активация невозможна", show_alert=True)
            return
    new_state = not user["is_active"]
    async with db.write() as tx:
        await db.set_user_active(tx, user["id"], new_state)
        await db.audit(tx, _actor(cb),
                       "user.activate" if new_state else "user.suspend",
                       "user", user["id"],
                       before={"is_active": bool(user["is_active"])},
                       after={"is_active": new_state})
    user = await db.get_user(user["id"])
    await _render_user_card(ui, db, cb.message.chat.id, user,
                            cb.data.split(":", 1)[0],
                            source_message_id=cb.message.message_id)
    await cb.answer("Статус обновлён")


@router.callback_query(F.data.startswith("ow:pwd:"))
@router.callback_query(F.data.startswith("ad:pwd:"))
async def user_pwd_ask(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    user = await _get_managed_user(cb, db)
    if not user:
        return
    prefix = cb.data.split(":", 1)[0]
    await _window(
        cb, ui,
        join(h("<b>Смена пароля · {}</b>", user["handle"]),
             "Текущий пароль перестанет действовать, все подключённые "
             "устройства будут отключены от кабинета. Продолжить?"),
        kb.confirm_kb(f"{prefix}:pwdok:{user['id']}", f"{prefix}:card:{user['id']}"))
    await cb.answer()


@router.callback_query(F.data.startswith("ow:pwdok:"))
@router.callback_query(F.data.startswith("ad:pwdok:"))
async def user_pwd_regen(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    """Пароль показывается на отдельном экране с единственной кнопкой:
    случайное нажатие не должно уносить то, что видно один раз."""
    user = await _get_managed_user(cb, db)
    if not user:
        return
    prefix = cb.data.split(":", 1)[0]
    password = gen_password()
    password_hash = await hash_password_async(password)
    async with db.write() as tx:
        await db.set_user_password(tx, user["id"], password_hash)
        # Смена пароля = отзыв доступа: старые устройства входят заново
        await db.unbind_user_chats(tx, user["id"])
        await db.audit(tx, _actor(cb), "user.password", "user", user["id"],
                       after={"chats_unbound": True})
    await _window(
        cb, ui,
        join(h("<b>Пароль обновлён · {}</b>", user["handle"]),
             h("Логин: <code>{}</code>\nПароль: <code>{}</code>",
               user["handle"], password),
             "Передайте логин и пароль — пароль показывается только один раз.",
             "Все устройства отключены от кабинета: вход по новому паролю"),
        kb.password_kb(prefix, user["id"]))
    await cb.answer()


@router.callback_query(F.data.startswith("ow:pct:"))
async def user_pct_ask(cb: CallbackQuery, state: FSMContext, db: Database,
                       ui: Messenger) -> None:
    user = await _get_managed_user(cb, db)
    if not user:
        return
    await state.set_state(EditPercentSG.value)
    await state.update_data(user_id=user["id"], handle=user["handle"], prefix="ow")
    await _window(
        cb, ui,
        join(h("<b>Доля · {}</b>", user["handle"]),
             h("Текущая доля: {}", fmt_percent(user["percent"])),
             "Введите новую долю в процентах — от 0 включительно до 100 "
             "не включительно, например 12,5",
             "Изменение действует только на новые заказы"),
        kb.cancel_kb(f"ow:card:{user['id']}"))
    await cb.answer()


@router.message(EditPercentSG.value, F.text)
async def user_pct_set(message: Message, state: FSMContext, db: Database,
                       ui: Messenger) -> None:
    await ui.drop_user_message(message)
    data = await state.get_data()
    if "user_id" not in data:
        return  # дубль сообщения: параллельный хендлер уже завершил шаг
    percent = parse_percent(message.text)
    if percent is None:
        await ui.window(
            message.chat.id,
            join(h("<b>Доля · {}</b>", data["handle"]),
                 "Доля — число от 0 включительно до 100 не включительно, "
                 "например 0, 10 или 12,5",
                 "Введите долю ещё раз"),
            kb.cancel_kb(f"ow:card:{data['user_id']}"))
        return
    user = await db.get_user(data["user_id"])
    if not user or user["deleted_at"] is not None:
        await state.clear()
        await ui.window(message.chat.id,
                        join(h("<b>Доля · {}</b>", data["handle"]),
                             "Учётная запись не найдена — изменение не сохранено"),
                        _section_menu_kb(data["prefix"]))
        return
    await state.clear()
    async with db.write() as tx:
        await db.set_user_percent(tx, user["id"], percent)
        await db.audit(tx, _actor(message), "user.percent", "user", user["id"],
                       before={"percent": user["percent"]}, after={"percent": percent})
    user = await db.get_user(user["id"])
    await _render_user_card(ui, db, message.chat.id, user, data["prefix"],
                            head="<b>Доля обновлена</b>")


# ------------------------------------------------- Бонусы администраторам

def _bonus_terms(amount: float) -> dict[str, str]:
    """Слова для бонуса и для удержания — экраны у них общие, а род разный.

    Отрицательная сумма — удержание (SPEC §3а), и называть его бонусом
    на экране, где сумма уже видна со знаком, значит спорить с
    собственным числом.
    """
    if amount > 0:
        return {"noun": "Бонус", "genitive": "бонуса", "created": "начислен",
                "cancelled": "отменён", "excluded": "исключён"}
    return {"noun": "Удержание", "genitive": "удержания", "created": "записано",
            "cancelled": "отменено", "excluded": "исключено"}


async def _get_managed_admin(cb: CallbackQuery, db: Database):
    """Действующий (не удалённый) администратор из callback-данных."""
    user_id = cb_int(cb.data)
    user = await db.get_user(user_id) if user_id else None
    if not user or user["deleted_at"] is not None or user["role"] != "admin":
        await cb.answer("Администратор не найден", show_alert=True)
        return None
    return user


@router.callback_query(F.data.startswith("ad:bonus:"))
async def bonus_ask_amount(cb: CallbackQuery, state: FSMContext, db: Database,
                           ui: Messenger) -> None:
    user = await _get_managed_admin(cb, db)
    if not user:
        return
    await state.set_state(AddBonusSG.amount)
    await state.update_data(admin_id=user["id"], handle=user["handle"])
    await _window(
        cb, ui,
        join(h("<b>Бонус или удержание · {}</b>", user["handle"]),
             "Введите сумму в рублях — например 500.\n"
             "Отрицательная сумма — удержание, например −300",
             "Сумма учтётся в ближайшей выплате администратора"),
        kb.cancel_kb(f"ad:card:{user['id']}"))
    await cb.answer()


@router.message(AddBonusSG.amount, F.text)
async def bonus_amount_step(message: Message, state: FSMContext, db: Database,
                            ui: Messenger) -> None:
    await ui.drop_user_message(message)
    data = await state.get_data()
    if "admin_id" not in data:
        return
    amount = parse_amount(message.text, allow_negative=True)
    if amount is None:
        await ui.window(
            message.chat.id,
            join(h("<b>Бонус или удержание · {}</b>", data["handle"]),
                 "Сумма — число, не ноль: 500 или −300",
                 "Введите сумму ещё раз"),
            kb.cancel_kb(f"ad:card:{data['admin_id']}"))
        return
    await state.update_data(amount=amount)
    await state.set_state(AddBonusSG.comment)
    terms = _bonus_terms(amount)
    await ui.window(
        message.chat.id,
        join(h("<b>{} · {} · {}</b>", terms["noun"], data["handle"],
               fmt_signed_money(amount)),
             h("Введите комментарий — за что (до {} символов).\n"
               "Комментарий обязателен: администратор увидит его в "
               "уведомлении и в своей статистике", COMMENT_MAX)),
        kb.cancel_kb(f"ad:card:{data['admin_id']}"))


@router.message(AddBonusSG.comment, F.text)
async def bonus_comment_step(message: Message, state: FSMContext, db: Database,
                             ui: Messenger) -> None:
    await ui.drop_user_message(message)
    data = await state.get_data()
    if "amount" not in data:
        return
    comment = " ".join(message.text.split())
    terms = _bonus_terms(data["amount"])
    if not comment or len(comment) > COMMENT_MAX:
        await ui.window(
            message.chat.id,
            join(h("<b>{} · {} · {}</b>", terms["noun"], data["handle"],
                   fmt_signed_money(data["amount"])),
                 h("Комментарий обязателен и не длиннее {} символов",
                   COMMENT_MAX),
                 "Введите комментарий ещё раз"),
            kb.cancel_kb(f"ad:card:{data['admin_id']}"))
        return
    user = await db.get_user(data["admin_id"])
    if not user or user["deleted_at"] is not None:
        await state.clear()
        await ui.window(message.chat.id,
                        join(h("<b>{} · {}</b>", terms["noun"], data["handle"]),
                             h("Администратор не найден — {} не {}",
                               terms["noun"].lower(), terms["created"])),
                        kb.admins_menu_kb())
        return
    await state.clear()
    positive = data["amount"] > 0
    kind_word = "Вам начислен бонус" if positive else "Удержание из выплаты"
    effect = ("Войдёт в ближайшую выплату" if positive
              else "Вычтется из ближайшей выплаты")
    async with db.write() as tx:
        bonus_id = await db.create_bonus(tx, user["id"], data["amount"], comment)
        await db.audit(tx, _actor(message), "bonus.create", "bonus", bonus_id,
                       after={"admin": user["handle"], "amount": data["amount"],
                              "comment": comment})
        devices = await notify.to_user(
            db, tx, user,
            join(f"<b>{kind_word}</b>",
                 h("Сумма: {}\nЗа что: {}", fmt_signed_money(data["amount"]), comment),
                 effect),
            kind="bonus", dedup=f"bonus:{bonus_id}")
    ui.wake()
    await _render_user_card(
        ui, db, message.chat.id, user, "ad",
        head=join(h("<b>{} №{} {}</b>", terms["noun"], bonus_id, terms["created"]),
                  h("Сумма: {}\nЗа что: {}", fmt_signed_money(data["amount"]),
                    comment),
                  notify.devices_note(devices, "Администратор")),
    )


@router.callback_query(F.data.startswith("ad:blist:"))
async def bonus_list(cb: CallbackQuery, state: FSMContext, db: Database,
                     ui: Messenger, config: Config) -> None:
    await state.clear()
    user = await _get_managed_admin(cb, db)
    if not user:
        return
    bonuses = await db.admin_unpaid_bonuses(user["id"])
    if not bonuses:
        await _render_user_card(ui, db, cb.message.chat.id, user, "ad",
                                source_message_id=cb.message.message_id)
        await cb.answer("Невыплаченных бонусов и удержаний нет")
        return
    body = "\n".join(
        h("№{} · {} · {} · {}", b["id"], fmt_signed_money(b["amount"]),
          fmt_dt(b["created_at"], config.tz, "%d.%m.%Y"), b["comment"] or "—")
        for b in bonuses)
    await _window(cb, ui,
                  join(h("<b>Бонусы и удержания · {}</b>", user["handle"]), body,
                       "Нажмите строку, чтобы отменить её. "
                       "Выплаченные суммы не корректируются"),
                  kb.bonuses_kb(bonuses, user["id"]))
    await cb.answer()


@router.callback_query(F.data.startswith("ad:bdel:"))
async def bonus_cancel_ask(cb: CallbackQuery, db: Database, ui: Messenger,
                           config: Config) -> None:
    bonus_id = cb_int(cb.data)
    bonus = await db.get_bonus(bonus_id) if bonus_id else None
    if not bonus or bonus["cancelled_at"] is not None or bonus["payout_id"] is not None:
        await cb.answer("Уже выплачено или отменено", show_alert=True)
        return
    terms = _bonus_terms(bonus["amount"])
    await _window(
        cb, ui,
        join(h("<b>Отмена {} №{}</b>", terms["genitive"], bonus["id"]),
             h("Администратор: {}\nСумма: {}\nЗа что: {}\nЗаписано: {}",
               bonus["admin_handle"], fmt_signed_money(bonus["amount"]),
               bonus["comment"] or "—", fmt_dt(bonus["created_at"], config.tz)),
             h("{} будет {} из расчёта выплат", terms["noun"],
               terms["excluded"])),
        kb.confirm_kb(f"ad:bdelok:{bonus['id']}", f"ad:blist:{bonus['admin_id']}"))
    await cb.answer()


@router.callback_query(F.data.startswith("ad:bdelok:"))
async def bonus_cancel_confirm(cb: CallbackQuery, db: Database,
                               ui: Messenger) -> None:
    bonus_id = cb_int(cb.data)
    bonus = await db.get_bonus(bonus_id) if bonus_id else None
    if not bonus:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    user = await db.get_user(bonus["admin_id"])
    terms = _bonus_terms(bonus["amount"])
    devices = 0
    async with db.write() as tx:
        done = await db.cancel_bonus(tx, bonus["id"])
        if done:
            await db.audit(tx, _actor(cb), "bonus.cancel", "bonus", bonus["id"],
                           before={"amount": bonus["amount"]},
                           after={"cancelled": True})
            if user and user["deleted_at"] is None:
                devices = await notify.to_user(
                    db, tx, user,
                    join(h("<b>{} {}</b>", terms["noun"], terms["cancelled"]),
                         h("{} {} ({}) {} из расчёта выплат", terms["noun"],
                           fmt_signed_money(bonus["amount"]),
                           bonus["comment"] or "—", terms["excluded"]),
                         SUPPORT_LINE),
                    kind="bonus", dedup=f"bonus:{bonus['id']}:cancel")
    ui.wake()
    if not done:
        await cb.answer("Уже выплачено или отменено", show_alert=True)
        return
    head = h("<b>{} №{} {}</b>", terms["noun"], bonus["id"], terms["cancelled"])
    if user:
        await _render_user_card(
            ui, db, cb.message.chat.id, user, "ad",
            head=join(head,
                      h("Сумма {} исключена из расчёта выплат",
                        fmt_signed_money(bonus["amount"])),
                      notify.devices_note(devices, "Администратор")),
            source_message_id=cb.message.message_id)
    else:
        await _window(cb, ui, head, kb.admins_menu_kb())
    await cb.answer(h("{} {}", terms["noun"], terms["cancelled"]))


# ------------------------------------------------- Время сбросов серии

@router.callback_query(F.data.startswith("ad:reset:"))
async def series_reset_ask(cb: CallbackQuery, state: FSMContext, db: Database,
                           ui: Messenger) -> None:
    user = await _get_managed_admin(cb, db)
    if not user:
        return
    await state.set_state(EditResetSG.value)
    await state.update_data(admin_id=user["id"], handle=user["handle"])
    note = "по умолчанию" if user["series_reset_min"] is None else "задано индивидуально"
    await _window(
        cb, ui,
        join(h("<b>Сбросы серии · {}</b>", user["handle"]),
             h("Сейчас: {} ({})",
               fmt_reset_times(normalize_reset(user["series_reset_min"])), note),
             "Серия сбрасывается каждые 12 часов; с каждым сбросом "
             "вознаграждение снова считается с первой ступени.",
             "Введите время первого сброса — например 9:00; второй будет "
             "через 12 часов. Можно ввести оба: 9:00 21:00.\n"
             "Прочерк — вернуть значение по умолчанию (09:00 и 21:00)"),
        kb.cancel_kb(f"ad:card:{user['id']}"))
    await cb.answer()


@router.message(EditResetSG.value, F.text)
async def series_reset_set(message: Message, state: FSMContext, db: Database,
                           ui: Messenger) -> None:
    await ui.drop_user_message(message)
    data = await state.get_data()
    if "admin_id" not in data:
        return
    raw = message.text.strip()
    if raw in {"-", "—", "–", "−"}:
        reset_min = None
    else:
        reset_min = parse_series_reset(raw)
        if reset_min is None:
            await ui.window(
                message.chat.id,
                join(h("<b>Сбросы серии · {}</b>", data["handle"]),
                     "Введите одно время (например 9:00) или пару времён "
                     "с разницей ровно 12 часов (например 9:00 21:00).\n"
                     "Прочерк — значение по умолчанию"),
                kb.cancel_kb(f"ad:card:{data['admin_id']}"))
            return
    user = await db.get_user(data["admin_id"])
    if not user or user["deleted_at"] is not None:
        await state.clear()
        await ui.window(message.chat.id,
                        join(h("<b>Сбросы серии · {}</b>", data["handle"]),
                             "Администратор не найден — изменение не сохранено"),
                        kb.admins_menu_kb())
        return
    await state.clear()
    async with db.write() as tx:
        await db.set_user_series_reset(tx, user["id"], reset_min)
        await db.audit(tx, _actor(message), "user.series_reset", "user", user["id"],
                       before={"series_reset_min": user["series_reset_min"]},
                       after={"series_reset_min": reset_min})
    user = await db.get_user(user["id"])
    await _render_user_card(ui, db, message.chat.id, user, "ad",
                            head="<b>Сбросы серии обновлены</b>")


# ------------------------------------------------------------- Удаление

@router.callback_query(F.data.startswith("ow:del:"))
@router.callback_query(F.data.startswith("ad:del:"))
async def user_delete_ask(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    user = await _get_managed_user(cb, db)
    if not user:
        return
    prefix = cb.data.split(":", 1)[0]
    # Владельца с прикреплёнными администраторами удалить нельзя:
    # сначала админов перекрепляют или удаляют
    if user["role"] == "owner":
        admins = await db.admins_of_owner(user["id"])
        if admins:
            await _window(
                cb, ui,
                join(h("<b>Удаление · {}</b>", user["handle"]),
                     h("У владельца есть администраторы: {}.",
                       ", ".join(a["handle"] for a in admins)),
                     "Сначала перекрепите или удалите их — после этого "
                     "владельца можно будет удалить"),
                kb.back_kb(f"ow:card:{user['id']}", "К карточке"))
            await cb.answer()
            return
    total = await db.unpaid_total(user)
    warn = ""
    if round(total["due_sum"], 2) > 0:
        # Предупреждаем только о том, что действительно пропадёт.
        # Отрицательный итог никто и не собирался выплачивать: фраза
        # «к выплате числится −250 ₽» пугала суммой, которой нет
        warn = h("Внимание: к выплате числится {} — после удаления эта "
                 "сумма не будет выплачена", fmt_money(total["due_sum"]))
    await _window(
        cb, ui,
        join(h("<b>Удаление · {}</b>", user["handle"]),
             "Доступ в кабинет будет закрыт, логин освободится. "
             "История заказов и выплат сохранится в отчётах",
             warn),
        kb.confirm_kb(f"{prefix}:delok:{user['id']}", f"{prefix}:card:{user['id']}"))
    await cb.answer()


@router.callback_query(F.data.startswith("ow:delok:"))
@router.callback_query(F.data.startswith("ad:delok:"))
async def user_delete_confirm(cb: CallbackQuery, db: Database,
                              ui: Messenger) -> None:
    user = await _get_managed_user(cb, db)
    if not user:
        return
    prefix = cb.data.split(":", 1)[0]
    # Повторная проверка: администратора могли прикрепить, пока экран висел
    if user["role"] == "owner" and await db.admins_of_owner(user["id"]):
        await cb.answer(
            "У владельца есть прикреплённые администраторы — удаление недоступно",
            show_alert=True)
        return
    async with db.write() as tx:
        await db.delete_user(tx, user["id"])
        await db.audit(tx, _actor(cb), "user.delete", "user", user["id"],
                       before={"handle": user["handle"], "role": user["role"]},
                       after={"deleted": True})
    await _window(cb, ui,
                  join(h("<b>{} удалён</b>", user["handle"]),
                       "Логин доступен для повторного подключения"),
                  _section_menu_kb(prefix))
    await cb.answer("Учётная запись удалена")


# ------------------------------------------------- Добавление учётных записей

@router.callback_query(F.data.in_({"ow:add", "ad:add"}))
async def user_add_start(cb: CallbackQuery, state: FSMContext,
                         ui: Messenger) -> None:
    await state.clear()
    prefix = cb.data.split(":", 1)[0]
    await state.set_state(AddOwnerSG.handle if prefix == "ow" else AddAdminSG.handle)
    await _window(cb, ui,
                  join(f"<b>{ADD_TITLES[prefix]}</b>",
                       h("Введите логин — латинские буквы, цифры, дефис или "
                         "подчёркивание, от 2 до {} символов", HANDLE_MAX)),
                  kb.cancel_kb(f"{prefix}:menu"))
    await cb.answer()


async def _add_handle_step(message: Message, state: FSMContext, db: Database,
                           ui: Messenger, prefix: str, next_state: State) -> None:
    await ui.drop_user_message(message)
    title = ADD_TITLES[prefix]
    handle = message.text.strip().lower()
    if not HANDLE_RE.fullmatch(handle):
        await ui.window(
            message.chat.id,
            join(f"<b>{title}</b>",
                 h("Формат логина: латинские буквы, цифры, дефис, "
                   "подчёркивание, от 2 до {} символов", HANDLE_MAX),
                 "Введите логин ещё раз"),
            kb.cancel_kb(f"{prefix}:menu"))
        return
    if not await db.handle_available(handle):
        await ui.window(
            message.chat.id,
            join(f"<b>{title}</b>",
                 "Логин закреплён за действующей учётной записью",
                 "Укажите другой логин"),
            kb.cancel_kb(f"{prefix}:menu"))
        return
    await state.update_data(handle=handle)
    await state.set_state(next_state)
    await ui.window(message.chat.id,
                    join(h("<b>{} · {}</b>", title, handle), "Введите имя"),
                    kb.cancel_kb(f"{prefix}:menu"))


async def _add_name_step(message: Message, state: FSMContext, ui: Messenger,
                         prefix: str, next_state: State) -> None:
    await ui.drop_user_message(message)
    data = await state.get_data()
    if "handle" not in data:
        return
    title = ADD_TITLES[prefix]
    name = " ".join(message.text.split())
    if not name or len(name) > NAME_MAX:
        await ui.window(
            message.chat.id,
            join(h("<b>{} · {}</b>", title, data["handle"]),
                 h("Имя — непустая строка до {} символов", NAME_MAX),
                 "Введите имя ещё раз"),
            kb.cancel_kb(f"{prefix}:menu"))
        return
    await state.update_data(name=name)
    await state.set_state(next_state)
    await ui.window(
        message.chat.id,
        join(h("<b>{} · {}</b>", title, data["handle"]),
             h("Укажите контакт (телефон или Telegram), до {} символов.\n"
               "Если контакт не требуется — отправьте прочерк", CONTACT_MAX)),
        kb.cancel_kb(f"{prefix}:menu"))


def _normalize_contact(text: str) -> str | None:
    """Контакт или None, если он длиннее допустимого."""
    contact = " ".join(text.split())
    if contact in {"-", "—", "–", "−"}:
        return ""
    if len(contact) > CONTACT_MAX:
        return None
    return contact


def _credentials_screen(title: str, handle: str, password: str, card: str) -> str:
    return join(f"<b>{title}</b>",
                h("Логин: <code>{}</code>\nПароль: <code>{}</code>", handle, password),
                "Передайте логин и пароль — пароль показывается только один раз",
                card)


@router.message(AddOwnerSG.handle, F.text)
async def owner_add_handle(message: Message, state: FSMContext, db: Database,
                           ui: Messenger) -> None:
    await _add_handle_step(message, state, db, ui, "ow", AddOwnerSG.name)


@router.message(AddOwnerSG.name, F.text)
async def owner_add_name(message: Message, state: FSMContext,
                         ui: Messenger) -> None:
    await _add_name_step(message, state, ui, "ow", AddOwnerSG.contact)


@router.message(AddOwnerSG.contact, F.text)
async def owner_add_contact(message: Message, state: FSMContext, db: Database,
                            ui: Messenger) -> None:
    await ui.drop_user_message(message)
    data = await state.get_data()
    if "name" not in data:
        return
    contact = _normalize_contact(message.text)
    if contact is None:
        await ui.window(message.chat.id,
                        join(h("<b>Новый владелец · {}</b>", data["handle"]),
                             h("Контакт не длиннее {} символов", CONTACT_MAX),
                             "Введите контакт ещё раз"),
                        kb.cancel_kb("ow:menu"))
        return
    await state.clear()
    # Логин мог быть занят, пока шёл ввод — проверяем перед созданием
    if not await db.handle_available(data["handle"]):
        await ui.window(message.chat.id,
                        join("<b>Новый владелец</b>",
                             "Логин уже закреплён за действующей учётной "
                             "записью. Подключение отменено"),
                        kb.owners_menu_kb())
        return
    password = gen_password()
    password_hash = await hash_password_async(password)
    async with db.write() as tx:
        user_id = await db.create_user(tx, "owner", data["handle"], data["name"],
                                       contact, password_hash)
        await db.audit(tx, _actor(message), "user.create", "user", user_id,
                       after={"role": "owner", "handle": data["handle"]})
    user = await db.get_user(user_id)
    await ui.window(
        message.chat.id,
        _credentials_screen("Владелец подключён", user["handle"], password,
                            await _user_card_text(db, user)),
        kb.password_kb("ow", user_id))


@router.message(AddAdminSG.handle, F.text)
async def admin_add_handle(message: Message, state: FSMContext, db: Database,
                           ui: Messenger) -> None:
    await _add_handle_step(message, state, db, ui, "ad", AddAdminSG.name)


@router.message(AddAdminSG.name, F.text)
async def admin_add_name(message: Message, state: FSMContext,
                         ui: Messenger) -> None:
    await _add_name_step(message, state, ui, "ad", AddAdminSG.contact)


@router.message(AddAdminSG.contact, F.text)
async def admin_add_contact(message: Message, state: FSMContext, db: Database,
                            ui: Messenger) -> None:
    await ui.drop_user_message(message)
    data = await state.get_data()
    if "name" not in data:
        return
    contact = _normalize_contact(message.text)
    if contact is None:
        await ui.window(message.chat.id,
                        join(h("<b>Новый администратор · {}</b>", data["handle"]),
                             h("Контакт не длиннее {} символов", CONTACT_MAX),
                             "Введите контакт ещё раз"),
                        kb.cancel_kb("ad:menu"))
        return
    await state.update_data(contact=contact)
    await state.set_state(AddAdminSG.owner)
    owners = await db.list_users("owner", active_only=True)
    await ui.window(
        message.chat.id,
        join(h("<b>Новый администратор · {}</b>", data["handle"]),
             "Выберите владельца, к которому прикрепить администратора. "
             "Владелец получает долю с каждого его заказа"),
        kb.owner_pick_kb(owners, "ad:pickown", cancel_cb="ad:menu"))


@router.callback_query(AddAdminSG.owner, F.data.startswith("ad:pickown:"))
async def admin_add_pick_owner(cb: CallbackQuery, state: FSMContext,
                               db: Database, ui: Messenger) -> None:
    owner_id = cb_int(cb.data) or None
    owner = None
    if owner_id:
        owner = await db.get_user(owner_id)
        if not owner or owner["deleted_at"] is not None or not owner["is_active"]:
            await cb.answer("Владелец не найден", show_alert=True)
            return
    data = await state.get_data()
    if "handle" not in data:
        # Дубль нажатия: параллельный хендлер уже создал администратора;
        # окно не трогаем — на нём одноразовый пароль
        await cb.answer()
        return
    await state.clear()
    if not await db.handle_available(data["handle"]):
        await _window(cb, ui,
                      join("<b>Новый администратор</b>",
                           "Логин уже закреплён за действующей учётной "
                           "записью. Подключение отменено"),
                      kb.admins_menu_kb())
        await cb.answer()
        return
    password = gen_password()
    password_hash = await hash_password_async(password)
    async with db.write() as tx:
        user_id = await db.create_user(
            tx, "admin", data["handle"], data["name"], data["contact"],
            password_hash, owner_id=owner["id"] if owner else None)
        await db.audit(tx, _actor(cb), "user.create", "user", user_id,
                       after={"role": "admin", "handle": data["handle"],
                              "owner": owner["handle"] if owner else None})
    user = await db.get_user(user_id)
    await _window(cb, ui,
                  _credentials_screen("Администратор подключён", user["handle"],
                                      password, await _user_card_text(db, user)),
                  kb.password_kb("ad", user_id))
    await cb.answer()


# ------------------------------------------------------- Смена владельца

@router.callback_query(F.data.startswith("ad:own:"))
async def admin_owner_ask(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    user = await _get_managed_user(cb, db)
    if not user:
        return
    current = "—"
    if user["owner_id"]:
        owner = await db.get_user(user["owner_id"])
        if owner and owner["deleted_at"] is None:
            current = owner["handle"]
    owners = await db.list_users("owner", active_only=True)
    await _window(
        cb, ui,
        join(h("<b>Смена владельца · {}</b>", user["handle"]),
             h("Текущий владелец: {}", current),
             "Выбор действует только на новые заказы"),
        kb.owner_pick_kb(owners, f"ad:setown:{user['id']}",
                         cancel_cb=f"ad:card:{user['id']}"))
    await cb.answer()


@router.callback_query(F.data.startswith("ad:setown:"))
async def admin_owner_set(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    parsed = cb_ints(cb.data, 2)
    if parsed is None:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return
    admin_id, raw_owner_id = parsed
    user = await db.get_user(admin_id)
    if not user or user["deleted_at"] is not None or user["role"] != "admin":
        await cb.answer("Администратор не найден", show_alert=True)
        return
    owner_id = raw_owner_id or None
    owner = None
    if owner_id:
        owner = await db.get_user(owner_id)
        if not owner or owner["deleted_at"] is not None or not owner["is_active"]:
            await cb.answer("Владелец не найден", show_alert=True)
            return
    async with db.write() as tx:
        await db.set_admin_owner(tx, user["id"], owner_id)
        await db.audit(tx, _actor(cb), "user.owner", "user", user["id"],
                       before={"owner_id": user["owner_id"]},
                       after={"owner_id": owner_id,
                              "owner": owner["handle"] if owner else None})
    user = await db.get_user(user["id"])
    await _render_user_card(ui, db, cb.message.chat.id, user, "ad",
                            source_message_id=cb.message.message_id)
    await cb.answer("Владелец обновлён")


# ------------------------------------------------------------------- Сводка

@router.callback_query(F.data == "sum")
async def summary(cb: CallbackQuery, state: FSMContext, db: Database,
                  ui: Messenger) -> None:
    await state.clear()
    await _window(cb, ui, await reports.vrheaven_summary(db),
                  kb.to_vrheaven_menu_kb())
    await cb.answer()


# ------------------------------------------------------------------ Выплата

def _payout_refusal(total) -> str:
    """Почему выплата не состоялась — по текущему состоянию периода.

    Отрицательный итог — не «долей нет»: доли есть, и получатель обязан
    знать, что мешает выплате и когда она пройдёт. Одна формулировка на
    оба отказа — при выборе получателя и при подтверждении.
    """
    if round(total["due_sum"], 2) < 0:
        return h("Итог периода отрицательный: {}. Удержание переносится в "
                 "следующий период — выплата пройдёт, когда начисленного "
                 "станет не меньше удержанного",
                 fmt_signed_money(total["due_sum"]))
    return "У получателя уже нет долей к выплате"


@router.callback_query(F.data == "po:list")
async def payout_list(cb: CallbackQuery, state: FSMContext, db: Database,
                      ui: Messenger) -> None:
    await state.clear()
    # Получатель с нулевым итогом периода не платится; у администратора
    # итог включает бонусы
    entries = []
    for r in await db.owners_unpaid_summary():
        if db.is_payable(r):
            entries.append((r["id"], f"{r['handle']} · владелец"
                                     f" · {fmt_money(r['due_sum'])}"))
    for r in await db.admins_unpaid_summary():
        if db.is_payable(r):
            entries.append((r["id"], f"{r['handle']} · администратор"
                                     f" · {fmt_money(r['due_sum'])}"))
    if not entries:
        await _window(cb, ui, join("<b>Выплата</b>", "Невыплаченных долей нет"),
                      kb.to_vrheaven_menu_kb())
        await cb.answer()
        return
    await _window(cb, ui,
                  join("<b>Выплата</b>", "Выберите получателя"),
                  kb.payout_pick_kb(entries))
    await cb.answer()


@router.callback_query(F.data.startswith("po:p:"))
async def payout_pick(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    user_id = cb_int(cb.data)
    user = await db.get_user(user_id) if user_id else None
    if not user or user["deleted_at"] is not None:
        await cb.answer("Получатель не найден", show_alert=True)
        return
    total = await db.unpaid_total(user)
    if not db.is_payable(total):
        await _window(cb, ui, join("<b>Выплата</b>", _payout_refusal(total)),
                      kb.back_kb("po:list", "К списку"))
        await cb.answer()
        return
    bonus_line = ""
    if total["bonus_sum"]:
        bonus_line = h("\nДоля по заказам: {}\nБонусы и удержания: {}",
                       fmt_money(total["share_sum"]),
                       fmt_signed_money(total["bonus_sum"]))
    await _window(
        cb, ui,
        join("<b>Подтверждение выплаты</b>",
             h("Получатель: {} · {} · {}", user["handle"], user["name"],
               kb.ROLE_LABELS[user["role"]])
             + h("\nЗаказов в периоде: {}\nОборот: {}", total["orders_count"],
                 fmt_money(total["turnover"]))
             + bonus_line
             + h("\n<b>К выплате: {}</b>", fmt_money(total["due_sum"])),
             "Выплата закроет период получателя: доли и бонусы будут "
             "помечены выплаченными"),
        kb.confirm_kb(f"po:ok:{user['id']}", "po:list"))
    await cb.answer()


@router.callback_query(F.data.startswith("po:ok:"))
async def payout_confirm(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    user_id = cb_int(cb.data)
    user = await db.get_user(user_id) if user_id else None
    if not user or user["deleted_at"] is not None:
        await cb.answer("Получатель не найден", show_alert=True)
        return
    devices = 0
    result = None
    async with db.write() as tx:
        result = await db.create_payout(tx, user)
        if result is not None:
            payout_id, amount, orders_count, bonus_total = result
            await db.audit(tx, _actor(cb), "payout.create", "payout", payout_id,
                           after={"user": user["handle"], "amount": amount,
                                  "orders": orders_count, "bonuses": bonus_total})
            bonus_line = (h("\nБонусы и удержания: {}",
                            fmt_signed_money(bonus_total)) if bonus_total else "")
            devices = await notify.to_user(
                db, tx, user,
                join("<b>Выплата проведена</b>",
                     h("Сумма: {}\nЗаказов закрыто: {}",
                       fmt_money(amount), orders_count) + bonus_line,
                     "Открыт новый учётный период"),
                kind="payout", dedup=f"payout:{payout_id}")
    ui.wake()
    if result is None:
        # Экран подтверждения мог быть собран до удержания, пришедшего
        # секунду назад: выплата не состоялась, и сказать почему обязан
        # сегодняшний остаток, а не тот, что был на экране
        total = await db.unpaid_total(user)
        await _window(cb, ui, join("<b>Выплата</b>", _payout_refusal(total)),
                      kb.to_vrheaven_menu_kb())
        await cb.answer()
        return
    _, amount, orders_count, bonus_total = result
    bonus_line = (h("\nБонусы и удержания: {}", fmt_signed_money(bonus_total))
                  if bonus_total else "")
    await _window(
        cb, ui,
        join("<b>Выплата проведена</b>",
             h("Получатель: {} · {}\nСумма: {}\nЗаказов закрыто: {}",
               user["handle"], user["name"], fmt_money(amount), orders_count)
             + bonus_line,
             notify.devices_note(devices, "Получатель")),
        kb.to_vrheaven_menu_kb())
    await cb.answer("Выплата проведена")


# ------------------------------------------------------------------- Акции

PROMOS_TEXT = join(
    "<b>Акции</b>",
    "Акция — приз колеса фортуны, и вся акция — это её название. "
    "Администратор отмечает её в заказе после шлемов и длительности, "
    "на сеансах от 30 минут: так видно, какой приз за какой заказ выдан.",
    "Денег у акции нет. На цену сеанса, скидку, вознаграждение "
    "администратора, долю владельца и выплаты она не влияет ничем — "
    "это признак заказа для учёта и статистики.",
)


async def _promos_screen(ui: Messenger, db: Database, chat_id: int,
                         source_message_id: int | None = None) -> None:
    promos = await db.list_promos()
    text = join(PROMOS_TEXT,
                "Нажмите акцию, чтобы удалить её"
                if promos else "Действующих акций сейчас нет")
    await ui.window(chat_id, text, kb.promos_kb(promos),
                    source_message_id=source_message_id)


@router.callback_query(F.data == "pr:menu")
async def promos_menu(cb: CallbackQuery, state: FSMContext, db: Database,
                      ui: Messenger) -> None:
    await state.clear()
    await _promos_screen(ui, db, cb.message.chat.id, cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data.startswith("pr:open:"))
async def promo_open(cb: CallbackQuery, state: FSMContext, db: Database,
                     ui: Messenger) -> None:
    await state.clear()
    promo_id = cb_int(cb.data)
    promo = await db.get_promo(promo_id) if promo_id else None
    if not promo or promo["archived_at"] is not None:
        await cb.answer("Акция уже удалена", show_alert=True)
        return
    await _window(cb, ui,
                  join(h("<b>Акция · {}</b>", promo["name"]),
                       "Приз колеса фортуны. Никаких денег у акции нет: "
                       "на цену сеанса и на вознаграждение администратора "
                       "она не влияет"),
                  kb.promo_card_kb(promo))
    await cb.answer()


@router.callback_query(F.data == "pr:add")
async def promo_add_start(cb: CallbackQuery, state: FSMContext,
                          ui: Messenger) -> None:
    await state.clear()
    await state.set_state(AddPromoSG.name)
    await _window(cb, ui,
                  join("<b>Новая акция</b>",
                       h("Введите название — например 3=4 или День рождения "
                         "(до {} символов)", PROMO_NAME_MAX)),
                  kb.cancel_kb("pr:menu"))
    await cb.answer()


@router.message(AddPromoSG.name, F.text)
async def promo_add_name(message: Message, state: FSMContext, db: Database,
                         ui: Messenger) -> None:
    await ui.drop_user_message(message)
    name = " ".join(message.text.split())
    if not name or len(name) > PROMO_NAME_MAX:
        await ui.window(message.chat.id,
                        join("<b>Новая акция</b>",
                             h("Название — непустая строка до {} символов",
                               PROMO_NAME_MAX),
                             "Введите название ещё раз"),
                        kb.cancel_kb("pr:menu"))
        return
    if await db.promo_name_taken(name):
        await ui.window(message.chat.id,
                        join("<b>Новая акция</b>",
                             h("Акция «{}» уже существует", name),
                             "Укажите другое название"),
                        kb.cancel_kb("pr:menu"))
        return
    # Имя — вся акция целиком, поэтому она заводится прямо здесь:
    # спрашивать больше нечего
    await state.clear()
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, name)
        await db.audit(tx, _actor(message), "promo.create", "promo", promo_id,
                       after={"name": name})
    await _promos_screen(ui, db, message.chat.id)


@router.callback_query(F.data.startswith("pr:del:"))
async def promo_delete_ask(cb: CallbackQuery, state: FSMContext, db: Database,
                           ui: Messenger) -> None:
    await state.clear()
    promo_id = cb_int(cb.data)
    promo = await db.get_promo(promo_id) if promo_id else None
    if not promo or promo["archived_at"] is not None:
        await cb.answer("Акция уже удалена", show_alert=True)
        return
    await _window(cb, ui,
                  join(h("<b>Удаление акции · {}</b>", promo["name"]),
                       "Кнопка акции исчезнет у администраторов. Уже "
                       "оформленные заказы сохранятся во всех расчётах"),
                  kb.confirm_kb(f"pr:delok:{promo['id']}", f"pr:open:{promo['id']}"))
    await cb.answer()


@router.callback_query(F.data.startswith("pr:delok:"))
async def promo_delete_confirm(cb: CallbackQuery, db: Database,
                               ui: Messenger) -> None:
    promo_id = cb_int(cb.data)
    promo = await db.get_promo(promo_id) if promo_id else None
    if not promo:
        await cb.answer("Акция не найдена", show_alert=True)
        return
    async with db.write() as tx:
        done = await db.archive_promo(tx, promo["id"])
        if done:
            await db.audit(tx, _actor(cb), "promo.archive", "promo", promo["id"],
                           before={"name": promo["name"]},
                           after={"archived": True})
    if not done:
        await cb.answer("Акция уже удалена", show_alert=True)
        return
    await _promos_screen(ui, db, cb.message.chat.id, cb.message.message_id)
    await cb.answer(f"Акция «{promo['name']}» удалена")


# ---------------------------------------------------------------- Настройки

SETTINGS_TEXT = join(
    "<b>Настройки</b>",
    "Цены и параметры применяются только к новым заказам.",
    "Выберите раздел",
)


async def _settings_screen(ui: Messenger, db: Database, chat_id: int,
                           source_message_id: int | None = None) -> None:
    settings = await db.get_settings()
    await ui.window(chat_id, SETTINGS_TEXT,
                    kb.settings_menu_kb(settings, len(await db.list_promos())),
                    source_message_id=source_message_id)


@router.callback_query(F.data == "st:menu")
async def settings_menu(cb: CallbackQuery, state: FSMContext, db: Database,
                        ui: Messenger) -> None:
    await state.clear()
    await _settings_screen(ui, db, cb.message.chat.id, cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data == "st:prices")
async def settings_prices(cb: CallbackQuery, state: FSMContext, db: Database,
                          ui: Messenger) -> None:
    await state.clear()
    settings = await db.get_settings()
    await _window(cb, ui,
                  join("<b>Цены сеансов</b>",
                       "Итоговая цена стандартного сеанса округляется "
                       "до 10 ₽ — всегда, в том числе без скидки"),
                  kb.prices_kb(settings))
    await cb.answer()


async def _discount_screen(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    settings = await db.get_settings()
    note = ""
    if not int(settings["discount_days"]):
        note = "Ни один день не выбран — скидка не действует"
    elif not settings["discount_enabled"]:
        note = "Скидка сейчас выключена"
    await _window(cb, ui,
                  join("<b>Скидка</b>",
                       "Скидка применяется автоматически в выбранные дни "
                       "и часы. На акции скидка не действует никогда",
                       note),
                  kb.discount_kb(settings))


async def _free15_screen(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    settings = await db.get_settings()
    note = ("" if settings["free15_enabled"]
            else "Сейчас выключено — администраторам этот тип не показывается")
    await _window(cb, ui,
                  join(h("<b>{}</b>", FREE15_LABEL),
                       "Отдельный тип заказа: администратор выбирает его "
                       "первым шагом, шлемы, длительность и акция не "
                       "спрашиваются. Цена — 0 ₽, вознаграждение "
                       "администратора — 0 ₽, места в серии заказ не занимает",
                       note),
                  kb.free15_kb(settings))


@router.callback_query(F.data == "st:disc")
async def settings_discount(cb: CallbackQuery, state: FSMContext, db: Database,
                            ui: Messenger) -> None:
    await state.clear()
    await _discount_screen(cb, db, ui)
    await cb.answer()


@router.callback_query(F.data == "st:free")
async def settings_free15(cb: CallbackQuery, state: FSMContext, db: Database,
                          ui: Messenger) -> None:
    await state.clear()
    await _free15_screen(cb, db, ui)
    await cb.answer()


def _discount_days_text(settings: dict) -> str:
    lines = [h("Промежуток скидки: {}", kb.fmt_discount_time(settings)),
             h("Дни: {}", fmt_days(settings["discount_days"])),
             "Нажмите день, чтобы включить или выключить его"]
    if not int(settings["discount_days"]):
        lines.append("Ни один день не выбран — скидка не действует")
    elif not settings["discount_enabled"]:
        lines.append("Скидка сейчас выключена в настройках")
    return join("<b>Дни скидки</b>", *lines)


@router.callback_query(F.data == "st:days")
async def discount_days_menu(cb: CallbackQuery, state: FSMContext, db: Database,
                             ui: Messenger) -> None:
    await state.clear()
    settings = await db.get_settings()
    await _window(cb, ui, _discount_days_text(settings),
                  kb.discount_days_kb(settings))
    await cb.answer()


@router.callback_query(F.data.startswith("st:day:"))
async def discount_day_toggle(cb: CallbackQuery, state: FSMContext, db: Database,
                              ui: Messenger) -> None:
    day = cb_int(cb.data)
    if day is None or not 0 <= day < len(WEEKDAY_NAMES):
        await cb.answer("День не найден", show_alert=True)
        return
    await state.clear()
    settings = await db.get_settings()
    new_mask = toggle_day(settings["discount_days"], day)
    async with db.write() as tx:
        await db.set_setting(tx, "discount_days", new_mask)
        await db.audit(tx, _actor(cb), "setting.change", "setting", None,
                       before={"discount_days": int(settings["discount_days"])},
                       after={"discount_days": new_mask})
    settings = await db.get_settings()
    await _window(cb, ui, _discount_days_text(settings),
                  kb.discount_days_kb(settings))
    await cb.answer("Дни скидки обновлены")


@router.callback_query(F.data.startswith("st:tgl:"))
async def setting_toggle(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    key = cb_tail(cb.data, "st:tgl:")
    if key not in TOGGLE_KEYS:
        await cb.answer("Настройка не найдена", show_alert=True)
        return
    settings = await db.get_settings()
    value = 0 if settings[key] else 1
    async with db.write() as tx:
        await db.set_setting(tx, key, value)
        await db.audit(tx, _actor(cb), "setting.change", "setting", None,
                       before={key: settings[key]}, after={key: value})
    if TOGGLE_SCREENS[key] == "st:disc":
        await _discount_screen(cb, db, ui)
    else:
        await _free15_screen(cb, db, ui)
    await cb.answer("Настройка обновлена")


@router.callback_query(F.data.startswith("st:set:"))
async def setting_ask(cb: CallbackQuery, state: FSMContext, db: Database,
                      ui: Messenger) -> None:
    key = cb_tail(cb.data, "st:set:")
    if key not in SETTING_TITLES:
        await cb.answer("Настройка не найдена", show_alert=True)
        return
    settings = await db.get_settings()
    if key == "discount_time":
        current = kb.fmt_discount_time(settings)
        prompt = ("Введите промежуток в формате 10:00–16:00. "
                  "Промежуток через полночь не поддерживается: начало "
                  "должно быть раньше конца")
    elif key == "discount_percent":
        current = fmt_percent(settings[key])
        prompt = "Введите размер скидки в процентах — больше 0 и меньше 100"
    else:
        current = fmt_money(settings[key])
        prompt = "Введите цену в рублях"
    await state.set_state(EditSettingSG.value)
    await state.update_data(key=key)
    await _window(cb, ui,
                  join(h("<b>{}</b>", SETTING_TITLES[key]),
                       h("Текущее значение: {}", current), prompt),
                  kb.cancel_kb(SETTING_SCREENS.get(key, "st:prices")))
    await cb.answer()


async def _set_discount_time(message: Message, state: FSMContext, db: Database,
                             ui: Messenger) -> None:
    """Промежуток скидки одним вводом: обе границы меняются вместе,
    поэтому начало заведомо раньше конца."""
    parsed = parse_time_range(message.text)
    if parsed is None:
        await ui.window(
            message.chat.id,
            join(h("<b>{}</b>", SETTING_TITLES["discount_time"]),
                 "Введите промежуток в формате 10:00–16:00.",
                 "Начало должно быть раньше конца: промежуток через "
                 "полночь не поддерживается"),
            kb.cancel_kb("st:disc"))
        return
    start, end = parsed
    settings = await db.get_settings()
    await state.clear()
    async with db.write() as tx:
        await db.set_setting(tx, "discount_start_min", start)
        await db.set_setting(tx, "discount_end_min", end)
        await db.audit(tx, _actor(message), "setting.change", "setting", None,
                       before={"discount_start_min": settings["discount_start_min"],
                               "discount_end_min": settings["discount_end_min"]},
                       after={"discount_start_min": start, "discount_end_min": end})
    settings = await db.get_settings()
    await ui.window(message.chat.id,
                    join("<b>Скидка</b>", "Промежуток обновлён"),
                    kb.discount_kb(settings))


@router.message(EditSettingSG.value, F.text)
async def setting_set(message: Message, state: FSMContext, db: Database,
                      ui: Messenger) -> None:
    await ui.drop_user_message(message)
    data = await state.get_data()
    if "key" not in data:
        return
    key = data["key"]
    if key == "discount_time":
        await _set_discount_time(message, state, db, ui)
        return
    value = parse_amount(message.text)
    if value is None or (key == "discount_percent" and value >= 100):
        await ui.window(
            message.chat.id,
            join(h("<b>{}</b>", SETTING_TITLES[key]),
                 "Значение — положительное число"
                 + (" меньше 100" if key == "discount_percent" else ""),
                 "Введите значение ещё раз"),
            kb.cancel_kb(SETTING_SCREENS.get(key, "st:prices")))
        return
    settings = await db.get_settings()
    old_value = settings[key]
    if key.startswith("price_") and old_value > 0 and (
            value >= old_value * 3 or value <= old_value / 3):
        # Лишний ноль в цене переписал бы прайс всем клубам молча:
        # показываем «было → станет» и просим подтвердить
        await state.update_data(pending=value)
        await ui.window(
            message.chat.id,
            join(h("<b>{}</b>", SETTING_TITLES[key]),
                 h("Было: {}\nСтанет: {}", fmt_money(old_value), fmt_money(value)),
                 "Цена меняется более чем втрое. Подтвердите изменение"),
            kb.confirm_kb("st:apply", "st:prices"))
        return
    await state.clear()
    await _apply_setting(message, db, ui, key, old_value, value,
                         message.chat.id)


async def _apply_setting(event, db: Database, ui: Messenger, key: str,
                         old_value: float, value: float, chat_id: int) -> None:
    async with db.write() as tx:
        await db.set_setting(tx, key, value)
        await db.audit(tx, _actor(event), "setting.change", "setting", None,
                       before={key: old_value}, after={key: value})
    settings = await db.get_settings()
    if key.startswith("price_"):
        await ui.window(chat_id, join("<b>Цены сеансов</b>", "Цена обновлена"),
                        kb.prices_kb(settings))
    else:
        await ui.window(chat_id, join("<b>Скидка</b>", "Значение обновлено"),
                        kb.discount_kb(settings))


@router.callback_query(EditSettingSG.value, F.data == "st:apply")
async def setting_apply(cb: CallbackQuery, state: FSMContext, db: Database,
                        ui: Messenger) -> None:
    data = await state.get_data()
    key, value = data.get("key"), data.get("pending")
    if key not in SETTING_TITLES or value is None:
        await state.clear()
        await _settings_screen(ui, db, cb.message.chat.id, cb.message.message_id)
        await cb.answer(STALE_BUTTON)
        return
    settings = await db.get_settings()
    await state.clear()
    await _apply_setting(cb, db, ui, key, settings[key], value,
                         cb.message.chat.id)
    await cb.answer("Значение обновлено")


# ------------------------------------------------------- Доступ VR Heaven

@router.callback_query(F.data == "sa:menu")
async def super_admins_menu(cb: CallbackQuery, state: FSMContext, db: Database,
                            ui: Messenger, config: Config) -> None:
    await state.clear()
    rows = {row["tg_id"]: row for row in await db.list_super_admins()}
    entries = []
    for tg_id in await super_admin_ids(db, config):
        fixed = is_bootstrap(config, tg_id)
        label = rows[tg_id]["label"] if tg_id in rows and rows[tg_id]["label"] else ""
        suffix = " · запасной вход" if fixed else ""
        entries.append((tg_id, f"{tg_id}{' · ' + label if label else ''}{suffix}",
                        not fixed))
    await _window(
        cb, ui,
        join("<b>Доступ VR Heaven</b>",
             h("Супер-админов: {}", len(entries)),
             "Записи из настроек сервера («запасной вход») из бота убрать "
             "нельзя — это защита от потери доступа.",
             "Нажмите на запись, чтобы убрать её"),
        kb.super_admins_kb(entries))
    await cb.answer()


@router.callback_query(F.data == "sa:fixed")
async def super_admin_fixed(cb: CallbackQuery) -> None:
    await cb.answer("Это запасной вход из настроек сервера — "
                    "убрать его можно только на сервере", show_alert=True)


@router.callback_query(F.data == "sa:add")
async def super_admin_add_ask(cb: CallbackQuery, state: FSMContext,
                              ui: Messenger) -> None:
    await state.set_state(AddSuperAdminSG.tg_id)
    await _window(cb, ui,
                  join("<b>Новый супер-админ</b>",
                       "Введите Telegram ID — узнать его можно у @userinfobot",
                       "Супер-админ видит все заказы, суммы и настройки"),
                  kb.cancel_kb("sa:menu"))
    await cb.answer()


@router.message(AddSuperAdminSG.tg_id, F.text)
async def super_admin_add(message: Message, state: FSMContext, db: Database,
                          ui: Messenger, config: Config) -> None:
    await ui.drop_user_message(message)
    raw = message.text.strip()
    if not raw.lstrip("-").isdigit() or len(raw) > 18:
        await ui.window(message.chat.id,
                        join("<b>Новый супер-админ</b>",
                             "Telegram ID — это число, например 123456789",
                             "Введите ID ещё раз"),
                        kb.cancel_kb("sa:menu"))
        return
    tg_id = int(raw)
    await state.clear()
    if tg_id in await super_admin_ids(db, config):
        await ui.window(message.chat.id,
                        join("<b>Новый супер-админ</b>",
                             h("{} уже в списке", tg_id)),
                        kb.back_kb("sa:menu", "К списку"))
        return
    # Супер-админ не бывает владельцем или администратором (SPEC §12.8):
    # его чат уводится в панель VR Heaven, и кабинет перестанет
    # открываться, а уведомления кабинета продолжат приходить
    staff_user = await db.get_user_by_chat(tg_id)
    if staff_user is not None:
        await ui.window(
            message.chat.id,
            join("<b>Новый супер-админ</b>",
                 h("{} — это кабинет «{}» ({}). Супер-админ не может быть "
                   "владельцем или администратором",
                   tg_id, staff_user["handle"],
                   kb.ROLE_LABELS[staff_user["role"]]),
                 "Сначала отвяжите устройство сменой пароля кабинета "
                 "или укажите другой Telegram ID"),
            kb.back_kb("sa:menu", "К списку"))
        return
    async with db.write() as tx:
        await db.add_super_admin(tx, tg_id, "", message.from_user.id)
        await db.audit(tx, _actor(message), "superadmin.add", "superadmin", tg_id,
                       after={"tg_id": tg_id})
    await ui.window(message.chat.id,
                    join("<b>Супер-админ добавлен</b>",
                         h("{} получил полный доступ. Попросите его открыть "
                           "бота и нажать /start", tg_id)),
                    kb.back_kb("sa:menu", "К списку"))


@router.callback_query(F.data.startswith("sa:del:"))
async def super_admin_del_ask(cb: CallbackQuery, ui: Messenger,
                              config: Config) -> None:
    tg_id = cb_int(cb.data)
    if tg_id is None or is_bootstrap(config, tg_id):
        await cb.answer("Эту запись убрать нельзя", show_alert=True)
        return
    await _window(cb, ui,
                  join("<b>Убрать супер-админа</b>",
                       h("{} потеряет доступ к панели VR Heaven", tg_id),
                       "Продолжить?"),
                  kb.confirm_kb(f"sa:delok:{tg_id}", "sa:menu"))
    await cb.answer()


@router.callback_query(F.data.startswith("sa:delok:"))
async def super_admin_del(cb: CallbackQuery, state: FSMContext, db: Database,
                          ui: Messenger, config: Config) -> None:
    tg_id = cb_int(cb.data)
    if tg_id is None or is_bootstrap(config, tg_id):
        await cb.answer("Эту запись убрать нельзя", show_alert=True)
        return
    if tg_id == cb.from_user.id and len(await super_admin_ids(db, config)) <= 1:
        await cb.answer("Это последний доступ к панели — убрать его нельзя",
                        show_alert=True)
        return
    async with db.write() as tx:
        done = await db.remove_super_admin(tx, tg_id)
        if done:
            await db.audit(tx, _actor(cb), "superadmin.remove", "superadmin",
                           tg_id, before={"tg_id": tg_id}, after={"removed": True})
    await super_admins_menu(cb, state, db, ui, config)


# ------------------------------------------------------------------ Экспорт

@router.callback_query(F.data == "ex")
async def export_menu(cb: CallbackQuery, state: FSMContext, ui: Messenger) -> None:
    await state.clear()
    await _window(cb, ui,
                  join("<b>Экспорт данных</b>",
                       "Пять файлов CSV: учётные записи, заказы, выплаты, "
                       "бонусы и журнал действий.",
                       "Файлы приходят в чат и остаются в нём. Разделитель «;», "
                       "кодировка UTF-8 — Excel открывает без настройки"),
                  kb.export_kb())
    await cb.answer()


@router.callback_query(F.data == "ex:all")
async def export_csv(cb: CallbackQuery, state: FSMContext, db: Database,
                     ui: Messenger, config: Config) -> None:
    await state.clear()
    tz = config.tz
    files = [
        ("users.csv", xp.users_csv(await db.export_users(), tz)),
        ("orders.csv", xp.orders_csv(await db.export_orders(), tz)),
        ("payouts.csv", xp.payouts_csv(await db.export_payouts(), tz)),
        ("bonuses.csv", xp.bonuses_csv(await db.export_bonuses(), tz)),
        ("audit.csv", xp.audit_csv(await db.export_audit(), tz)),
    ]
    stamp = utcnow_iso().replace(":", "").replace("-", "")[:15]
    captions = {"users.csv": "Владельцы и администраторы", "orders.csv": "Заказы",
                "payouts.csv": "Выплаты", "bonuses.csv": "Бонусы администраторам",
                "audit.csv": "Журнал действий"}
    async with db.write() as tx:
        for name, data in files:
            await notify.to_chat(db, tx, cb.message.chat.id, captions[name],
                                 kind="export",
                                 document=document_payload(name, data),
                                 dedup=f"export:vr:{stamp}:{name}")
        await db.audit(tx, _actor(cb), "export.download", "setting", None,
                       after={"files": len(files)})
    ui.wake()
    await _window(cb, ui,
                  join("<b>Экспорт данных</b>", "Файлы придут в этот чат"),
                  kb.to_vrheaven_menu_kb())
    await cb.answer("Формирую выгрузку")


# ---------------------------------------- Сообщения и кнопки вне сценариев

@router.message()
async def cleanup_message(message: Message, state: FSMContext,
                          ui: Messenger) -> None:
    """Ввод вне сценария удаляется; вне FSM окно возвращается в меню."""
    await ui.drop_user_message(message)
    if await state.get_state() is None:
        await ui.window(message.chat.id, MENU_TEXT, kb.vrheaven_menu_kb())


@router.callback_query()
async def stale_callback(cb: CallbackQuery, state: FSMContext,
                         ui: Messenger) -> None:
    """Кнопка устаревшего экрана (например, после обновления бота)."""
    await state.clear()
    await _window(cb, ui, MENU_TEXT, kb.vrheaven_menu_kb())
    await cb.answer("Экран устарел — открыт текущий")
