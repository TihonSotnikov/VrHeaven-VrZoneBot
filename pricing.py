"""Расчёт цены заказа и вознаграждения администратора.

Цена стандартного сеанса всегда выводится из текущих настроек в момент
оформления: база → скидка по расписанию → округление до 10 ₽; заранее
вычисленные цены нигде не хранятся. Акция идёт по своей цене из настроек
акций — скидка на неё не действует, ПК-бонус не начисляется.

Вознаграждение администратора — не процент, а фиксированная сумма по
месту заказа в 12-часовой серии (лесенка SALARY_LADDER): серия
сбрасывается дважды в сутки в моменты сброса администратора (по умолчанию
09:00 и 21:00; VR Heaven может задать индивидуальное время). Заказ по
акции — обычный заказ серии. Доля владельца остаётся процентной.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

KIND_STANDARD = "standard"
KIND_GROUP = "group"    # исторические заказы акции «3=4» (до перевода акций в настройки)
KIND_PROMO = "promo"

HEADSETS_CHOICES = (1, 2)
MINUTES_CHOICES = (15, 30, 60)

# Расписание скидки целиком задаётся настройками (панель VR Heaven) и
# судится по локальному времени из конфига:
#   discount_days      — битовая маска дней недели (бит 0 — понедельник,
#                        бит 6 — воскресенье), по умолчанию Пн–Пт;
#   discount_start_min — начало промежутка, минут от полуночи (включительно);
#   discount_end_min   — конец промежутка, минут от полуночи (исключая);
#   discount_percent   — размер скидки.
WEEKDAY_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# ПК-бонус напоминается на сеансах не короче этого времени
PC_BONUS_FROM_MINUTES = 30

# Вознаграждение администратора по номеру заказа в серии:
# 1-й — 50 ₽, 2-й — 100 ₽, 3-й — 150 ₽, 4-й — 200 ₽, 5-й и далее — 250 ₽
SALARY_LADDER = (50.0, 100.0, 150.0, 200.0, 250.0)

# Серия длится 12 часов; сбросы по умолчанию — 09:00 и 21:00.
# Хранится только первый сброс суток (минуты 0..719), второй — через 12 часов.
SERIES_PERIOD_MIN = 12 * 60
SERIES_RESET_DEFAULT_MIN = 9 * 60


@dataclass(frozen=True)
class Quote:
    base_price: float
    discount_percent: float  # 0, если скидка не действует
    price: float             # итоговая цена клиента (после скидки и округления)


def round10(value: float) -> int:
    """Округляет до ближайших 10 ₽ (удобно для наличного расчёта)."""
    # предварительное округление до копеек гасит двоичную погрешность
    # произведений вида base * 0.8 на границе половины десятки
    return int(round(value, 2) / 10 + 0.5) * 10


def ladder_amount(series_pos: int) -> float:
    """Вознаграждение администратора за заказ с номером series_pos в серии."""
    return SALARY_LADDER[min(series_pos, len(SALARY_LADDER)) - 1]


def normalize_reset(value) -> int:
    """Момент первого сброса серии в минутах 0..719.

    None — время не задано индивидуально, действует умолчание 09:00.
    Любое время суток приводится к первому сбросу: 21:00 ≡ 09:00.
    """
    if value is None:
        return SERIES_RESET_DEFAULT_MIN
    return int(value) % SERIES_PERIOD_MIN


def series_start(reset_min, now: datetime) -> datetime:
    """Начало текущей 12-часовой серии для момента now (локальное время).

    Сбросы происходят каждые 12 часов в моменты reset_min и reset_min + 12ч;
    начало серии — последний сброс, не превосходящий now.

    Моменты собираются по календарю, а не прибавлением timedelta: в поясе
    с переходом на летнее время прибавление сместило бы сброс на час
    дважды в году, и вознаграждение считалось бы не от того момента.
    """
    r = normalize_reset(reset_min)
    tz = now.tzinfo
    for day_shift in (0, -1):
        day = (now + timedelta(days=day_shift)).date()
        for shift in (SERIES_PERIOD_MIN, 0):
            minutes = r + shift
            candidate = datetime.combine(
                day, time(minutes // 60, minutes % 60), tzinfo=tz
            )
            if candidate <= now:
                return candidate
    day = (now - timedelta(days=1)).date()
    return datetime.combine(day, time(r // 60, r % 60), tzinfo=tz)


def split(price: float, admin_amount: float,
          owner_percent: float) -> tuple[float, float, float]:
    """Трёхстороннее деление цены заказа.

    Вознаграждение администратора — фиксированная сумма по серии; доля
    владельца — процент от цены, округлённый до копеек. Остаток VR Heaven
    считается вычитанием и поглощает погрешность округления — сумма трёх
    частей всегда равна цене с точностью до копейки. Остаток может быть
    отрицательным: лесенка вознаграждения не зависит от цены заказа.
    """
    admin_share = round(admin_amount, 2)
    owner_share = round(price * owner_percent / 100, 2)
    vr_share = round(price - admin_share - owner_share, 2)
    return admin_share, owner_share, vr_share


def base_price(settings: dict, headsets: int, minutes: int) -> float:
    return settings[f"price_{headsets}_{minutes}"]


def day_enabled(days_mask: float, weekday: int) -> bool:
    """Входит ли день недели (0 — понедельник) в маску дней скидки."""
    return bool(int(days_mask) >> weekday & 1)


def toggle_day(days_mask: float, weekday: int) -> int:
    """Маска с переключённым днём — для кнопок выбора дней скидки."""
    return int(days_mask) ^ (1 << weekday)


def fmt_days(days_mask: float) -> str:
    """Маска дней в подпись кнопки: 31 -> 'Пн Вт Ср Чт Пт'."""
    names = [name for i, name in enumerate(WEEKDAY_NAMES)
             if day_enabled(days_mask, i)]
    return " ".join(names) if names else "не выбраны"


def discount_active(settings: dict, now: datetime) -> bool:
    """Действует ли скидка в момент now.

    Скидка включена, день недели отмечен в маске и локальное время попадает
    в промежуток [начало, конец): в минуту конца скидки уже нет.
    """
    if not settings["discount_enabled"]:
        return False
    if not day_enabled(settings["discount_days"], now.weekday()):
        return False
    minutes = now.hour * 60 + now.minute
    return settings["discount_start_min"] <= minutes < settings["discount_end_min"]


def pc_bonus_applies(settings: dict, kind: str, minutes: int | None) -> bool:
    """Нужно ли напомнить админу начислить клиенту ПК-бонус.

    Напоминание не зависит от скидки — на цену ПК-бонус не влияет вовсе.
    При заказах по акции баллы не начисляются.
    """
    return (bool(settings["pc_bonus_enabled"]) and kind == KIND_STANDARD
            and minutes is not None and minutes >= PC_BONUS_FROM_MINUTES)


def quote(settings: dict, now: datetime, headsets: int, minutes: int) -> Quote:
    """Цена стандартного сеанса на момент now: база → скидка → округление.

    Акции через quote не проходят: их цена берётся из настройки акции
    как есть, без скидки и округления.
    """
    base = base_price(settings, headsets, minutes)
    discount = settings["discount_percent"] if discount_active(settings, now) else 0
    return Quote(
        base_price=base,
        discount_percent=discount,
        price=float(round10(base * (1 - discount / 100))),
    )


def order_label(kind: str, headsets: int | None = None,
                minutes: int | None = None, promo_name: str | None = None) -> str:
    if kind == KIND_GROUP:
        return "Акция 3=4"
    if kind == KIND_PROMO:
        return f"Акция · {promo_name}" if promo_name else "Акция"
    word = "шлем" if headsets == 1 else "шлема"
    return f"{headsets} {word} · {minutes} мин"


def order_row_label(order) -> str:
    """Подпись заказа по строке из БД (учитывает акции по имени)."""
    promo_name = order["promo_name"] if "promo_name" in order.keys() else None
    return order_label(order["kind"], order["headsets"], order["minutes"], promo_name)
