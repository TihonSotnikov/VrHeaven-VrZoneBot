"""Inline-клавиатуры VR Heaven, владельца и администратора.

Клавиатура есть ровно у одного сообщения в чате — у Окна. Записи кнопок
не несут, поэтому промахнуться мимо живого экрана невозможно.

Порядок кнопок в меню — по частоте использования; необратимое действие
стоит последним. Списки листаются постранично: клавиатура Telegram
конечна, а число учётных записей — нет.
"""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pricing import WEEKDAY_NAMES, day_enabled, fmt_days, order_label, order_row_label
from utils import fmt_dt, fmt_money, fmt_percent, fmt_signed_money, fmt_time_min

# --------------------------------------------------------------------- Общие

CANCEL_CB = "cancel"
PAGE_SIZE = 8

ROLE_LABELS = {"owner": "владелец", "admin": "администратор"}

SUPPORT = "@VrHeaven"


def cancel_kb(cb: str = CANCEL_CB) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data=cb)
    return kb.as_markup()


def confirm_kb(yes_cb: str, cancel_cb: str = CANCEL_CB,
               yes_text: str = "Подтвердить") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=yes_text, callback_data=yes_cb)
    kb.button(text="Отмена", callback_data=cancel_cb)
    kb.adjust(1)
    return kb.as_markup()


def back_kb(cb: str, text: str = "Назад") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=cb)
    return kb.as_markup()


def _paging(kb: InlineKeyboardBuilder, *, prev_cb: str | None,
            next_cb: str | None, prev_text: str = "‹ Назад") -> int:
    """Кнопки листания; возвращает их число для раскладки."""
    count = 0
    if prev_cb:
        kb.button(text=prev_text, callback_data=prev_cb)
        count += 1
    if next_cb:
        kb.button(text="Дальше ›", callback_data=next_cb)
        count += 1
    return count


# ----------------------------------------------------------------- VR Heaven

def vrheaven_menu_kb() -> InlineKeyboardMarkup:
    """Частое — сверху, необратимое — последним."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Сводка", callback_data="sum")
    kb.button(text="Выплата", callback_data="po:list")
    kb.button(text="Администраторы", callback_data="ad:menu")
    kb.button(text="Владельцы", callback_data="ow:menu")
    kb.button(text="Настройки", callback_data="st:menu")
    kb.button(text="Экспорт данных", callback_data="ex")
    kb.button(text="Доступ VR Heaven", callback_data="sa:menu")
    kb.button(text="Как работает", callback_data="guide")
    kb.button(text="Отменить заказ", callback_data="ac:list")
    kb.adjust(1)
    return kb.as_markup()


def to_vrheaven_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="В меню", callback_data="am")
    return kb.as_markup()


def orders_pick_kb(orders, tz, *, prev_cb: str | None = None,
                   next_cb: str | None = None) -> InlineKeyboardMarkup:
    """Заказы для отмены VR Heaven; выплаченные (любой из долей) помечены."""
    kb = InlineKeyboardBuilder()
    for o in orders:
        paid = (" · выплачен" if o["admin_payout_id"] is not None
                or o["owner_payout_id"] is not None else "")
        kb.button(
            text=f"№{o['id']} {o['admin_handle']} · {fmt_money(o['price'])}"
                 f" · {fmt_dt(o['created_at'], tz, '%d.%m')}{paid}",
            callback_data=f"ac:o:{o['id']}",
        )
    # Листание по курсору идёт только вперёд, поэтому подпись честная:
    # кнопка возвращает к началу списка, а не на предыдущую страницу
    paging = _paging(kb, prev_cb=prev_cb, next_cb=next_cb,
                     prev_text="‹ В начало")
    kb.button(text="Найти заказ по номеру", callback_data="ac:find")
    kb.button(text="В меню", callback_data="am")
    kb.adjust(*([1] * len(orders) + ([paging] if paging else []) + [1, 1]))
    return kb.as_markup()


def owners_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Список владельцев", callback_data="ow:list")
    kb.button(text="Добавить владельца", callback_data="ow:add")
    kb.button(text="В меню", callback_data="am")
    kb.adjust(1)
    return kb.as_markup()


def admins_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Список администраторов", callback_data="ad:list")
    kb.button(text="Добавить администратора", callback_data="ad:add")
    kb.button(text="В меню", callback_data="am")
    kb.adjust(1)
    return kb.as_markup()


def users_list_kb(users, prefix: str, *, page: int = 0,
                  has_next: bool = False) -> InlineKeyboardMarkup:
    """Страница списка владельцев (prefix='ow') или администраторов ('ad')."""
    kb = InlineKeyboardBuilder()
    for u in users:
        status = "" if u["is_active"] else " · приостановлен"
        kb.button(
            text=f"{u['handle']} · {u['name']}{status}",
            callback_data=f"{prefix}:card:{u['id']}",
        )
    paging = _paging(
        kb,
        prev_cb=f"{prefix}:list:{page - 1}" if page > 0 else None,
        next_cb=f"{prefix}:list:{page + 1}" if has_next else None,
    )
    kb.button(text="Назад", callback_data=f"{prefix}:menu")
    kb.adjust(*([1] * len(users) + ([paging] if paging else []) + [1]))
    return kb.as_markup()


def user_card_kb(user, prefix: str, bonus_count: int = 0) -> InlineKeyboardMarkup:
    """Карточка учётной записи. Процент правится только владельцу
    (вознаграждение администратора считается лесенкой серии);
    администратору доступны бонусы и время сбросов серии."""
    kb = InlineKeyboardBuilder()
    if user["role"] == "admin":
        kb.button(text="Начислить бонус", callback_data=f"ad:bonus:{user['id']}")
        if bonus_count:
            kb.button(text=f"Бонусы к выплате ({bonus_count})",
                      callback_data=f"ad:blist:{user['id']}")
        kb.button(text="Сменить владельца", callback_data=f"ad:own:{user['id']}")
        kb.button(text="Сбросы серии", callback_data=f"ad:reset:{user['id']}")
    else:
        kb.button(text="Изменить долю", callback_data=f"ow:pct:{user['id']}")
    kb.button(text="Новый пароль", callback_data=f"{prefix}:pwd:{user['id']}")
    if user["is_active"]:
        kb.button(text="Приостановить", callback_data=f"{prefix}:toggle:{user['id']}")
    else:
        kb.button(text="Активировать", callback_data=f"{prefix}:toggle:{user['id']}")
    kb.button(text="История действий", callback_data=f"{prefix}:log:{user['id']}")
    kb.button(text="Удалить", callback_data=f"{prefix}:del:{user['id']}")
    kb.button(text="К списку", callback_data=f"{prefix}:list:0")
    kb.adjust(1)
    return kb.as_markup()


def password_kb(prefix: str, user_id: int) -> InlineKeyboardMarkup:
    """Экран одноразового пароля: единственная кнопка, чтобы случайное
    нажатие не унесло пароль до того, как его передали."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Пароль передан", callback_data=f"{prefix}:card:{user_id}")
    return kb.as_markup()


def owner_pick_kb(owners, cb_prefix: str,
                  cancel_cb: str = CANCEL_CB) -> InlineKeyboardMarkup:
    """Выбор владельца для администратора: '{cb_prefix}:{owner_id}',
    0 — без владельца (прямой администратор VR Heaven)."""
    kb = InlineKeyboardBuilder()
    for o in owners:
        kb.button(text=f"{o['handle']} · {o['name']}",
                  callback_data=f"{cb_prefix}:{o['id']}")
    kb.button(text="Без владельца", callback_data=f"{cb_prefix}:0")
    kb.button(text="Отмена", callback_data=cancel_cb)
    kb.adjust(1)
    return kb.as_markup()


def payout_pick_kb(entries) -> InlineKeyboardMarkup:
    """Получатели выплаты: entries — пары (user_id, текст кнопки)."""
    kb = InlineKeyboardBuilder()
    for user_id, text in entries:
        kb.button(text=text, callback_data=f"po:p:{user_id}")
    kb.button(text="В меню", callback_data="am")
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------- Настройки

def fmt_discount_time(settings: dict) -> str:
    """Промежуток скидки одной строкой: '10:00–16:00'."""
    return (f"{fmt_time_min(settings['discount_start_min'])}"
            f"–{fmt_time_min(settings['discount_end_min'])}")


def settings_menu_kb(settings: dict, promo_count: int) -> InlineKeyboardMarkup:
    """Четыре раздела, каждый — отдельное решение бизнеса, а не полка
    для однотипных кнопок: сколько стоит сеанс, когда дешевле, что идёт
    по своей цене и что получает клиент сверх сеанса."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Цены сеансов", callback_data="st:prices")
    kb.button(
        text=f"Скидка — {'вкл' if settings['discount_enabled'] else 'выкл'}"
             f" · {fmt_percent(settings['discount_percent'])}"
             f" · {fmt_discount_time(settings)}",
        callback_data="st:disc",
    )
    kb.button(text=f"Акции ({promo_count})", callback_data="pr:menu")
    kb.button(
        text=f"ПК-бонус — {'вкл' if settings['pc_bonus_enabled'] else 'выкл'}"
             f" · {int(settings['pc_bonus_points'])} баллов",
        callback_data="st:pc",
    )
    kb.button(text="В меню", callback_data="am")
    kb.adjust(1)
    return kb.as_markup()


def prices_kb(settings: dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for headsets in (1, 2):
        for minutes in (15, 30, 60):
            key = f"price_{headsets}_{minutes}"
            kb.button(
                text=f"{order_label('standard', headsets, minutes)} — "
                     f"{fmt_money(settings[key])}",
                callback_data=f"st:set:{key}",
            )
    kb.button(text="Назад", callback_data="st:menu")
    kb.adjust(1)
    return kb.as_markup()


def discount_kb(settings: dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"Скидка — {'вкл' if settings['discount_enabled'] else 'выкл'}",
        callback_data="st:tgl:discount_enabled",
    )
    kb.button(text=f"Размер — {fmt_percent(settings['discount_percent'])}",
              callback_data="st:set:discount_percent")
    kb.button(text=f"Время — {fmt_discount_time(settings)}",
              callback_data="st:set:discount_time")
    kb.button(text=f"Дни — {fmt_days(settings['discount_days'])}",
              callback_data="st:days")
    kb.button(text="Назад", callback_data="st:menu")
    kb.adjust(1)
    return kb.as_markup()


def pc_bonus_kb(settings: dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"ПК-бонус — {'вкл' if settings['pc_bonus_enabled'] else 'выкл'}",
        callback_data="st:tgl:pc_bonus_enabled",
    )
    kb.button(text=f"Баллы — {int(settings['pc_bonus_points'])}",
              callback_data="st:set:pc_bonus_points")
    kb.button(text="Назад", callback_data="st:menu")
    kb.adjust(1)
    return kb.as_markup()


def discount_days_kb(settings: dict) -> InlineKeyboardMarkup:
    """Дни недели скидки: нажатие на день включает или выключает его."""
    kb = InlineKeyboardBuilder()
    for weekday, name in enumerate(WEEKDAY_NAMES):
        mark = "✅" if day_enabled(settings["discount_days"], weekday) else "◻️"
        kb.button(text=f"{mark} {name}", callback_data=f"st:day:{weekday}")
    kb.button(text="Назад", callback_data="st:disc")
    kb.adjust(4, 3, 1)
    return kb.as_markup()


def promos_kb(promos) -> InlineKeyboardMarkup:
    """Нажатие на акцию открывает её экран — цену можно изменить,
    а удаление остаётся отдельным явным действием."""
    kb = InlineKeyboardBuilder()
    for p in promos:
        kb.button(text=f"{p['name']} — {fmt_money(p['price'])}",
                  callback_data=f"pr:open:{p['id']}")
    kb.button(text="Добавить акцию", callback_data="pr:add")
    kb.button(text="Назад", callback_data="st:menu")
    kb.adjust(1)
    return kb.as_markup()


def promo_card_kb(promo) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Изменить цену", callback_data=f"pr:price:{promo['id']}")
    kb.button(text="Удалить акцию", callback_data=f"pr:del:{promo['id']}")
    kb.button(text="К списку акций", callback_data="pr:menu")
    kb.adjust(1)
    return kb.as_markup()


def bonuses_kb(bonuses, admin_id: int) -> InlineKeyboardMarkup:
    """Невыплаченные бонусы администратора: нажатие открывает отмену."""
    kb = InlineKeyboardBuilder()
    for b in bonuses:
        kb.button(text=f"№{b['id']} · {fmt_signed_money(b['amount'])} — отменить",
                  callback_data=f"ad:bdel:{b['id']}")
    kb.button(text="К карточке", callback_data=f"ad:card:{admin_id}")
    kb.adjust(1)
    return kb.as_markup()


# ------------------------------------------------------- Доступ VR Heaven

def super_admins_kb(entries) -> InlineKeyboardMarkup:
    """entries — (tg_id, подпись, можно ли убрать)."""
    kb = InlineKeyboardBuilder()
    for tg_id, text, removable in entries:
        kb.button(text=text,
                  callback_data=f"sa:del:{tg_id}" if removable else "sa:fixed")
    kb.button(text="Добавить супер-админа", callback_data="sa:add")
    kb.button(text="В меню", callback_data="am")
    kb.adjust(1)
    return kb.as_markup()


# --------------------------------------------------------------- Экспорт

def export_kb() -> InlineKeyboardMarkup:
    """Экран экспорта VR Heaven. У владельца выгрузка живёт на своём
    экране (`op:export`) и этой клавиатурой не пользуется."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Выгрузить всё", callback_data="ex:all")
    kb.button(text="В меню", callback_data="am")
    kb.adjust(1)
    return kb.as_markup()


# ------------------------------------------------------------------- Команда

def staff_admin_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Новый заказ", callback_data="no:new")
    kb.button(text="Моя статистика", callback_data="stat")
    kb.button(text="История выплат", callback_data="hist")
    kb.button(text="Как пользоваться", callback_data="guide")
    kb.button(text="Поддержка", callback_data="help")
    kb.button(text="Отменить заказ", callback_data="sc:list")
    kb.adjust(1)
    return kb.as_markup()


def staff_owner_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Текущий период", callback_data="op:period")
    kb.button(text="Мои администраторы", callback_data="op:admins")
    kb.button(text="История выплат", callback_data="hist")
    kb.button(text="Экспорт данных", callback_data="op:export")
    kb.button(text="Как пользоваться", callback_data="guide")
    kb.button(text="Поддержка", callback_data="help")
    kb.adjust(1)
    return kb.as_markup()


def to_staff_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="В меню", callback_data="sm")
    return kb.as_markup()


def order_kind_kb(promos) -> InlineKeyboardMarkup:
    """Выбор типа заказа: стандартный сеанс и действующие акции."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Стандартный сеанс", callback_data="no:std")
    for p in promos:
        kb.button(text=f"Акция · {p['name']} — {fmt_money(p['price'])}",
                  callback_data=f"no:promo:{p['id']}")
    kb.button(text="Отмена", callback_data="sm")
    kb.adjust(1)
    return kb.as_markup()


def headsets_kb(*, with_back: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="1 шлем", callback_data="no:h:1")
    kb.button(text="2 шлема", callback_data="no:h:2")
    if with_back:
        kb.button(text="Назад", callback_data="no:new")
    kb.button(text="Отмена", callback_data="sm")
    kb.adjust(1)
    return kb.as_markup()


def duration_kb(headsets: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for minutes in (15, 30, 60):
        kb.button(text=f"{minutes} мин", callback_data=f"no:d:{headsets}:{minutes}")
    kb.button(text="Назад", callback_data="no:std")
    kb.button(text="Отмена", callback_data="sm")
    kb.adjust(1)
    return kb.as_markup()


def payment_kb(back_cb: str) -> InlineKeyboardMarkup:
    """Момент приёма денег: подтверждение и явный отказ от заказа."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Оплата получена", callback_data="no:ok")
    kb.button(text="Назад", callback_data=back_cb)
    kb.button(text="Отменить заказ", callback_data="no:drop")
    kb.adjust(1)
    return kb.as_markup()


def order_done_kb(order_id: int, *, cancellable: bool) -> InlineKeyboardMarkup:
    """Экран после заказа: отмена — одна кнопка, а не поиск в списке."""
    kb = InlineKeyboardBuilder()
    if cancellable:
        kb.button(text=f"Отменить заказ №{order_id}", callback_data=f"sc:o:{order_id}")
    kb.button(text="Новый заказ", callback_data="no:new")
    kb.button(text="В меню", callback_data="sm")
    kb.adjust(1)
    return kb.as_markup()


def staff_orders_pick_kb(orders, tz) -> InlineKeyboardMarkup:
    """Заказы администратора, доступные к отмене (в пределах 15 минут)."""
    kb = InlineKeyboardBuilder()
    for o in orders:
        kb.button(
            text=f"№{o['id']} {order_row_label(o)}"
                 f" · {fmt_money(o['price'])} · {fmt_dt(o['created_at'], tz, '%H:%M')}",
            callback_data=f"sc:o:{o['id']}",
        )
    kb.button(text="В меню", callback_data="sm")
    kb.adjust(1)
    return kb.as_markup()


def welcome_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Войти", callback_data="slogin")
    return kb.as_markup()


def login_cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data="scancel")
    return kb.as_markup()
