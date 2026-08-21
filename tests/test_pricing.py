"""Расчёт цены: базовые цены, скидка по расписанию, округление, ПК-бонус;
лесенка вознаграждения и 12-часовые серии."""

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from db import SETTINGS_DEFAULTS
from pricing import (
    KIND_GROUP,
    KIND_PROMO,
    KIND_STANDARD,
    MINUTES_CHOICES,
    SALARY_LADDER,
    SERIES_RESET_DEFAULT_MIN,
    day_enabled,
    discount_active,
    fmt_days,
    ladder_amount,
    normalize_reset,
    order_label,
    order_row_label,
    pc_bonus_applies,
    quote,
    round10,
    series_start,
    toggle_day,
)

MONDAY_MORNING = datetime(2026, 7, 13, 10, 0)   # понедельник, ровно 10:00
MONDAY_16 = datetime(2026, 7, 13, 16, 0)        # понедельник, ровно 16:00
SATURDAY = datetime(2026, 7, 18, 10, 0)         # суббота


def settings(**overrides) -> dict:
    return {**SETTINGS_DEFAULTS, **overrides}


def test_default_base_prices():
    s = settings(discount_enabled=0)
    expected = {(1, 15): 200, (1, 30): 300, (1, 60): 500,
                (2, 15): 350, (2, 30): 500, (2, 60): 800}
    for (headsets, minutes), price in expected.items():
        q = quote(s, MONDAY_MORNING, headsets, minutes)
        assert q.price == price
        assert q.discount_percent == 0


def test_weekday_discount_applies_on_monday_morning():
    q = quote(settings(), MONDAY_MORNING, 1, 30)
    assert q.base_price == 300
    assert q.discount_percent == 20
    assert q.price == 240


def test_all_standard_prices_discounted_in_discount_hours():
    """Скидку получают все стандартные сеансы — длительность не важна;
    ПК-бонус на цену не влияет."""
    s = settings()
    expected = {(1, 15): 160, (1, 30): 240, (1, 60): 400,
                (2, 15): 280, (2, 30): 400, (2, 60): 640}
    for (headsets, minutes), price in expected.items():
        q = quote(s, MONDAY_MORNING, headsets, minutes)
        assert q.discount_percent == 20, (headsets, minutes)
        assert q.price == price, (headsets, minutes)


def test_discount_boundaries():
    friday_1559 = datetime(2026, 7, 17, 15, 59)    # пятница, 15:59 — действует
    friday_1600 = datetime(2026, 7, 17, 16, 0)     # пятница, 16:00 — нет
    sunday = datetime(2026, 7, 19, 10, 0)          # воскресенье — нет
    s = settings()
    assert discount_active(s, friday_1559)
    assert not discount_active(s, friday_1600)
    assert not discount_active(s, sunday)


def test_default_schedule_is_weekdays_10_to_16():
    """По умолчанию скидка начинается в 10:00, а не с полуночи:
    начало включительно, конец исключая."""
    assert SETTINGS_DEFAULTS["discount_start_min"] == 600
    assert SETTINGS_DEFAULTS["discount_end_min"] == 960
    assert SETTINGS_DEFAULTS["discount_days"] == 0b0011111
    s = settings()
    assert not discount_active(s, datetime(2026, 7, 13, 0, 0))    # полночь
    assert not discount_active(s, datetime(2026, 7, 13, 9, 59))
    assert discount_active(s, datetime(2026, 7, 13, 10, 0))       # ровно начало
    assert discount_active(s, datetime(2026, 7, 13, 15, 59))
    assert not discount_active(s, datetime(2026, 7, 13, 16, 0))   # ровно конец
    assert not discount_active(s, datetime(2026, 7, 13, 23, 59))


def test_custom_time_window_from_settings():
    """Промежуток берётся из настроек, минуты учитываются."""
    s = settings(discount_start_min=12 * 60 + 30, discount_end_min=13 * 60 + 15)
    assert not discount_active(s, datetime(2026, 7, 13, 12, 29))
    assert discount_active(s, datetime(2026, 7, 13, 12, 30))
    assert discount_active(s, datetime(2026, 7, 13, 13, 14))
    assert not discount_active(s, datetime(2026, 7, 13, 13, 15))
    # цена считается со скидкой только внутри промежутка
    assert quote(s, datetime(2026, 7, 13, 12, 45), 1, 15).price == 160
    assert quote(s, datetime(2026, 7, 13, 14, 0), 1, 15).price == 200
    # промежуток до конца суток
    whole_evening = settings(discount_start_min=18 * 60, discount_end_min=24 * 60)
    assert discount_active(whole_evening, datetime(2026, 7, 13, 23, 59))


def test_custom_days_from_settings():
    """Дни недели задаются маской: скидка идёт ровно в отмеченные дни."""
    weekend = settings(discount_days=0b1100000)          # Сб и Вс
    assert discount_active(weekend, SATURDAY)
    assert discount_active(weekend, datetime(2026, 7, 19, 10, 0))   # воскресенье
    assert not discount_active(weekend, MONDAY_MORNING)
    only_wednesday = settings(discount_days=0b0000100)
    assert discount_active(only_wednesday, datetime(2026, 7, 15, 10, 0))
    assert not discount_active(only_wednesday, datetime(2026, 7, 14, 10, 0))
    # пустая маска — скидки нет ни в один день
    never = settings(discount_days=0)
    for day in range(13, 20):
        assert not discount_active(never, datetime(2026, 7, day, 10, 0))


def test_day_mask_helpers():
    weekdays = SETTINGS_DEFAULTS["discount_days"]
    assert [day_enabled(weekdays, d) for d in range(7)] == [
        True, True, True, True, True, False, False]
    assert fmt_days(weekdays) == "Пн Вт Ср Чт Пт"
    assert fmt_days(0) == "не выбраны"
    assert fmt_days(0b1111111) == "Пн Вт Ср Чт Пт Сб Вс"
    # переключение добавляет и снимает день, не трогая остальные
    assert toggle_day(weekdays, 5) == 0b0111111
    assert toggle_day(toggle_day(weekdays, 5), 5) == weekdays
    assert toggle_day(weekdays, 0) == 0b0011110
    # значения из SQLite приходят как float — маска всё равно целочисленная
    assert day_enabled(31.0, 4) and not day_enabled(31.0, 5)
    assert toggle_day(31.0, 5) == 63


def test_discount_inactive_at_16_on_weekend_or_disabled():
    assert not discount_active(settings(), MONDAY_16)
    assert not discount_active(settings(), SATURDAY)
    assert not discount_active(settings(discount_enabled=0), MONDAY_MORNING)
    assert quote(settings(), SATURDAY, 1, 15).price == 200


def test_rounding_to_nearest_10():
    assert round10(784) == 780
    assert round10(785) == 790
    assert round10(786) == 790
    # нестандартная цена: 990 − 20% = 792 -> 790
    q = quote(settings(price_1_15=990), MONDAY_MORNING, 1, 15)
    assert q.price == 790


def test_rounding_exercised_when_discount_breaks_multiple_of_10():
    # 190 − 20% = 152 — не кратно 10, округление обязано сработать: -> 150
    q = quote(settings(price_1_15=190), MONDAY_MORNING, 1, 15)
    assert q.base_price == 190
    assert q.price == 150
    # без скидки 190 кратно 10 — не меняется
    assert quote(settings(price_1_15=190, discount_enabled=0),
                 MONDAY_MORNING, 1, 15).price == 190
    # цена, не кратная 10, округляется и без скидки: 195 -> 200
    assert quote(settings(price_1_15=195, discount_enabled=0),
                 MONDAY_MORNING, 1, 15).price == 200


def test_round10_matches_exact_decimal_reference():
    """round10 против эталона на Decimal: ближайшие 10 ₽, половина — вверх.
    Плотный проход по копеечной сетке ловит и двоичный шум, и границы половины."""
    kop = Decimal("0.01")
    value = Decimal("0")
    while value <= 300:
        expected = int((value / 10).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 10)
        assert round10(float(value)) == expected, value
        value += kop
    # крупные значения и точные половины десятки
    for v, expected in [(785, 790), (784.99, 780), (785.01, 790),
                        (99995, 100000), (99994.99, 99990)]:
        assert round10(v) == expected, v


def test_rounding_immune_to_float_noise_at_half_boundary():
    """База, у которой ×0,8 попадает ровно на половину десятки: двоичная
    погрешность произведения не должна утянуть округление вниз."""
    # 231.25 × 0.8 = 185 -> 190; 981.25 × 0.8 = 785 -> 790
    q = quote(settings(price_1_15=231.25), MONDAY_MORNING, 1, 15)
    assert q.price == 190
    q = quote(settings(price_1_15=981.25), MONDAY_MORNING, 1, 15)
    assert q.price == 790
    # чуть ниже половины — вниз: 231.24 × 0.8 = 184.992 -> 180
    q = quote(settings(price_1_15=231.24), MONDAY_MORNING, 1, 15)
    assert q.price == 180


def test_discount_governed_by_config_timezone_not_utc():
    """Скидка судит по локальным полям aware-времени конфига, а не по UTC."""
    nsk = ZoneInfo("Asia/Novosibirsk")
    s = settings(discount_start_min=0)          # промежуток с полуночи
    # понедельник 00:30 в Новосибирске == воскресенье 17:30 UTC — скидка есть
    monday_local = datetime(2026, 7, 20, 0, 30, tzinfo=nsk)
    assert monday_local.astimezone(ZoneInfo("UTC")).weekday() == 6  # в UTC воскресенье
    assert discount_active(s, monday_local)
    # суббота 05:00 в Новосибирске == пятница 22:00 UTC — скидки нет
    saturday_local = datetime(2026, 7, 18, 5, 0, tzinfo=nsk)
    assert saturday_local.astimezone(ZoneInfo("UTC")).weekday() == 4  # в UTC пятница
    assert not discount_active(s, saturday_local)
    # час тоже локальный: понедельник 17:00 в Новосибирске (скидки уже нет)
    # == 10:00 UTC, где промежуток ещё действовал бы
    evening_local = datetime(2026, 7, 20, 17, 0, tzinfo=nsk)
    assert evening_local.astimezone(ZoneInfo("UTC")).hour == 10
    assert not discount_active(settings(), evening_local)


def test_custom_discount_percent_from_settings():
    q = quote(settings(discount_percent=50), MONDAY_MORNING, 1, 15)
    assert q.price == 100


def test_order_label():
    assert order_label(KIND_STANDARD, 1, 15) == "1 шлем · 15 мин"
    assert order_label(KIND_STANDARD, 2, 60) == "2 шлема · 60 мин"
    assert order_label(KIND_GROUP) == "Акция 3=4"          # исторические заказы
    assert order_label(KIND_PROMO, promo_name="3=4") == "Акция · 3=4"
    assert order_label(KIND_PROMO) == "Акция"              # снимок имени утерян
    assert order_row_label({"kind": "promo", "headsets": None, "minutes": None,
                            "promo_name": "День рождения"}) == "Акция · День рождения"
    assert order_row_label({"kind": "standard", "headsets": 1, "minutes": 30,
                            "promo_name": None}) == "1 шлем · 30 мин"


def test_pc_bonus_does_not_touch_the_price():
    """ПК-бонус — только напоминание: цена от него не зависит вовсе,
    и со скидкой он уживается на одном заказе."""
    with_bonus = settings()
    without_bonus = settings(pc_bonus_enabled=0)
    for now in (MONDAY_MORNING, SATURDAY):            # в скидку и вне её
        for minutes in MINUTES_CHOICES:
            assert (quote(with_bonus, now, 1, minutes)
                    == quote(without_bonus, now, 1, minutes)), (now, minutes)
    # 30 минут в часы скидки: и скидка в цене, и напоминание о баллах
    q = quote(with_bonus, MONDAY_MORNING, 1, 30)
    assert q.discount_percent == 20 and q.price == 240
    assert pc_bonus_applies(with_bonus, KIND_STANDARD, 30)


def test_pc_bonus_reminder_rules():
    """Напоминание — на сеансах от 30 минут; заказы по акции без баллов."""
    s = settings()
    assert pc_bonus_applies(s, KIND_STANDARD, 30)
    assert pc_bonus_applies(s, KIND_STANDARD, 60)
    assert not pc_bonus_applies(s, KIND_STANDARD, 15)
    assert not pc_bonus_applies(s, KIND_PROMO, None)
    assert not pc_bonus_applies(s, KIND_GROUP, None)
    assert not pc_bonus_applies(settings(pc_bonus_enabled=0), KIND_STANDARD, 30)
    # порог «от 30 минут», а не перечень длительностей
    assert not pc_bonus_applies(s, KIND_STANDARD, 29)
    assert pc_bonus_applies(s, KIND_STANDARD, 45)
    assert pc_bonus_applies(s, KIND_STANDARD, 90)
    # длительности у акции нет — на None проверка не падает
    assert not pc_bonus_applies(s, KIND_STANDARD, None)


# ------------------------------------------------- Лесенка вознаграждения

def test_salary_ladder_values():
    """1-й — 50, 2-й — 100, 3-й — 150, 4-й — 200, 5-й и далее — 250."""
    assert SALARY_LADDER == (50.0, 100.0, 150.0, 200.0, 250.0)
    assert [ladder_amount(pos) for pos in range(1, 6)] == [50, 100, 150, 200, 250]
    for pos in (6, 7, 10, 100):
        assert ladder_amount(pos) == 250


def test_normalize_reset_defaults_and_wraps():
    assert SERIES_RESET_DEFAULT_MIN == 540            # 09:00
    assert normalize_reset(None) == 540               # умолчание
    assert normalize_reset(540) == 540
    assert normalize_reset(1260) == 540               # 21:00 ≡ 09:00
    assert normalize_reset(0) == 0
    assert normalize_reset(720) == 0                  # 12:00 ≡ 00:00
    assert normalize_reset(60.0) == 60                # REAL из SQLite


def test_series_start_default_09_21():
    """Серия по умолчанию: 09:00–21:00 и 21:00–09:00 (ночная смена)."""
    day = datetime(2026, 8, 12, 0, 0)

    def start(h, m=0):
        return series_start(None, day.replace(hour=h, minute=m))

    assert start(9) == day.replace(hour=9)            # ровно момент сброса
    assert start(12) == day.replace(hour=9)
    assert start(20, 59) == day.replace(hour=9)
    assert start(21) == day.replace(hour=21)          # вечерний сброс
    assert start(23, 59) == day.replace(hour=21)
    # ночь после полуночи принадлежит вчерашней вечерней серии
    assert start(0) == day.replace(hour=21) - timedelta(days=1)
    assert start(8, 59) == day.replace(hour=21) - timedelta(days=1)


def test_series_start_custom_reset():
    """Индивидуальное время сброса: 06:00 -> серии 06:00–18:00 и 18:00–06:00."""
    day = datetime(2026, 8, 12, 0, 0)
    assert series_start(6 * 60, day.replace(hour=7)) == day.replace(hour=6)
    assert series_start(6 * 60, day.replace(hour=17, minute=59)) == day.replace(hour=6)
    assert series_start(6 * 60, day.replace(hour=18)) == day.replace(hour=18)
    assert (series_start(6 * 60, day.replace(hour=5, minute=59))
            == day.replace(hour=18) - timedelta(days=1))
    # хранится любой из двух моментов: 18:00 задаёт те же серии, что и 06:00
    assert series_start(18 * 60, day.replace(hour=7)) == day.replace(hour=6)
    # полночь: серии 00:00–12:00 и 12:00–24:00
    assert series_start(0, day.replace(hour=0)) == day
    assert series_start(0, day.replace(hour=11, minute=59)) == day
    assert series_start(0, day.replace(hour=12)) == day.replace(hour=12)


def test_series_start_timezone_aware():
    """Серия судится по локальному времени: aware-момент даёт aware-начало."""
    nsk = ZoneInfo("Asia/Novosibirsk")
    now = datetime(2026, 8, 12, 10, 30, tzinfo=nsk)
    start = series_start(None, now)
    assert start == datetime(2026, 8, 12, 9, 0, tzinfo=nsk)
    assert start.tzinfo is nsk
