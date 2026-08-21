"""Ввод и вывод времени: промежуток скидки и сбросы 12-часовой серии."""

import pytest

from utils import (
    fmt_reset_times,
    fmt_time_min,
    parse_series_reset,
    parse_time,
    parse_time_range,
)


def test_fmt_time_min():
    assert fmt_time_min(0) == "00:00"
    assert fmt_time_min(600) == "10:00"
    assert fmt_time_min(960) == "16:00"
    assert fmt_time_min(9 * 60 + 5) == "09:05"
    assert fmt_time_min(1439) == "23:59"
    assert fmt_time_min(1440) == "24:00"
    assert fmt_time_min(600.0) == "10:00"      # значение из SQLite — float


@pytest.mark.parametrize("text, expected", [
    ("10", 600), ("10:00", 600), ("9:30", 570), ("09:05", 545),
    ("0:00", 0), ("00:00", 0), ("23:59", 1439), ("24:00", 1440),
    (" 16:00 ", 960),
    # \d в Python охватывает и не-ASCII цифры; int() понимает их так же,
    # как float() в parse_amount — поведение ввода одинаковое во всём боте
    ("١٠", 600),
])
def test_parse_time_accepts(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("text", [
    "", "abc", "25:00", "10:60", "24:01", "-1:00", "10:", ":30", "10:5:30",
])
def test_parse_time_rejects(text):
    assert parse_time(text) is None


@pytest.mark.parametrize("text", [
    "10:00-16:00", "10-16", "10:00 - 16:00", "10:00 16:00", "10:00–16:00",
    "10:00 — 16:00", "10.00-16.00", " 10:00-16:00 ",
])
def test_parse_time_range_accepts_common_forms(text):
    assert parse_time_range(text) == (600, 960)


def test_parse_time_range_keeps_minutes():
    assert parse_time_range("12:30-13:15") == (750, 795)
    assert parse_time_range("0:00-24:00") == (0, 1440)


@pytest.mark.parametrize("text", [
    "",                 # пусто
    "10:00",            # одна граница
    "16:00-10:00",      # конец раньше начала
    "10:00-10:00",      # пустой промежуток
    "10:00-16:00-18:00",
    "-10:00-16:00",
    "утро-вечер",
    "10:00-25:00",
    "10:00-",
])
def test_parse_time_range_rejects(text):
    assert parse_time_range(text) is None


@pytest.mark.parametrize("text, expected", [
    ("9", 540), ("9:00", 540), ("09:00", 540),
    ("21:00", 540),             # второй сброс суток задаёт те же серии
    ("9:00 21:00", 540), ("9:00-21:00", 540), ("21:00 - 9:00", 540),
    ("9.00-21.00", 540),
    ("0:00", 0), ("12:00", 0), ("0:00 12:00", 0),
    ("6:30", 390), ("18:30", 390), ("6:30 18:30", 390),
    ("24:00", 0),               # конец суток ≡ полночь
])
def test_parse_series_reset_accepts(text, expected):
    assert parse_series_reset(text) == expected


@pytest.mark.parametrize("text", [
    "", "abc", "25:00",
    "9:00 20:00",       # не 12 часов между сбросами
    "9:00 9:00",        # одинаковые времена
    "9:00 21:00 5:00",  # три времени
])
def test_parse_series_reset_rejects(text):
    assert parse_series_reset(text) is None


def test_fmt_reset_times():
    assert fmt_reset_times(540) == "09:00 и 21:00"
    assert fmt_reset_times(0) == "00:00 и 12:00"
    assert fmt_reset_times(390) == "06:30 и 18:30"
    assert fmt_reset_times(1260) == "09:00 и 21:00"   # 21:00 нормализуется
    assert fmt_reset_times(540.0) == "09:00 и 21:00"  # REAL из SQLite
