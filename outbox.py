"""Долговечная доставка Записей.

Уведомление, отправленное «как получится», молча теряется — именно так
исчезали отчёты дня выплат и уведомления супер-админам. Здесь Запись
сначала попадает в таблицу в одной транзакции с изменением, которое её
породило, и только потом её пытается доставить фоновый работник. Он
переживает перезапуск, повторяет попытки с нарастающей паузой, а то, что
доставить не удалось совсем, остаётся видимым числом, о котором бот
докладывает супер-админам.

После каждой пачки Записей Окно чата переставляется вниз — экран, на
котором работал человек, оказывается под уведомлением целым.
"""

import asyncio
import logging
from datetime import timedelta

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)

import notify
from config import Config
from db import Actor, Database
from markup import h
from messaging import Messenger, payload_of
from utils import utcnow, utcnow_iso

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
# Паузы между попытками: секунды. Дальше — как за последней.
BACKOFF_SECONDS = (5, 30, 120, 600, 1800, 3600, 7200)

# Ответы Telegram, после которых повторять бессмысленно: чат недоступен
# навсегда, а не временно
_PERMANENT_MARKERS = (
    "chat not found", "bot was blocked", "user is deactivated",
    "peer_id_invalid", "bot can't initiate conversation",
    "have no rights to send", "chat_write_forbidden",
)


def _is_permanent(error: Exception) -> bool:
    if isinstance(error, (TelegramForbiddenError, TelegramNotFound)):
        return True
    if isinstance(error, TelegramBadRequest):
        text = str(error).lower()
        return any(marker in text for marker in _PERMANENT_MARKERS)
    return False


def _next_attempt(attempts: int) -> str:
    delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]
    return (utcnow() + timedelta(seconds=delay)).isoformat(timespec="seconds")


class OutboxWorker:
    def __init__(self, bot, db: Database, ui: Messenger, config: Config,
                 *, idle_interval: float = 2.0):
        self.bot = bot
        self.db = db
        self.ui = ui
        self.config = config
        self.idle_interval = idle_interval
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task | None = None

    def wake(self) -> None:
        """Сигнал работнику: появились новые Записи."""
        self._wakeup.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="outbox-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        while True:
            try:
                sent = await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Сбой доставки Записей")
                sent = 0
            if sent:
                continue
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.idle_interval)
            except TimeoutError:
                pass
            self._wakeup.clear()

    async def drain(self, limit: int = 25) -> int:
        """Одна пачка: доставить готовые Записи и переставить окна."""
        rows = await self.db.due_records(utcnow_iso(), limit=limit)
        if not rows:
            return 0
        touched: list[int] = []
        for row in rows:
            delivered = await self._deliver(row)
            if delivered and row["chat_id"] not in touched:
                touched.append(row["chat_id"])
        for chat_id in touched:
            await self.ui.reanchor(chat_id)
        return len(rows)

    async def _deliver(self, row) -> bool:
        chat_id = row["chat_id"]
        try:
            message_id = await self.ui.send_record(chat_id, payload_of(row))
        except TelegramRetryAfter as e:
            await self._retry_at(
                row, (utcnow() + timedelta(seconds=e.retry_after + 1))
                .isoformat(timespec="seconds"), str(e))
            return False
        except FileNotFoundError as e:
            await self._final(row, "dropped", f"Файл недоступен: {e}")
            return False
        except TelegramEntityTooLarge as e:
            await self._final(row, "dropped", str(e))
            return False
        except TelegramAPIError as e:
            if _is_permanent(e):
                await self._drop_dead_chat(row, e)
            else:
                await self._retry(row, e)
            return False
        except Exception as e:                       # неожиданное — не теряем
            await self._retry(row, e)
            return False
        async with self.db.write() as tx:
            await self.db.mark_record_sent(tx, row["id"], message_id)
        return True

    async def _retry(self, row, error: Exception) -> None:
        attempts = row["attempts"] + 1
        if attempts >= MAX_ATTEMPTS:
            await self._final(row, "failed", str(error))
            await self._alert_failure(row, error)
            log.error("Запись %s чату %s не доставлена: %s",
                      row["id"], row["chat_id"], error)
            return
        await self._retry_at(row, _next_attempt(row["attempts"]), str(error))
        log.warning("Запись %s чату %s отложена (попытка %s): %s",
                    row["id"], row["chat_id"], attempts, error)

    async def _retry_at(self, row, when_iso: str, error: str) -> None:
        async with self.db.write() as tx:
            await self.db.mark_record_retry(tx, row["id"], when_iso, error)

    async def _final(self, row, status: str, error: str) -> None:
        async with self.db.write() as tx:
            await self.db.mark_record_final(tx, row["id"], status, error)

    async def _drop_dead_chat(self, row, error: Exception) -> None:
        """Чат недоступен навсегда: отвязываем устройство один раз вместо
        того, чтобы вечно писать в журнал одну и ту же ошибку."""
        chat_id = row["chat_id"]
        async with self.db.write() as tx:
            await self.db.mark_record_final(tx, row["id"], "dropped", str(error))
            user = await self.db.get_user_by_chat(chat_id)
            removed = await self.db.unbind_chat(tx, chat_id)
            await self.db.drop_window(tx, chat_id)
            if removed:
                await self.db.audit(
                    tx, Actor.system(), "chat.unbind", "user",
                    user["id"] if user else None,
                    after={"chat_id": chat_id, "reason": "chat_unavailable"},
                )
                await self._enqueue_alert(
                    tx,
                    h("<b>Устройство отключено</b>\n\n"
                      "Кабинет: {}\nПричина: чат недоступен (бот заблокирован "
                      "или удалён)\nПри следующем входе устройство "
                      "подключится заново",
                      user["handle"] if user else chat_id),
                    dedup=f"deadchat:{chat_id}:{row['id']}",
                )
        log.info("Чат %s отвязан: %s", chat_id, error)

    async def _alert_failure(self, row, error: Exception) -> None:
        if row["kind"] == "alert":
            return                                    # тревога о тревоге не нужна
        async with self.db.write() as tx:
            await self._enqueue_alert(
                tx,
                h("<b>Сообщение не доставлено</b>\n\n"
                  "Чат: {}\nТип: {}\nПричина: {}\n\n"
                  "Сообщение осталось в очереди со статусом «не доставлено»",
                  row["chat_id"], row["kind"], str(error)[:200]),
                dedup=f"failed:{row['id']}",
            )

    async def _enqueue_alert(self, tx, text: str, *, dedup: str) -> None:
        await notify.to_super_admins(self.db, tx, self.config, text,
                                     kind="alert", dedup=dedup)
