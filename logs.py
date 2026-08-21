"""Журналирование с контекстом: у каждой строки есть адрес.

Строка «не удалось отправить сообщение» без чата и без действия ничего не
стоит. Каждый апдейт получает request_id; он попадает в строки журнала,
в записи audit_log и в очередь Записей, поэтому по одному идентификатору
собирается вся история одного нажатия.
"""

import json
import logging
import uuid
from contextvars import ContextVar

from aiogram import BaseMiddleware

request_id: ContextVar[str] = ContextVar("request_id", default="-")
chat_id_var: ContextVar[int | None] = ContextVar("chat_id", default=None)
actor_var: ContextVar[str] = ContextVar("actor", default="-")

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName"}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        record.chat_id = chat_id_var.get()
        record.actor = actor_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "chat_id": getattr(record, "chat_id", None),
            "actor": getattr(record, "actor", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(*, json_output: bool = False, level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(request_id)s chat=%(chat_id)s"
            " %(actor)s]: %(message)s"
        ))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Строка «Update id=... is handled» в журнале за каждое нажатие только
    # мешает искать настоящие события
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


class ContextMiddleware(BaseMiddleware):
    """Проставляет request_id и адрес чата на время обработки апдейта."""

    async def __call__(self, handler, event, data):
        token_id = request_id.set(uuid.uuid4().hex[:12])
        chat = data.get("event_chat")
        token_chat = chat_id_var.set(chat.id if chat else None)
        user = data.get("event_from_user")
        token_actor = actor_var.set(str(user.id) if user else "-")
        data["request_id"] = request_id.get()
        try:
            return await handler(event, data)
        finally:
            request_id.reset(token_id)
            chat_id_var.reset(token_chat)
            actor_var.reset(token_actor)
