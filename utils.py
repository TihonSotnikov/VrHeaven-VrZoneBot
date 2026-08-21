"""Форматирование денег и времени, разбор пользовательского ввода, пароли.

Экранирование текста сообщений живёт в markup.py: здесь нет ни одной
функции, знающей о разметке Telegram.
"""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# ------------------------------------------------------------------- Деньги

# Минус — типографский U+2212, а не дефис: он не является спецсимволом
# ни в одной разметке Telegram, поэтому отрицательная сумма не может
# сломать сообщение (см. AUDIT D-1).
MINUS = "−"


def fmt_num(amount: float) -> str:
    """1234567.5 -> '1 234 567,50'; отрицательные — с типографским минусом."""
    amount = round(float(amount), 2)
    sign = MINUS if amount < 0 else ""
    amount = abs(amount)
    if amount == int(amount):
        s = f"{int(amount):,}"
    else:
        s = f"{amount:,.2f}"
    return sign + s.replace(",", " ").replace(".", ",")


def fmt_money(amount: float) -> str:
    return f"{fmt_num(amount)} ₽"


def fmt_percent(percent: float) -> str:
    percent = round(float(percent), 2)
    if percent == int(percent):
        return f"{int(percent)}%"
    return f"{percent:.2f}".rstrip("0").replace(".", ",") + "%"


def fmt_signed_money(amount: float) -> str:
    """Сумма со знаком: бонус может быть удержанием (отрицательным)."""
    if amount > 0:
        return "+" + fmt_money(amount)
    return fmt_money(amount)


# Верхняя граница любой денежной суммы: защищает от опечатки «30000»
# вместо «300» и от переполнения при выводе.
AMOUNT_MAX = 1_000_000.0


def parse_amount(text: str, *, allow_negative: bool = False) -> float | None:
    """Разбор суммы: '1 500', '1500.50', '1500,50'. None — если некорректно.

    allow_negative разрешает удержание (отрицательный бонус); ноль
    не принимается никогда — нулевая сумма не является операцией.
    """
    t = (text.strip().replace(" ", "").replace(" ", "").replace(" ", "")
         .replace(MINUS, "-").replace(",", "."))
    try:
        value = round(float(t), 2)
    except (ValueError, OverflowError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if value == 0:
        return None
    if not allow_negative and value < 0:
        return None
    if abs(value) > AMOUNT_MAX:
        return None
    return value


def parse_percent(text: str) -> float | None:
    """Доля владельца из ввода: '0', '7', '12,5'.

    Диапазон — от 0 включительно до 100 не включительно (SPEC §3):
    доля 100% не оставила бы остатка VR Heaven. None — если некорректно.
    """
    t = text.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        value = round(float(t), 2)
    except (ValueError, OverflowError):
        return None
    if value != value:
        return None
    if not (0 <= value < 100):
        return None
    return value


# --------------------------------------------------------------------- Время

def fmt_time_min(minutes: float) -> str:
    """Минуты от полуночи в подпись: 600 -> '10:00', 1440 -> '24:00'."""
    total = int(minutes)
    return f"{total // 60:02d}:{total % 60:02d}"


_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{1,2}))?$")

# Границы промежутка разделяются дефисом (любым) или пробелами
_RANGE_SPLIT_RE = re.compile(r"\s*[-–—−]\s*|\s+")


def parse_time(text: str) -> int | None:
    """Время суток в минуты от полуночи: '10', '10:00', '9:30'.

    Допускается '24:00' — конец суток. None, если формат неверный.
    """
    match = _TIME_RE.match(text.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 24 or minute > 59 or (hour == 24 and minute):
        return None
    return hour * 60 + minute


def parse_time_range(text: str) -> tuple[int, int] | None:
    """Промежуток '10:00-16:00' (также '10-16', '10.00 – 16.00') -> (600, 960).

    None — если формат неверный или начало не раньше конца: промежуток
    через полночь не поддерживается, экран ввода об этом предупреждает.
    """
    parts = _RANGE_SPLIT_RE.split(text.strip().replace(".", ":"))
    if len(parts) != 2:
        return None
    start, end = parse_time(parts[0]), parse_time(parts[1])
    if start is None or end is None or start >= end:
        return None
    return start, end


def parse_series_reset(text: str) -> int | None:
    """Время сбросов 12-часовой серии: '9', '09:00' или пара '9:00 21:00'.

    Серия сбрасывается дважды в сутки с шагом ровно 12 часов, поэтому
    хранится только первый сброс суток — минуты 0..719; второй — через
    12 часов. Пара времён принимается, если отстоит ровно на 12 часов.
    None — формат неверный или пара не образует 12-часовые серии.
    """
    parts = _RANGE_SPLIT_RE.split(text.strip().replace(".", ":"))
    times = [parse_time(p) for p in parts]
    if any(t is None for t in times):
        return None
    if len(times) == 1:
        return times[0] % 720
    if len(times) == 2 and abs(times[0] - times[1]) == 720:
        return min(times) % 720
    return None


def fmt_reset_times(reset_min: float) -> str:
    """Оба момента сброса серии одной строкой: 540 -> '09:00 и 21:00'."""
    first = int(reset_min) % 720
    return f"{fmt_time_min(first)} и {fmt_time_min(first + 720)}"


# --------------------------------------------------------------------- Даты

def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    """Момент «сейчас» в UTC для хранения в БД (ISO 8601, сортируется строкой)."""
    return utcnow().isoformat(timespec="seconds")


def fmt_dt(iso: str, tz: ZoneInfo, fmt: str = "%d.%m.%Y %H:%M") -> str:
    return datetime.fromisoformat(iso).astimezone(tz).strftime(fmt)


# ------------------------------------------------------------------- Пароли
#
# PBKDF2 стоит ~20 мс — на event loop это блокировка всего бота, поэтому
# хендлеры вызывают hash_password_async/verify_password_async.

_PWD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
_PBKDF2_ROUNDS = 200_000


def gen_password(length: int = 10) -> str:
    return "".join(secrets.choice(_PWD_ALPHABET) for _ in range(length))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return hmac.compare_digest(digest, expected)


async def hash_password_async(password: str) -> str:
    import asyncio
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, stored: str) -> bool:
    import asyncio
    return await asyncio.to_thread(verify_password, password, stored)
