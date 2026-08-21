"""Разбор callback-данных и общий обработчик ошибок.

Две вещи, без которых кнопка может «зависнуть» навсегда: безопасный
разбор нажатия (Telegram передаёт то, что прислал клиент, а клиент бывает
самодельный) и обработчик, который отвечает на нажатие в любом случае.
Незакрытый спиннер на экране — это отсутствие обратной связи ровно в тот
момент, когда человек ждёт ответа.
"""

import logging
from collections import Counter

from aiogram.types import ErrorEvent

log = logging.getLogger(__name__)

# Счётчики для утренней сводки: молчаливых отказов быть не должно
COUNTERS: Counter = Counter()

STALE_BUTTON = "Кнопка устарела — откройте экран заново"
GENERIC_ERROR = "Что-то пошло не так. Попробуйте ещё раз или напишите @VrHeaven"

# Разумный предел: id в этой базе далеко не астрономические, а int()
# без границы принимает число любой длины и роняет SQLite
_MAX_ID = 2 ** 53


def cb_int(data: str | None, index: int = -1) -> int | None:
    """Целое из callback-данных 'prefix:action:123'. None — если мусор."""
    if not data:
        return None
    parts = data.split(":")
    try:
        value = int(parts[index])
    except (ValueError, IndexError, TypeError):
        return None
    if abs(value) > _MAX_ID:
        return None
    return value


def cb_ints(data: str | None, count: int) -> tuple[int, ...] | None:
    """Последние count целых из callback-данных. None — если мусор."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < count:
        return None
    values = []
    for raw in parts[-count:]:
        try:
            value = int(raw)
        except (ValueError, TypeError):
            return None
        if abs(value) > _MAX_ID:
            return None
        values.append(value)
    return tuple(values)


def cb_tail(data: str | None, prefix: str) -> str | None:
    """Хвост callback-данных после префикса — для строковых ключей."""
    if not data or not data.startswith(prefix):
        return None
    return data[len(prefix):]


def setup_error_handler(dp) -> None:
    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        COUNTERS["errors"] += 1
        update = event.update
        log.exception("Необработанная ошибка при обработке апдейта %s",
                      getattr(update, "update_id", "?"), exc_info=event.exception)
        callback = getattr(update, "callback_query", None)
        if callback is not None:
            try:
                await callback.answer(GENERIC_ERROR, show_alert=True)
            except Exception:
                log.debug("Не удалось ответить на нажатие после ошибки")
        return True                       # ошибка обработана, бот продолжает
