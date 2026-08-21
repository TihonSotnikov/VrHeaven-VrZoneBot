"""Постановка Записей в очередь доставки.

Записи ставятся в очередь внутри бизнес-транзакции: заказ и его
уведомления коммитятся вместе. Откатился заказ — уведомлений нет;
записался — доставка гарантирована, с повторами через перезапуски.

Ключ dedup_key делает повторную постановку безопасной: если хендлер
выполнится дважды (повтор callback, восстановление после сбоя),
получатель всё равно увидит одно сообщение.
"""

from config import Config
from db import Database, Tx
from roster import super_admin_ids


async def to_user(db: Database, tx: Tx, user, text: str, *, kind: str,
                  dedup: str | None = None, rich: bool = False,
                  document: dict | None = None) -> int:
    """Запись во все чаты кабинета пользователя. Возвращает число чатов."""
    chats = await db.chats_for_user(user["id"])
    for chat_id in chats:
        await db.enqueue_record(
            tx, chat_id=chat_id, kind=kind, text=text, rich=rich,
            document=document,
            dedup_key=f"{dedup}:chat:{chat_id}" if dedup else None,
        )
    return len(chats)


async def to_chat(db: Database, tx: Tx, chat_id: int, text: str, *, kind: str,
                  dedup: str | None = None, rich: bool = False,
                  document: dict | None = None) -> None:
    await db.enqueue_record(
        tx, chat_id=chat_id, kind=kind, text=text, rich=rich, document=document,
        dedup_key=f"{dedup}:chat:{chat_id}" if dedup else None,
    )


async def to_super_admins(db: Database, tx: Tx, config: Config, text: str, *,
                          kind: str, dedup: str | None = None,
                          rich: bool = False, document: dict | None = None,
                          exclude_tg_id: int | None = None) -> int:
    """Запись каждому супер-админу, кроме инициатора: он видит результат
    на собственном Окне и второй раз о нём читать не должен."""
    sent = 0
    for tg_id in await super_admin_ids(db, config):
        if tg_id == exclude_tg_id:
            continue
        await db.enqueue_record(
            tx, chat_id=tg_id, kind=kind, text=text, rich=rich, document=document,
            dedup_key=f"{dedup}:chat:{tg_id}" if dedup else None,
        )
        sent += 1
    return sent


def devices_note(count: int, who: str) -> str:
    """Строка о числе устройств получателя для экрана инициатора."""
    if count == 0:
        return f"{who} не подключил кабинет — уведомление не уйдёт"
    if count == 1:
        return f"{who} уведомлён"
    return f"{who} уведомлён · устройств: {count}"
