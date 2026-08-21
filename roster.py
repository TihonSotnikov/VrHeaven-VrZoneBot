"""Список супер-админов VR Heaven.

Действующий список = ADMIN_IDS из окружения ∪ активные записи в таблице
super_admins. ADMIN_IDS — механизм восстановления доступа: эти id нельзя
удалить из бота, поэтому потерять управление собственным журналом
невозможно. Всех остальных супер-админов VR Heaven заводит и убирает
прямо в боте, каждое изменение попадает в журнал действий.
"""

from config import Config
from db import Database


async def super_admin_ids(db: Database, config: Config) -> list[int]:
    rows = await db.list_super_admins()
    return sorted(set(config.admin_ids) | {row["tg_id"] for row in rows})


def is_bootstrap(config: Config, tg_id: int) -> bool:
    """Супер-админ из .env: доступ восстанавливается через сервер,
    из бота такая запись не убирается."""
    return tg_id in config.admin_ids


async def is_super_admin(db: Database, config: Config, tg_id: int) -> bool:
    if tg_id in config.admin_ids:
        return True
    rows = await db.list_super_admins()
    return any(row["tg_id"] == tg_id for row in rows)
