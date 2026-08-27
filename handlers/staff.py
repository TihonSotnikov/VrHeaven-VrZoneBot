"""Кабинеты команды клуба: вход по логину и паролю, заказы, отчёты.

Роутер подключается после роутера VR Heaven: сюда попадают все, кто не
входит в список супер-админов. Роль (владелец или администратор)
определяется по учётной записи, привязанной к чату.

Интерфейс — одно Окно и постоянные Записи: экраны правятся на месте,
чеки и уведомления остаются в чате навсегда, ввод пользователя (включая
пароль) удаляется сразу.
"""

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import export as xp
import keyboards as kb
import notify
import reports
from config import Config
from db import Actor, Database
from errors import STALE_BUTTON, cb_int, cb_ints
from handlers.common import (
    SUPPORT,
    SUPPORT_ASK,
    SUPPORT_LINE,
    SUPPORT_TEXT,
    guide_bytes,
    guide_caption,
)
from markup import h, join, lines
from messaging import Messenger, document_payload
from pricing import (
    FREE15_LABEL,
    HEADSETS_CHOICES,
    KIND_FREE15,
    KIND_STANDARD,
    MINUTES_CHOICES,
    ladder_amount,
    normalize_reset,
    order_label,
    order_row_label,
    promo_applies,
    quote,
)
from utils import (
    fmt_dt,
    fmt_money,
    fmt_percent,
    utcnow_iso,
    verify_password_async,
)

log = logging.getLogger(__name__)

router = Router(name="staff")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

MAX_LOGIN_ATTEMPTS = 5
LOGIN_COOLDOWN_SECONDS = 300

# Администратор может отменить собственный заказ только в этом окне,
# позже — только VR Heaven через поддержку
CANCEL_WINDOW = timedelta(minutes=15)

# Пауза после серии неудачных попыток входа: перебирать пароль
# бессмысленно и без неё, но поток попыток занимал бы бота.
# Словарь ограничен сверху: писать боту может кто угодно, а запись
# с истёкшей паузой уже ничего не значит и выбрасывается.
_login_block: dict[int, tuple[int, float]] = {}
MAX_LOGIN_BLOCKS = 10_000


class LoginSG(StatesGroup):
    handle = State()
    password = State()


class NewOrderSG(StatesGroup):
    confirm = State()


WELCOME_TEXT = join(
    "<b>VR Heaven · Учёт VR-сеансов</b>",
    "Кабинет команды клуба: заказы, статистика, выплаты",
    "Для входа используйте логин и пароль, выданные VR Heaven",
)

ACCESS_CLOSED_TEXT = join(
    "<b>VR Heaven · Учёт VR-сеансов</b>",
    "Доступ к кабинету закрыт",
    SUPPORT_ASK,
)

HANDLE_PROMPT = join("<b>Вход в кабинет</b>", "Введите логин")
PASSWORD_PROMPT = join("<b>Вход в кабинет</b>", "Введите пароль")
BAD_CREDENTIALS = join(
    "<b>Вход в кабинет</b>",
    "Логин или пароль не подходят. Введите логин ещё раз",
)

SUSPENDED_LOGIN_TEXT = join(
    "<b>Вход в кабинет</b>",
    "Учётная запись приостановлена.",
    SUPPORT_ASK,
)

NEW_DEVICE_TEXT = join(
    "<b>Вход с нового устройства</b>",
    "В ваш кабинет выполнен вход с нового устройства.",
    f"Если это не вы — обратитесь в поддержку: {SUPPORT}",
)

SUSPENDED_TEXT = join(
    "<b>Новый заказ</b>",
    "Оформление заказов приостановлено.",
    SUPPORT_ASK,
)


def _menu_text(user, note: str | None = None) -> str:
    title = ("Кабинет владельца" if user["role"] == "owner"
             else "Кабинет администратора")
    head = h("<b>VR Heaven · {}</b>\n{} · {}", title, user["handle"], user["name"])
    if not user["is_active"]:
        head += h("\nСтатус: приостановлен · {}", SUPPORT)
    return join(head, note, "Выберите раздел")


def _menu_kb(user):
    if user["role"] == "owner":
        return kb.staff_owner_menu_kb()
    return kb.staff_admin_menu_kb()


# ------------------------------------------------------------- Авторизация

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database,
                    ui: Messenger) -> None:
    await state.clear()
    await ui.drop_user_message(message)
    user = await db.get_user_by_chat(message.chat.id)
    # fresh=True: /start обязан оставить в чате видимое сообщение — см.
    # Messenger.window. Правка прежнего Окна проходит и в очищенном чате,
    # где человеку её уже не увидеть
    if user:
        await ui.window(message.chat.id, _menu_text(user), _menu_kb(user),
                        fresh=True)
    else:
        await ui.window(message.chat.id, WELCOME_TEXT, kb.welcome_kb(), fresh=True)


@router.callback_query(F.data == "slogin")
async def login_start(cb: CallbackQuery, state: FSMContext, ui: Messenger) -> None:
    await state.clear()
    await state.set_state(LoginSG.handle)
    await ui.window(cb.message.chat.id, HANDLE_PROMPT, kb.login_cancel_kb(),
                    source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data == "scancel")
async def login_cancel(cb: CallbackQuery, state: FSMContext, ui: Messenger) -> None:
    await state.clear()
    await ui.window(cb.message.chat.id, WELCOME_TEXT, kb.welcome_kb(),
                    source_message_id=cb.message.message_id)
    await cb.answer()


@router.message(LoginSG.handle, F.text)
async def login_handle(message: Message, state: FSMContext, ui: Messenger) -> None:
    """Логин принимается без проверки существования.

    Ответ «логин не найден» на этом шаге превращал бота в справочник
    действующих учётных записей для любого, кто умеет писать боту.
    """
    await ui.drop_user_message(message)
    await state.update_data(handle=message.text.strip().lower()[:64], attempts=0)
    await state.set_state(LoginSG.password)
    await ui.window(message.chat.id, PASSWORD_PROMPT, kb.login_cancel_kb())


def _blocked_for(chat_id: int) -> int:
    failures, until = _login_block.get(chat_id, (0, 0.0))
    remaining = int(until - time.time())
    return remaining if failures >= MAX_LOGIN_ATTEMPTS and remaining > 0 else 0


def _note_login_failure(chat_id: int) -> None:
    failures, until = _login_block.get(chat_id, (0, 0.0))
    if until and until < time.time():
        failures = 0
    failures += 1
    if chat_id not in _login_block and len(_login_block) >= MAX_LOGIN_BLOCKS:
        _prune_login_blocks()
    _login_block[chat_id] = (
        failures,
        time.time() + LOGIN_COOLDOWN_SECONDS if failures >= MAX_LOGIN_ATTEMPTS else 0.0,
    )


def _prune_login_blocks() -> None:
    """Убирает записи, чья пауза уже истекла: они никого не держат."""
    now = time.time()
    for chat_id, (_, until) in list(_login_block.items()):
        if until <= now:
            del _login_block[chat_id]


@router.message(LoginSG.password, F.text)
async def login_password(message: Message, state: FSMContext, db: Database,
                         ui: Messenger) -> None:
    await ui.drop_user_message(message)
    chat_id = message.chat.id
    blocked = _blocked_for(chat_id)
    if blocked:
        await state.clear()
        await ui.window(chat_id, join(
            WELCOME_TEXT,
            h("Слишком много попыток входа. Повторите через {} мин "
              "или обратитесь в поддержку: {}", blocked // 60 + 1, SUPPORT),
        ), kb.welcome_kb())
        return
    data = await state.get_data()
    handle = data.get("handle")
    if not handle:
        await state.clear()
        await ui.window(chat_id, WELCOME_TEXT, kb.welcome_kb())
        return
    user = await db.get_user_by_handle(handle)
    suspended = None if user else await db.get_suspended_by_handle(handle)
    candidate = user or suspended
    password_ok = candidate is not None and await verify_password_async(
        message.text.strip(), candidate["password_hash"]
    )
    if not password_ok:
        _note_login_failure(chat_id)
        attempts = data.get("attempts", 0) + 1
        if attempts >= MAX_LOGIN_ATTEMPTS:
            await state.clear()
            await ui.window(chat_id, join(
                WELCOME_TEXT,
                h("Превышено число попыток входа. Попробуйте позднее "
                  "или обратитесь в поддержку: {}", SUPPORT),
            ), kb.welcome_kb())
            return
        await state.update_data(attempts=attempts)
        await state.set_state(LoginSG.handle)
        await ui.window(chat_id, BAD_CREDENTIALS, kb.login_cancel_kb())
        return
    _login_block.pop(chat_id, None)
    if user is None:
        # Пароль верен, но запись приостановлена: человек имеет право
        # знать настоящую причину, а не искать опечатку в логине
        await state.clear()
        await ui.window(chat_id, SUSPENDED_LOGIN_TEXT, kb.welcome_kb())
        return
    await state.clear()
    chats_before = await db.chats_for_user(user["id"])
    is_new_device = chat_id not in chats_before
    async with db.write() as tx:
        await db.bind_chat(tx, user["id"], chat_id)
        await db.audit(tx, Actor.staff(user, message.from_user.id), "user.login",
                       "user", user["id"], after={"chat_id": chat_id,
                                                  "new_device": is_new_device})
        if is_new_device:
            # Прежние устройства предупреждаются о новом входе —
            # единственная сигнализация при общем пароле
            for other in chats_before:
                await notify.to_chat(db, tx, other, NEW_DEVICE_TEXT,
                                     kind="security",
                                     dedup=f"newdevice:{user['id']}:{chat_id}")
    ui.wake()
    await ui.window(chat_id, _menu_text(
        user, "Вход выполнен. Уведомления будут приходить в этот чат"
    ), _menu_kb(user))


# ------------------------------------------------------------------ Кабинет

async def _require_user(cb: CallbackQuery, state: FSMContext, db: Database,
                        ui: Messenger):
    """Пользователь по chat_id. Если пользователь удалён или привязка
    потеряна — блокирует интерфейс и сбрасывает состояние до авторизации."""
    user = await db.get_user_by_chat(cb.message.chat.id)
    if user:
        return user
    await state.clear()
    await ui.window(cb.message.chat.id, ACCESS_CLOSED_TEXT, kb.welcome_kb(),
                    source_message_id=cb.message.message_id)
    await cb.answer("Доступ закрыт")
    return None


async def _require_role(cb: CallbackQuery, state: FSMContext, db: Database,
                        ui: Messenger, role: str):
    """Guard экранов одной роли; чужая кнопка возвращает в меню."""
    user = await _require_user(cb, state, db, ui)
    if not user:
        return None
    if user["role"] != role:
        await state.clear()
        await ui.window(cb.message.chat.id, _menu_text(user), _menu_kb(user),
                        source_message_id=cb.message.message_id)
        await cb.answer()
        return None
    return user


@router.callback_query(F.data == "sm")
async def staff_menu(cb: CallbackQuery, state: FSMContext, db: Database,
                     ui: Messenger) -> None:
    user = await _require_user(cb, state, db, ui)
    if not user:
        return
    await state.clear()
    await ui.window(cb.message.chat.id, _menu_text(user), _menu_kb(user),
                    source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data == "help")
async def support(cb: CallbackQuery, state: FSMContext, db: Database,
                  ui: Messenger) -> None:
    user = await _require_user(cb, state, db, ui)
    if not user:
        return
    await ui.window(cb.message.chat.id, SUPPORT_TEXT, kb.to_staff_menu_kb(),
                    source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data == "guide")
async def guide(cb: CallbackQuery, state: FSMContext, db: Database,
                ui: Messenger, config: Config) -> None:
    """Инструкция приходит файлом Записью — её можно перечитать в любой
    момент, не занимая экран."""
    user = await _require_user(cb, state, db, ui)
    if not user:
        return
    role = "owner" if user["role"] == "owner" else "admin"
    _, filename, data = guide_bytes(config, role)
    async with db.write() as tx:
        await notify.to_chat(db, tx, cb.message.chat.id, guide_caption(role),
                             kind="guide", document=document_payload(filename, data))
    ui.wake()
    await cb.answer("Инструкция отправлена в чат")


# -------------------------------------------------------------- Новый заказ

async def _require_active_admin(cb: CallbackQuery, state: FSMContext,
                                db: Database, ui: Messenger):
    """Оформлять заказы может только действующий администратор."""
    user = await _require_role(cb, state, db, ui, "admin")
    if not user:
        return None
    if not user["is_active"]:
        await state.clear()
        await ui.window(cb.message.chat.id, SUSPENDED_TEXT, kb.to_staff_menu_kb(),
                        source_message_id=cb.message.message_id)
        await cb.answer()
        return None
    return user


async def _headsets_screen(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    """Шаг «сколько шлемов»; «Назад» есть, только когда выше есть экран."""
    settings = await db.get_settings()
    await ui.window(
        cb.message.chat.id,
        join("<b>Новый заказ · стандартный сеанс</b>", "Сколько шлемов?"),
        kb.headsets_kb(with_back=bool(settings["free15_enabled"])),
        source_message_id=cb.message.message_id,
    )


@router.callback_query(F.data == "no:new")
async def order_new(cb: CallbackQuery, state: FSMContext, db: Database,
                    ui: Messenger) -> None:
    """Первый шаг заказа.

    Экран выбора типа показывается, только когда есть из чего выбирать:
    единственный тип — это лишнее нажатие на каждом заказе. Акции здесь
    больше не выбираются — акция принадлежит заказу и предлагается после
    шлемов и длительности.
    """
    user = await _require_active_admin(cb, state, db, ui)
    if not user:
        return
    await state.clear()
    settings = await db.get_settings()
    if not settings["free15_enabled"]:
        await _headsets_screen(cb, db, ui)
        await cb.answer()
        return
    await ui.window(cb.message.chat.id,
                    join("<b>Новый заказ</b>", "Выберите тип заказа"),
                    kb.order_kind_kb(),
                    source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data == "no:std")
async def order_std(cb: CallbackQuery, state: FSMContext, db: Database,
                    ui: Messenger) -> None:
    user = await _require_active_admin(cb, state, db, ui)
    if not user:
        return
    await state.clear()
    await _headsets_screen(cb, db, ui)
    await cb.answer()


@router.callback_query(F.data.startswith("no:h:"))
async def order_headsets(cb: CallbackQuery, state: FSMContext, db: Database,
                         ui: Messenger) -> None:
    user = await _require_active_admin(cb, state, db, ui)
    if not user:
        return
    headsets = cb_int(cb.data)
    if headsets not in HEADSETS_CHOICES:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return
    word = "шлем" if headsets == 1 else "шлема"
    await ui.window(cb.message.chat.id,
                    join(h("<b>Новый заказ · {} {}</b>", headsets, word),
                         "Выберите длительность"),
                    kb.duration_kb(headsets),
                    source_message_id=cb.message.message_id)
    await cb.answer()


def _order_confirm_text(q, headsets: int, minutes: int, promo) -> str:
    """Экран подтверждения стандартного сеанса: состав, цена, приз, оплата.

    Акция — приз колеса фортуны, а не скидка: к оплате идёт обычная цена
    сеанса, посчитанная так же, как без акции. Приз назван строкой
    **после** суммы к оплате и без единой цифры: денег у него нет, а
    у прилавка называют одну сумму.
    """
    parts = [h("<b>Новый заказ · {}</b>",
               order_label(KIND_STANDARD, headsets, minutes,
                           promo["name"] if promo else None))]
    if q.discount_percent:
        parts.append(h("Базовая цена: {}\nСкидка: −{} (применена автоматически)",
                       fmt_money(q.base_price), fmt_percent(q.discount_percent)))
    parts.append(h("<b>К оплате: {}</b>", fmt_money(q.price)))
    if promo:
        parts.append(h("Приз: {}\nНа цену сеанса приз не влияет",
                       promo["name"]))
    parts.append("Примите оплату наличными или переводом и подтвердите")
    return join(*parts)


async def _promo_screen(cb: CallbackQuery, db: Database, ui: Messenger,
                        promos, headsets: int, minutes: int) -> None:
    """Шаг выбора приза: сеанс уже собран и назван в заголовке.

    Правило названо прямо на экране выбора: приз записывается к заказу
    для учёта, а цену сеанса не меняет. Именно здесь его легче всего
    принять за скидку.
    """
    await ui.window(
        cb.message.chat.id,
        join(h("<b>Новый заказ · {}</b>",
               order_label(KIND_STANDARD, headsets, minutes)),
             "Выберите акцию — приз колеса фортуны — или продолжите без неё.",
             "На цену сеанса приз не влияет"),
        kb.promo_pick_kb(promos, headsets, minutes),
        source_message_id=cb.message.message_id)


async def _confirm_screen(cb: CallbackQuery, state: FSMContext, db: Database,
                          ui: Messenger, config: Config, *, headsets: int,
                          minutes: int, promo, back_cb: str) -> None:
    """Ставит сценарий на подтверждение и показывает экран оплаты.

    Цена считается от текущих настроек и фиксируется здесь: клиенту
    названа именно она (SPEC §2). Акция в расчёт цены не входит вовсе —
    это приз колеса фортуны, у которого нет денежной величины: в заказ от
    неё едут только `promo_id` и снимок названия, а `quote` об акциях не
    знает. Бесплатный сеанс — не акция, а отдельный тип (KIND_FREE15).
    """
    settings = await db.get_settings()
    q = quote(settings, datetime.now(config.tz), headsets, minutes)
    await state.set_state(NewOrderSG.confirm)
    await state.update_data(
        client_token=uuid.uuid4().hex,
        kind=KIND_STANDARD, headsets=headsets, minutes=minutes,
        promo_id=promo["id"] if promo else None,
        promo_name=promo["name"] if promo else None,
        base_price=q.base_price, discount_percent=q.discount_percent,
        price=q.price,
        quoted_at=utcnow_iso(),
    )
    await ui.window(cb.message.chat.id,
                    _order_confirm_text(q, headsets, minutes, promo),
                    kb.payment_kb(back_cb),
                    source_message_id=cb.message.message_id)


@router.callback_query(F.data.startswith("no:d:"))
async def order_duration(cb: CallbackQuery, state: FSMContext, db: Database,
                         ui: Messenger, config: Config) -> None:
    """Длительность выбрана — дальше акция, если она вообще применима.

    Акции предлагаются после шлемов и длительности и только к сеансам
    не короче 30 минут; когда предлагать нечего, шаг не показывается.
    """
    user = await _require_active_admin(cb, state, db, ui)
    if not user:
        return
    parsed = cb_ints(cb.data, 2)
    if parsed is None:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return
    headsets, minutes = parsed
    if headsets not in HEADSETS_CHOICES or minutes not in MINUTES_CHOICES:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return
    promos = await db.list_promos() if promo_applies(minutes) else []
    if promos:
        await state.clear()
        await _promo_screen(cb, db, ui, promos, headsets, minutes)
        await cb.answer()
        return
    await _confirm_screen(cb, state, db, ui, config, headsets=headsets,
                          minutes=minutes, promo=None,
                          back_cb=f"no:h:{headsets}")
    await cb.answer()


@router.callback_query(F.data.startswith("no:p:"))
async def order_promo(cb: CallbackQuery, state: FSMContext, db: Database,
                      ui: Messenger, config: Config) -> None:
    """Выбор акции к уже собранному сеансу; 0 — без акции."""
    user = await _require_active_admin(cb, state, db, ui)
    if not user:
        return
    parsed = cb_ints(cb.data, 3)
    if parsed is None:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return
    headsets, minutes, promo_id = parsed
    if (headsets not in HEADSETS_CHOICES or minutes not in MINUTES_CHOICES
            or not promo_applies(minutes)):
        await cb.answer(STALE_BUTTON, show_alert=True)
        return
    promo = None
    if promo_id:
        promo = await db.get_promo(promo_id)
        if not promo or promo["archived_at"] is not None:
            # Акцию удалили, пока экран висел: сеанс уже собран, поэтому
            # возвращаемся на тот же шаг, а не в начало заказа
            await state.clear()
            promos = await db.list_promos()
            if promos:
                await _promo_screen(cb, db, ui, promos, headsets, minutes)
            else:
                await _headsets_screen(cb, db, ui)
            await cb.answer("Акция недоступна", show_alert=True)
            return
    await _confirm_screen(cb, state, db, ui, config, headsets=headsets,
                          minutes=minutes, promo=promo,
                          back_cb=f"no:d:{headsets}:{minutes}")
    await cb.answer()


@router.callback_query(F.data == "no:free")
async def order_free15(cb: CallbackQuery, state: FSMContext, db: Database,
                       ui: Messenger) -> None:
    """Бесплатные 15 минут: ни шлемов, ни длительности, ни акции.

    Настройка перечитывается здесь, а не только при отрисовке экрана:
    выключенный тип не должен оформляться устаревшей кнопкой.
    """
    user = await _require_active_admin(cb, state, db, ui)
    if not user:
        return
    await state.clear()
    settings = await db.get_settings()
    if not settings["free15_enabled"]:
        await _headsets_screen(cb, db, ui)
        await cb.answer("Бесплатный сеанс сейчас недоступен", show_alert=True)
        return
    await state.set_state(NewOrderSG.confirm)
    await state.update_data(
        client_token=uuid.uuid4().hex,
        kind=KIND_FREE15, headsets=None, minutes=None,
        promo_id=None, promo_name=None,
        base_price=0.0, discount_percent=0, price=0.0,
        quoted_at=utcnow_iso(),
    )
    await ui.window(
        cb.message.chat.id,
        join(h("<b>Новый заказ · {}</b>", FREE15_LABEL),
             h("<b>К оплате: {}</b>", fmt_money(0)),
             "Деньги не принимаются: вознаграждение за этот заказ "
             "не начисляется и место в серии он не занимает",
             "Подтвердите оформление"),
        kb.payment_kb("no:new", confirm_text="Оформить заказ"),
        source_message_id=cb.message.message_id,
    )
    await cb.answer()


@router.callback_query(NewOrderSG.confirm, F.data == "no:drop")
async def order_drop(cb: CallbackQuery, state: FSMContext, db: Database,
                     ui: Messenger) -> None:
    """Явный отказ от заказа: «Отмена» на экране оплаты слишком похожа
    на «назад», а цена клиенту уже названа."""
    data = await state.get_data()
    price = data.get("price", 0)
    await state.clear()
    user = await db.get_user_by_chat(cb.message.chat.id)
    await ui.window(
        cb.message.chat.id,
        join("<b>Заказ не оформлен</b>",
             # Принял администратор деньги или нет — бот не знает и знать
             # не может; сказать он вправе только о собственной записи.
             # У бесплатного сеанса суммы нет — и строки о ней тоже
             h("Сумма {} не записана", fmt_money(price)) if price else "",
             "Выберите раздел"),
        _menu_kb(user) if user else kb.welcome_kb(),
        source_message_id=cb.message.message_id,
    )
    # Не «отменён»: отменяют записанный заказ, а этого не было вовсе —
    # экран и всплывающий ответ обязаны говорить об одном и том же
    await cb.answer("Заказ не оформлен")


def _reward_line(order) -> str:
    """Вознаграждение администратора для уведомления VR Heaven.

    У бесплатного сеанса места в лесенке нет, и «№None в серии» здесь
    было бы не оговоркой, а неправдой о расчёте.
    """
    if order["kind"] == KIND_FREE15:
        return h("Вознаграждение администратора: {} (бесплатный сеанс)",
                 fmt_money(order["admin_share"]))
    return h("Вознаграждение администратора (№{} в серии): {}",
             order["series_pos"], fmt_money(order["admin_share"]))


def _receipt_text(order, tz) -> str:
    """Чек заказа — Запись в чате администратора. Без вознаграждения:
    экран держат перед клиентом."""
    return h("<b>Заказ №{}</b>\n{} · {} · {}",
             order["id"], order_row_label(order), fmt_money(order["price"]),
             fmt_dt(order["created_at"], tz, "%d.%m %H:%M"))


@router.callback_query(NewOrderSG.confirm, F.data == "no:ok")
async def order_payment(cb: CallbackQuery, state: FSMContext, db: Database,
                        ui: Messenger, config: Config) -> None:
    data = await state.get_data()
    if "client_token" not in data:
        # Состояние потеряно (перезапуск на старой версии, чужая кнопка):
        # заказ не записываем и честно об этом говорим
        await state.clear()
        await _stale_screen(cb, db, ui)
        await cb.answer("Заказ не записан — оформите заново", show_alert=True)
        return
    user = await db.get_user_by_chat(cb.message.chat.id)
    if not user or user["role"] != "admin" or not user["is_active"]:
        await state.clear()
        await ui.window(cb.message.chat.id,
                        join("<b>Новый заказ</b>",
                             "Оформление недоступно — заказ не записан"),
                        kb.to_staff_menu_kb(),
                        source_message_id=cb.message.message_id)
        await cb.answer()
        return
    # Владелец фиксируется в заказе всегда: связь заказа с клубом — факт,
    # а право на долю — отдельный факт. Приостановленный владелец получает
    # долю 0 и пометку, но заказ остаётся в его истории (AUDIT D-15)
    owner = await db.get_user(user["owner_id"]) if user["owner_id"] else None
    owner_suspended = bool(owner) and (
        owner["deleted_at"] is not None or not owner["is_active"])
    since_iso, _ = reports.series_start_utc_iso(
        normalize_reset(user["series_reset_min"]), config.tz
    )
    async with db.write() as tx:
        order, created = await db.create_order(
            tx,
            client_token=data["client_token"],
            admin_id=user["id"], admin_percent=user["percent"],
            owner_id=owner["id"] if owner else None,
            owner_percent=owner["percent"] if owner else 0,
            owner_suspended=owner_suspended,
            kind=data["kind"], headsets=data["headsets"], minutes=data["minutes"],
            promo_id=data.get("promo_id"), promo_name=data.get("promo_name"),
            base_price=data["base_price"],
            discount_percent=data["discount_percent"], price=data["price"],
            quoted_at=data.get("quoted_at"), series_since_iso=since_iso,
        )
        owner_devices = 0
        if created:
            await db.audit(
                tx, Actor.staff(user, cb.from_user.id), "order.create", "order",
                order["id"],
                # Тип заказа записан рядом с деньгами: у бесплатного
                # сеанса нули и пустое место в серии — это норма, и
                # объясняет её именно он
                after={"kind": order["kind"], "price": order["price"],
                       "admin_share": order["admin_share"],
                       "owner_share": order["owner_share"],
                       "series_pos": order["series_pos"]},
            )
            await notify.to_user(db, tx, user, _receipt_text(order, config.tz),
                                 kind="order_receipt",
                                 dedup=f"order:{order['id']}:receipt")
            if owner and not owner_suspended:
                total = await db.owner_unpaid_total(owner["id"])
                owner_devices = await notify.to_user(
                    db, tx, owner,
                    join("<b>Новый заказ в вашем клубе</b>",
                         h("Ваша доля: {}\nК выплате: {}",
                           fmt_money(order["owner_share"]),
                           fmt_money(total["share_sum"])),
                         h("Администратор: {}\nЗаказ: {} · {}",
                           user["handle"], order_row_label(order),
                           fmt_money(order["price"]))),
                    kind="order_owner", dedup=f"order:{order['id']}:owner")
            await notify.to_super_admins(
                db, tx, config,
                join(h("<b>Новый заказ №{}</b>", order["id"]),
                     h("Администратор: {}\nВладелец: {}\nЗаказ: {} · {}",
                       user["handle"], order["owner_handle"] or "—",
                       order_row_label(order), fmt_money(order["price"])),
                     lines(
                         _reward_line(order),
                         h("Доля владельца ({}): {}",
                           fmt_percent(order["owner_percent"]),
                           fmt_money(order["owner_share"])),
                         h("Остаток VR Heaven: {}",
                           fmt_money(round(order["price"] - order["admin_share"]
                                           - order["owner_share"], 2))))),
                kind="order_vrheaven", dedup=f"order:{order['id']}:vr")
    await state.clear()
    ui.wake()
    note = (notify.devices_note(owner_devices, "Владелец")
            if created and owner and not owner_suspended else "")
    if order["kind"] == KIND_FREE15:
        earned = ("Вознаграждение за бесплатный сеанс не начисляется.\n"
                  "Место в серии он не занимает — следующий заказ идёт "
                  "по прежней ступени")
    else:
        earned = h("Ваше вознаграждение: {} (№{} в серии)\n"
                   "Следующий заказ серии: {}",
                   fmt_money(order["admin_share"]), order["series_pos"],
                   fmt_money(ladder_amount(order["series_pos"] + 1)))
    await ui.window(
        cb.message.chat.id,
        join(h("<b>Заказ №{} записан</b>", order["id"]),
             earned,
             "Отменить заказ можно в течение 15 минут",
             note),
        kb.order_done_kb(order["id"], cancellable=True),
        source_message_id=cb.message.message_id,
    )
    await cb.answer("Заказ оформлен" if created else "Заказ уже был записан")


# ------------------------------------------------- Отмена собственного заказа

def _removed_reward(order) -> str:
    """Строка «вознаграждение снято» — когда его было что снимать.

    У бесплатного сеанса вознаграждения не было вовсе, и «снято 0 ₽»
    противоречило бы экрану оформления. То же правило, что у доли
    владельца ниже.
    """
    return (h("Вознаграждение {} снято", fmt_money(order["admin_share"]))
            if order["admin_share"] else "")


def _cancel_blocked_text(order) -> str | None:
    """Причина, по которой администратор не может отменить заказ сам.

    None — отмена доступна. Выплаченная (любая) доля блокирует отмену
    даже внутри 15-минутного окна: проведённые выплаты не корректируются.
    """
    if order["admin_payout_id"] is not None or order["owner_payout_id"] is not None:
        return h("Доля по заказу уже включена в выплату. "
                 "Для отмены обратитесь в поддержку: {}", SUPPORT)
    created = datetime.fromisoformat(order["created_at"])
    if datetime.now(UTC) - created >= CANCEL_WINDOW:
        return h("Срок отмены истёк (15 минут). "
                 "Для отмены обратитесь в поддержку: {}", SUPPORT)
    return None


def _cancellable(order) -> bool:
    return order["cancelled_at"] is None and _cancel_blocked_text(order) is None


@router.callback_query(F.data == "sc:list")
async def cancel_list(cb: CallbackQuery, state: FSMContext, db: Database,
                      ui: Messenger, config: Config) -> None:
    user = await _require_role(cb, state, db, ui, "admin")
    if not user:
        return
    await state.clear()
    since = (datetime.now(UTC) - CANCEL_WINDOW).isoformat(timespec="seconds")
    orders = [o for o in await db.admin_recent_orders(user["id"], since)
              if _cancellable(o)]
    if not orders:
        await ui.window(
            cb.message.chat.id,
            join("<b>Отмена заказа</b>",
                 "Заказов, доступных к отмене, нет.",
                 h("Отменить заказ можно в течение 15 минут после оформления, "
                   "позже — через поддержку: {}", SUPPORT)),
            kb.to_staff_menu_kb(), source_message_id=cb.message.message_id)
        await cb.answer()
        return
    await ui.window(
        cb.message.chat.id,
        join("<b>Отмена заказа</b>",
             "Выберите заказ — отмена доступна в течение 15 минут "
             "после оформления"),
        kb.staff_orders_pick_kb(orders, config.tz),
        source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data.startswith("sc:o:"))
async def cancel_pick(cb: CallbackQuery, state: FSMContext, db: Database,
                      ui: Messenger, config: Config) -> None:
    user = await _require_role(cb, state, db, ui, "admin")
    if not user:
        return
    order_id = cb_int(cb.data)
    order = await db.get_order(order_id) if order_id else None
    if not order or order["admin_id"] != user["id"]:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if order["cancelled_at"] is not None:
        await cb.answer("Заказ уже отменён", show_alert=True)
        return
    blocked = _cancel_blocked_text(order)
    if blocked:
        await ui.window(cb.message.chat.id,
                        join(h("<b>Отмена заказа №{}</b>", order["id"]), blocked),
                        kb.to_staff_menu_kb(),
                        source_message_id=cb.message.message_id)
        await cb.answer()
        return
    await ui.window(
        cb.message.chat.id,
        join(h("<b>Отмена заказа №{}</b>", order["id"]),
             h("Заказ: {}\nВремя: {}\nВаше вознаграждение: {}",
               order_row_label(order),
               fmt_dt(order["created_at"], config.tz, "%d.%m %H:%M"),
               fmt_money(order["admin_share"])),
             "Заказ и вознаграждение будут исключены из расчёта долей"),
        kb.confirm_kb(f"sc:ok:{order['id']}", "sm"),
        source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data.startswith("sc:ok:"))
async def cancel_confirm(cb: CallbackQuery, state: FSMContext, db: Database,
                         ui: Messenger, config: Config) -> None:
    user = await _require_role(cb, state, db, ui, "admin")
    if not user:
        return
    order_id = cb_int(cb.data)
    order = await db.get_order(order_id) if order_id else None
    if not order or order["admin_id"] != user["id"]:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if order["cancelled_at"] is None:
        blocked = _cancel_blocked_text(order)
        if blocked:
            await ui.window(cb.message.chat.id,
                            join(h("<b>Отмена заказа №{}</b>", order_id), blocked),
                            kb.to_staff_menu_kb(),
                            source_message_id=cb.message.message_id)
            await cb.answer()
            return
    cancelled_by = h(
        "Заказ отменил администратор {}\nКонтакт администратора: {}\n{}",
        user["handle"], user["contact"] or "—", SUPPORT_LINE)
    owner_devices = 0
    own_devices = 0
    own_total = None
    async with db.write() as tx:
        # Условный UPDATE проверяет выплаты в самом запросе: выплата,
        # пришедшая в этот же миг, не может проскочить мимо запрета
        done = await db.cancel_order(tx, order_id, for_self=True)
        if done:
            await db.audit(tx, Actor.staff(user, cb.from_user.id), "order.cancel",
                           "order", order_id,
                           before={"cancelled": False},
                           after={"cancelled": True, "by": "admin",
                                  "price": order["price"]})
            # Чек заказа ушёл на все устройства кабинета, значит и отмена
            # обязана дойти до них: иначе на втором телефоне навсегда
            # остаётся чек заказа, которого больше нет. Инициатор видит
            # результат на своём Окне и второй раз о нём не читает
            own_total = await db.admin_unpaid_total(user["id"])
            own_devices = await notify.to_user(
                db, tx, user,
                join("<b>Заказ отменён</b>",
                     lines(h("Ваш заказ №{} исключён из расчёта долей", order_id),
                           _removed_reward(order),
                           h("К выплате: {}", fmt_money(own_total["due_sum"]))),
                     cancelled_by),
                kind="order_cancelled", dedup=f"cancel:{order_id}:admin",
                exclude_chat_id=cb.message.chat.id)
            if order["owner_id"]:
                owner = await db.get_user(order["owner_id"])
                if owner and owner["deleted_at"] is None:
                    total = await db.owner_unpaid_total(owner["id"])
                    # Доля 0 — заказ оформлен при приостановленном владельце
                    # (SPEC §7): снимать нечего, и говорить об этом незачем
                    removed = h("\nВаша доля {} снята",
                                fmt_money(order["owner_share"])
                                ) if order["owner_share"] else ""
                    owner_devices = await notify.to_user(
                        db, tx, owner,
                        join("<b>Заказ отменён</b>",
                             h("Заказ №{} исключён из расчёта долей", order_id)
                             + removed
                             + h("\nК выплате: {}",
                                 fmt_money(total["share_sum"])),
                             cancelled_by),
                        kind="order_cancelled",
                        dedup=f"cancel:{order_id}:owner")
            await notify.to_super_admins(
                db, tx, config,
                join(h("<b>Заказ №{} отменён</b>", order_id),
                     h("Администратор: {}\nВладелец: {}\nЗаказ: {} · {}",
                       order["admin_handle"], order["owner_handle"] or "—",
                       order_row_label(order), fmt_money(order["price"])),
                     cancelled_by),
                kind="order_cancelled", dedup=f"cancel:{order_id}:vr")
    ui.wake()
    if not done:
        await ui.window(cb.message.chat.id,
                        join(h("<b>Заказ №{}</b>", order_id),
                             "Заказ уже отменён или его доля вошла в выплату"),
                        kb.to_staff_menu_kb(),
                        source_message_id=cb.message.message_id)
        await cb.answer()
        return
    # Инициатор Записи об отмене не получает (SPEC §5) — значит, свою
    # новую цифру он обязан увидеть здесь: на других устройствах она уже
    # есть, а на этом экране её иначе не будет нигде
    await ui.window(
        cb.message.chat.id,
        join(h("<b>Заказ №{} отменён</b>", order_id),
             lines(_removed_reward(order),
                   h("К выплате: {}", fmt_money(own_total["due_sum"]))),
             notify.devices_note(owner_devices, "Владелец") if owner_devices else "",
             h("Ваши другие устройства уведомлены: {}", own_devices)
             if own_devices else ""),
        kb.to_staff_menu_kb(), source_message_id=cb.message.message_id)
    await cb.answer("Заказ отменён")


# --------------------------------------------------- Статистика и история

@router.callback_query(F.data == "stat")
async def my_stats(cb: CallbackQuery, state: FSMContext, db: Database,
                   ui: Messenger, config: Config) -> None:
    user = await _require_role(cb, state, db, ui, "admin")
    if not user:
        return
    report = await reports.admin_period(db, user, config.tz)
    await ui.window(cb.message.chat.id, report, kb.to_staff_menu_kb(),
                    source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data == "hist")
async def payout_history(cb: CallbackQuery, state: FSMContext, db: Database,
                         ui: Messenger, config: Config) -> None:
    user = await _require_user(cb, state, db, ui)
    if not user:
        return
    payouts = await db.payouts_for_user(user["id"])
    report = reports.payout_history(payouts, config.tz, user["handle"])
    await ui.window(cb.message.chat.id, report, kb.to_staff_menu_kb(),
                    source_message_id=cb.message.message_id)
    await cb.answer()


# ----------------------------------------------------------------- Владелец

@router.callback_query(F.data == "op:period")
async def owner_period(cb: CallbackQuery, state: FSMContext, db: Database,
                       ui: Messenger, config: Config) -> None:
    user = await _require_role(cb, state, db, ui, "owner")
    if not user:
        return
    report = await reports.owner_period(db, user, config.tz)
    await ui.window(cb.message.chat.id, report, kb.to_staff_menu_kb(),
                    source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data == "op:admins")
async def owner_admins(cb: CallbackQuery, state: FSMContext, db: Database,
                       ui: Messenger) -> None:
    user = await _require_role(cb, state, db, ui, "owner")
    if not user:
        return
    report = await reports.owner_admins(db, user)
    await ui.window(cb.message.chat.id, report, kb.to_staff_menu_kb(),
                    source_message_id=cb.message.message_id)
    await cb.answer()


@router.callback_query(F.data == "op:export")
async def owner_export(cb: CallbackQuery, state: FSMContext, db: Database,
                       ui: Messenger, config: Config) -> None:
    """Экспорт владельца: его администраторы, его заказы, его выплаты и
    журнал действий по его данным — тот же набор возможностей, что у
    VR Heaven, в границах его видимости."""
    user = await _require_role(cb, state, db, ui, "owner")
    if not user:
        return
    await state.clear()
    tz = config.tz
    files = [
        ("users.csv", xp.owner_users_csv(await db.export_users(owner_id=user["id"]), tz),
         "Мои администраторы"),
        ("orders.csv", xp.owner_orders_csv(await db.export_orders(owner_id=user["id"]), tz),
         "Мои заказы"),
        ("payouts.csv",
         xp.owner_payouts_csv(await db.export_payouts(user_id=user["id"]), tz),
         "Мои выплаты"),
        ("audit.csv", xp.owner_audit_csv(await db.export_owner_audit(user["id"]), tz),
         "Журнал действий по моим данным"),
    ]
    stamp = utcnow_iso().replace(":", "").replace("-", "")[:15]
    async with db.write() as tx:
        for name, data, caption in files:
            await notify.to_chat(
                db, tx, cb.message.chat.id, caption, kind="export",
                document=document_payload(f"{user['handle']}-{name}", data),
                dedup=f"export:{user['id']}:{stamp}:{name}",
            )
        await db.audit(tx, Actor.staff(user, cb.from_user.id), "export.download",
                       "user", user["id"], after={"files": len(files)})
    ui.wake()
    await ui.window(
        cb.message.chat.id,
        join("<b>Экспорт данных</b>",
             "Файлы придут в этот чат: администраторы, заказы, выплаты, "
             "журнал действий",
             "Разделитель «;», кодировка UTF-8 — Excel открывает без настройки"),
        kb.to_staff_menu_kb(), source_message_id=cb.message.message_id)
    await cb.answer("Формирую выгрузку")


# ---------------------------------------- Сообщения и кнопки вне сценариев

@router.message()
async def cleanup_message(message: Message, state: FSMContext, db: Database,
                          ui: Messenger) -> None:
    """Ввод вне сценария удаляется; вне FSM окно приводится к актуальному."""
    await ui.drop_user_message(message)
    if await state.get_state() is not None:
        return  # окно уже показывает текущий шаг сценария
    user = await db.get_user_by_chat(message.chat.id)
    if user:
        await ui.window(message.chat.id, _menu_text(user), _menu_kb(user))
    else:
        await ui.window(message.chat.id, WELCOME_TEXT, kb.welcome_kb())


async def _stale_screen(cb: CallbackQuery, db: Database, ui: Messenger) -> None:
    user = await db.get_user_by_chat(cb.message.chat.id)
    if user:
        await ui.window(cb.message.chat.id, _menu_text(user), _menu_kb(user),
                        source_message_id=cb.message.message_id)
    else:
        await ui.window(cb.message.chat.id, WELCOME_TEXT, kb.welcome_kb(),
                        source_message_id=cb.message.message_id)


@router.callback_query()
async def stale_callback(cb: CallbackQuery, state: FSMContext, db: Database,
                         ui: Messenger) -> None:
    """Кнопка устаревшего экрана: окно приводится к актуальному состоянию
    и человек узнаёт об этом словами, а не молчанием."""
    await state.clear()
    await _stale_screen(cb, db, ui)
    await cb.answer("Экран устарел — открыт текущий")
