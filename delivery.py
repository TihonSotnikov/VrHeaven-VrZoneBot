"""Транспорт: ограничение темпа исходящих запросов и разумные повторы.

Слой сидит на bot.session.middleware и охватывает **каждый** вызов Bot API —
отправку, правку, удаление, ответ на кнопку. Обойти его нельзя, менять
вызывающий код не нужно.

Одно различие здесь важнее остальных: при сетевой ошибке повторяются
только идемпотентные методы. Отправка сообщения не повторяется никогда —
запрос мог дойти до Telegram и потеряться уже на ответе, и повтор
продублировал бы финансовое уведомление. Такие сбои уходят наверх,
в очередь Записей, которая знает про dedup_key и умеет решать этот вопрос
правильно.
"""

import asyncio
import logging
import random
import time

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
)

log = logging.getLogger(__name__)

# Повторять при сетевом сбое безопасно только там, где повтор ничего
# не создаёт заново
IDEMPOTENT_METHODS = (EditMessageText, EditMessageReplyMarkup, DeleteMessage,
                      AnswerCallbackQuery)

GLOBAL_RATE = 25.0        # запросов в секунду суммарно (лимит Telegram ~30)
CHAT_RATE = 1.0           # запросов в секунду в один чат
CHAT_BURST = 5.0
MAX_RETRIES = 3

# Ведёрко заводится на каждый чат, а писать боту может кто угодно: без
# верхней границы словарь растёт от каждого случайного /start и живёт до
# перезапуска. Наполнившееся ведёрко ничего не ограничивает — его можно
# выбросить и завести заново при следующем запросе.
MAX_BUCKETS = 10_000


class _Bucket:
    """Простое токенное ведро на монотонных часах."""

    def __init__(self, rate: float, burst: float):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.updated = time.monotonic()

    def take(self) -> float:
        """Забирает токен; возвращает, сколько секунд надо подождать."""
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate

    def is_full(self, now: float) -> bool:
        return self.tokens + (now - self.updated) * self.rate >= self.burst


def _bucket_for(buckets: dict[int, _Bucket], key: int, rate: float,
                burst: float) -> _Bucket:
    """Ведёрко чата; на переполнении словаря выбрасывает наполнившиеся.

    Ведёрко активного чата не трогается никогда: ограничение важнее
    памяти. Если выбрасывать нечего, словарь растёт — но тогда его
    размер и есть число чатов, которые прямо сейчас пишут боту.
    """
    bucket = buckets.get(key)
    if bucket is None:
        if len(buckets) >= MAX_BUCKETS:
            now = time.monotonic()
            for existing_key, existing in list(buckets.items()):
                if existing.is_full(now):
                    del buckets[existing_key]
        bucket = buckets[key] = _Bucket(rate, burst)
    return bucket


class ThrottleMiddleware(BaseRequestMiddleware):
    def __init__(self, *, global_rate: float = GLOBAL_RATE,
                 chat_rate: float = CHAT_RATE, max_retries: int = MAX_RETRIES):
        self._global = _Bucket(global_rate, global_rate)
        self._chats: dict[int, _Bucket] = {}
        self._chat_rate = chat_rate
        self._max_retries = max_retries
        self._lock = asyncio.Lock()

    async def _wait_slot(self, chat_id) -> None:
        async with self._lock:
            delay = self._global.take()
            if chat_id is not None:
                bucket = _bucket_for(self._chats, chat_id, self._chat_rate,
                                     CHAT_BURST)
                delay = max(delay, bucket.take())
        if delay > 0:
            await asyncio.sleep(delay)

    async def __call__(self, make_request, bot, method):
        chat_id = getattr(method, "chat_id", None)
        idempotent = isinstance(method, IDEMPOTENT_METHODS)
        for attempt in range(self._max_retries):
            await self._wait_slot(chat_id)
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                if attempt == self._max_retries - 1:
                    raise
                pause = e.retry_after + random.uniform(0.1, 0.5)
                log.warning("Telegram просит подождать %.1f с (%s)",
                            pause, type(method).__name__)
                await asyncio.sleep(pause)
            except (TelegramServerError, TelegramNetworkError):
                # Повторяем только то, что нельзя выполнить дважды с
                # разным результатом; отправку сообщений — никогда
                if not idempotent or attempt == self._max_retries - 1:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError("Недостижимо: цикл повторов завершился без результата")


class InboundThrottle(BaseMiddleware):
    """Ограничение входящего потока по чату.

    Бот открыт любому пользователю Telegram: /start и попытки входа может
    слать кто угодно. Лишние апдейты отбрасываются до хендлеров, поэтому
    поток от одного чата не мешает работать остальным.
    """

    def __init__(self, *, rate: float = 2.0, burst: float = 10.0):
        self._buckets: dict[int, _Bucket] = {}
        self._rate = rate
        self._burst = burst

    async def __call__(self, handler, event, data):
        chat = data.get("event_chat")
        if chat is not None:
            bucket = _bucket_for(self._buckets, chat.id, self._rate, self._burst)
            if bucket.take() > 0:
                log.warning("Поток из чата %s ограничен", chat.id)
                callback = getattr(event, "callback_query", None)
                if callback is not None:
                    await callback.answer("Слишком часто — подождите секунду")
                return None
        return await handler(event, data)
