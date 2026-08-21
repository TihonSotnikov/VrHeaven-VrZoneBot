"""Состояние сценариев (FSM) в той же базе SQLite.

Оперативная память для этого не годится: перезапуск бота — а он случается
при каждом обновлении — уносил бы незавершённые заказы вместе с ценой,
уже названной клиенту. Здесь состояние переживает рестарт, и следующее
нажатие администратора срабатывает так, как будто ничего не произошло.

Работает в паре с SimpleEventIsolation в main.py: update_data — это
чтение с последующей записью, и без сериализации обновлений по чату два
одновременных апдейта затирали бы друг друга.
"""

import json
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from db import Database
from utils import utcnow_iso

# Брошенные сценарии не должны копиться
STATE_TTL_DAYS = 7


class SQLiteStorage(BaseStorage):
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _key(key: StorageKey) -> tuple:
        return (key.bot_id, key.chat_id, key.user_id, key.thread_id or 0,
                key.business_connection_id or "", key.destiny)

    async def _row(self, key: StorageKey):
        return await self.db.fetchone(
            "SELECT state, data FROM fsm_state WHERE bot_id = ? AND chat_id = ?"
            " AND user_id = ? AND thread_id = ? AND business = ? AND destiny = ?",
            self._key(key),
        )

    async def _upsert(self, key: StorageKey, *, state=..., data=...) -> None:
        row = await self._row(key)
        current_state = row["state"] if row else None
        current_data = row["data"] if row else "{}"
        new_state = current_state if state is ... else state
        new_data = current_data if data is ... else data
        async with self.db.write() as tx:
            await tx.execute(
                "INSERT INTO fsm_state (bot_id, chat_id, user_id, thread_id,"
                " business, destiny, state, data, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(bot_id, chat_id, user_id, thread_id, business, destiny)"
                " DO UPDATE SET state = excluded.state, data = excluded.data,"
                " updated_at = excluded.updated_at",
                (*self._key(key), new_state, new_data, utcnow_iso()),
            )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        if value is None:
            row = await self._row(key)
            if row is not None and row["data"] in ("{}", ""):
                async with self.db.write() as tx:
                    await tx.execute(
                        "DELETE FROM fsm_state WHERE bot_id = ? AND chat_id = ?"
                        " AND user_id = ? AND thread_id = ? AND business = ?"
                        " AND destiny = ?",
                        self._key(key),
                    )
                return
        await self._upsert(key, state=value)

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._row(key)
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            raise TypeError(f"Данные сценария должны быть словарём, получено {type(data)}")
        # Несериализуемое значение должно падать здесь и сейчас, а не
        # теряться молча при следующем чтении
        await self._upsert(key, data=json.dumps(data, ensure_ascii=False))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._row(key)
        if not row or not row["data"]:
            return {}
        return json.loads(row["data"])

    async def cleanup(self, before_iso: str) -> int:
        async with self.db.write() as tx:
            cur = await tx.execute(
                "DELETE FROM fsm_state WHERE updated_at < ?", (before_iso,)
            )
            return cur.rowcount

    async def close(self) -> None:
        return None
