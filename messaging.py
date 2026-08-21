"""Сообщения бота: одно Окно и сколько угодно Записей.

Модель чата:

* **Окно** — единственное сообщение с кнопками. Меню, списки, карточки,
  шаги сценариев, экраны подтверждения. Правится на месте, при появлении
  новой Записи переставляется под неё. Ровно одно на чат.
* **Запись** — сообщение без кнопок, которое остаётся в чате навсегда:
  чек заказа, уведомление о заказе, отмена, бонус, выплата, отчёт,
  предупреждение о входе, резервная копия, выгрузка. Бот их не удаляет
  никогда.
* **Ввод пользователя** удаляется сразу после обработки.
* **Тост** — ответ на нажатие кнопки; всё сиюминутное живёт там.

Бот вызывает deleteMessage ровно для двух вещей: собственного Окна и
сообщения пользователя. Всё остальное в чате остаётся.

Порядок операций в перестановке окна обратный привычному: сначала
отправляем новое сообщение, и только потом удаляем прежнее. Если отправка
не удалась, в чате остаётся рабочее старое окно, а не пустота.
"""

import asyncio
import base64
import json
import logging
import os

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import SendRichMessage
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
)

from db import Database
from markup import CAPTION_LIMIT, TEXT_LIMIT, Report, clamp

log = logging.getLogger(__name__)


def document_payload(filename: str, data: bytes) -> dict:
    """Готовый к постановке в очередь документ из байтов в памяти."""
    return {"kind": "bytes", "filename": filename,
            "b64": base64.b64encode(data).decode()}


def file_payload(path: str, filename: str) -> dict:
    """Документ файлом на диске (резервная копия)."""
    return {"kind": "file", "path": path, "filename": filename}


class Messenger:
    """Единственная точка, откуда бот отправляет, правит и удаляет сообщения."""

    def __init__(self, bot: Bot, db: Database, *, rich: bool = True):
        self.bot = bot
        self.db = db
        # Нативные таблицы (Rich Messages) — свежая часть Bot API. Если
        # клиент или сервер их не принимает, бот сам переходит на
        # моноширинные таблицы и продолжает работать.
        self.rich_enabled = rich
        self._rich_failures = 0
        self._locks: dict[int, asyncio.Lock] = {}
        # Работник очереди подписывается сюда, чтобы Записи уходили сразу
        # после коммита, а не через секунду опроса
        self.on_enqueue = None

    def wake(self) -> None:
        """Сигнал очереди: транзакция с Записями закоммичена."""
        if self.on_enqueue is not None:
            self.on_enqueue()

    def chat_lock(self, chat_id: int) -> asyncio.Lock:
        """Все операции с сообщениями одного чата строго последовательны:
        две одновременные Записи не могут удалить одно и то же окно дважды."""
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = self._locks[chat_id] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------- Рендер

    def render(self, content) -> tuple[str, bool]:
        """Приводит содержимое к (html, нативная ли таблица)."""
        if isinstance(content, Report):
            if self.rich_enabled:
                return content.to_html(rich=True), True
            return clamp(content.to_html(rich=False)), False
        return clamp(str(content)), False

    def _demote_rich(self, error: Exception) -> None:
        self._rich_failures += 1
        if self.rich_enabled and self._rich_failures >= 3:
            self.rich_enabled = False
            log.error("Нативные таблицы отключены после трёх отказов: %s", error)

    async def _send(self, chat_id: int, text: str, markup, rich: bool) -> Message:
        if rich:
            return await self.bot(SendRichMessage(
                chat_id=chat_id,
                rich_message=InputRichMessage(html=text),
                reply_markup=markup,
            ))
        return await self.bot.send_message(chat_id, text, reply_markup=markup)

    async def _edit(self, chat_id: int, message_id: int, text: str,
                    markup, rich: bool) -> None:
        if rich:
            # Отдельного editRichMessage нет: содержимое передаётся
            # параметром rich_message обычного editMessageText
            await self.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                rich_message=InputRichMessage(html=text), reply_markup=markup,
            )
        else:
            await self.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=markup
            )

    # --------------------------------------------------------------- Окно

    async def window(self, chat_id: int, content, markup=None, *,
                     source_message_id: int | None = None) -> None:
        """Показывает Окно чата: правит текущее или ставит новое.

        source_message_id — сообщение, кнопку которого нажали. Нажатое
        сообщение не удаляется никогда: это может быть Запись. Если окно
        чата ещё не известно (первый запуск, потерянный указатель), нажатое
        сообщение усыновляется как окно.
        """
        markup_json = markup.model_dump_json() if markup is not None else None
        async with self.chat_lock(chat_id):
            current = await self.db.get_window(chat_id)
            target = current["message_id"] if current else source_message_id
            if target is not None:
                text, rich = self.render(content)
                try:
                    await self._edit(chat_id, target, text, markup, rich)
                    await self._store(chat_id, target, text, markup_json, rich)
                    return
                except TelegramBadRequest as e:
                    if "message is not modified" in str(e):
                        await self._store(chat_id, target, text, markup_json, rich)
                        return
                    if rich:
                        self._demote_rich(e)
                    # окно потеряно или нередактируемо — ставим новое ниже
            await self._anchor(chat_id, content, markup, markup_json, current)

    async def _anchor(self, chat_id: int, content, markup,
                      markup_json: str | None, current) -> None:
        """Ставит новое Окно и только после успеха убирает прежнее."""
        text, rich = self.render(content)
        try:
            message = await self._send(chat_id, text, markup, rich)
        except TelegramBadRequest as e:
            if not rich:
                raise
            self._demote_rich(e)
            self.rich_enabled = False
            text, rich = self.render(content)
            message = await self._send(chat_id, text, markup, rich)
        await self._store(chat_id, message.message_id, text, markup_json, rich)
        if current is not None and current["message_id"] != message.message_id:
            await self._delete(chat_id, current["message_id"])

    async def _store(self, chat_id: int, message_id: int, text: str,
                     markup_json: str | None, rich: bool) -> None:
        async with self.db.write() as tx:
            await self.db.save_window(tx, chat_id, message_id, text=text,
                                      markup_json=markup_json, rich=rich)

    async def _delete(self, chat_id: int, message_id: int) -> None:
        try:
            await self.bot.delete_message(chat_id, message_id)
        except TelegramAPIError as e:
            log.info("Окно %s чата %s уже недоступно: %s", message_id, chat_id, e)

    async def reanchor(self, chat_id: int) -> None:
        """Переставляет Окно под свежие Записи, сохраняя его содержимое.

        Это и есть решение проблемы «пуш съел экран оплаты»: уведомление
        приходит, экран с кнопкой «Оплата получена» появляется под ним
        нетронутым, данные сценария не задеты.
        """
        async with self.chat_lock(chat_id):
            current = await self.db.get_window(chat_id)
            if current is None:
                return
            if not current["text"]:
                # Окно, доставшееся от прежней версии бота: содержимого нет,
                # воспроизвести нечем — убираем, следующий экран станет новым
                await self._delete(chat_id, current["message_id"])
                async with self.db.write() as tx:
                    await self.db.drop_window(tx, chat_id)
                return
            markup = None
            if current["markup_json"]:
                markup = InlineKeyboardMarkup.model_validate_json(current["markup_json"])
            try:
                message = await self._send(chat_id, current["text"], markup,
                                           bool(current["is_rich"]))
            except TelegramAPIError as e:
                # Не удалось — прежнее окно остаётся рабочим, ничего не теряем
                log.warning("Окно чата %s не переставлено: %s", chat_id, e)
                return
            await self._store(chat_id, message.message_id, current["text"],
                              current["markup_json"], bool(current["is_rich"]))
            await self._delete(chat_id, current["message_id"])

    # ------------------------------------------------------------- Записи

    async def send_record(self, chat_id: int, payload: dict) -> int:
        """Отправляет Запись — сообщение без кнопок, остающееся навсегда."""
        document = payload.get("document")
        if document:
            return await self._send_document(chat_id, payload, document)
        text = clamp(payload["text"])
        rich = bool(payload.get("rich")) and self.rich_enabled
        try:
            message = await self._send(chat_id, text, None, rich)
        except TelegramBadRequest as e:
            if not rich:
                raise
            self._demote_rich(e)
            message = await self._send(chat_id, text, None, False)
        return message.message_id

    async def _send_document(self, chat_id: int, payload: dict, document: dict) -> int:
        if document["kind"] == "file":
            if not os.path.exists(document["path"]):
                raise FileNotFoundError(document["path"])
            file = FSInputFile(document["path"], filename=document["filename"])
        else:
            file = BufferedInputFile(base64.b64decode(document["b64"]),
                                     filename=document["filename"])
        message = await self.bot.send_document(
            chat_id, file, caption=clamp(payload.get("text", ""), CAPTION_LIMIT)
        )
        return message.message_id

    async def drop_user_message(self, message: Message) -> None:
        """Ввод пользователя (включая пароль) не задерживается в чате."""
        try:
            await message.delete()
        except TelegramAPIError:
            pass


def payload_of(row) -> dict:
    return json.loads(row["payload"])


__all__ = [
    "CAPTION_LIMIT",
    "TEXT_LIMIT",
    "Messenger",
    "document_payload",
    "file_payload",
    "payload_of",
]
