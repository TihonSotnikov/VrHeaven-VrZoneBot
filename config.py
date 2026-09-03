"""Конфигурация из окружения.

Секрет читается тремя способами, в порядке убывания предпочтительности:
учётные данные systemd (`LoadCredential=bot_token:...`), файл из
BOT_TOKEN_FILE, переменная BOT_TOKEN. На сервере используется первый —
токен не лежит в окружении процесса и не виден в /proc.
"""

import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    tz: ZoneInfo
    db_path: str
    backup_dir: str = "backups"
    data_dir: str = "."
    backup_keep_days: int = 7
    backup_keep_weeks: int = 8
    backup_keep_months: int = 12
    backup_offhost_cmd: str = ""
    log_json: bool = False
    log_level: str = "INFO"
    env_name: str = "prod"
    guides_dir: str = field(default="guides")
    # Группа супер-админов и тема в ней. Не заданы — панель живёт только
    # в личных чатах, как и было
    superadmin_chat_id: int | None = None
    superadmin_topic_id: int | None = None

    def is_panel_group(self, chat_id: int | None) -> bool:
        """Групповой чат супер-админов — любая его тема."""
        return (self.superadmin_chat_id is not None
                and chat_id == self.superadmin_chat_id)

    def in_panel_topic(self, chat_id: int | None, thread_id: int | None) -> bool:
        """Настроенная тема — единственное место в группе, где бот отвечает.

        Приватность бота держится на этом: в остальных темах он получает
        каждое сообщение (privacy mode выключен) и не говорит ни слова.
        """
        return (self.is_panel_group(chat_id)
                and thread_id == self.superadmin_topic_id)

    def panel_thread_id(self, chat_id: int) -> int | None:
        """Тема, в которую уходит сообщение этого чата; для остальных — None.

        Без message_thread_id сообщение в форуме падает в «General», а не
        в тему панели. Тема выводится из конфигурации по chat_id — поэтому
        её не нужно ни хранить в очереди Записей, ни тащить через схему.
        """
        return self.superadmin_topic_id if self.is_panel_group(chat_id) else None


def _read_token() -> str:
    credentials = os.getenv("CREDENTIALS_DIRECTORY")
    if credentials:
        path = os.path.join(credentials, "bot_token")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
    token_file = os.getenv("BOT_TOKEN_FILE", "").strip()
    if token_file:
        if not os.path.exists(token_file):
            raise RuntimeError(
                f"BOT_TOKEN_FILE указывает на несуществующий файл: {token_file}")
        with open(token_file, encoding="utf-8") as handle:
            return handle.read().strip()
    return os.getenv("BOT_TOKEN", "").strip()


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise RuntimeError(f"{name} должен быть целым числом") from e
    if value < minimum:
        raise RuntimeError(f"{name} должен быть не меньше {minimum}")
    return value


def _opt_int_env(name: str) -> int | None:
    """Необязательное целое: не задано — None, задано мусором — отказ старта.

    Отдельно от _int_env: у id чата нет ни значения по умолчанию, ни
    нижней границы — id группы отрицателен.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"{name} должен быть целым числом") from e


def load_config() -> Config:
    load_dotenv()

    token = _read_token()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан — заполните .env (см. .env.example)")

    raw_ids = os.getenv("ADMIN_IDS", "").strip()
    try:
        admin_ids = frozenset(int(x) for x in re.split(r"[,\s]+", raw_ids) if x)
    except ValueError as e:
        raise RuntimeError(
            "ADMIN_IDS должен содержать числовые Telegram ID через запятую") from e
    if not admin_ids:
        raise RuntimeError("ADMIN_IDS не задан — заполните .env (см. .env.example)")

    tz_name = os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:
        raise RuntimeError(f"Неизвестный часовой пояс: {tz_name}") from e

    db_path = os.getenv("DB_PATH", "adminbot.db").strip() or "adminbot.db"
    data_dir = (os.getenv("DATA_DIR", "").strip()
                or os.path.dirname(os.path.abspath(db_path)))
    backup_dir = (os.getenv("BACKUP_DIR", "").strip()
                  or os.path.join(data_dir, "backups"))

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        tz=tz,
        db_path=db_path,
        data_dir=data_dir,
        backup_dir=backup_dir,
        backup_keep_days=_int_env("BACKUP_KEEP_DAYS", 7),
        backup_keep_weeks=_int_env("BACKUP_KEEP_WEEKS", 8),
        backup_keep_months=_int_env("BACKUP_KEEP_MONTHS", 12),
        backup_offhost_cmd=os.getenv("BACKUP_OFFHOST_CMD", "").strip(),
        log_json=os.getenv("LOG_JSON", "").strip().lower() in {"1", "true", "yes"},
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        env_name=os.getenv("ENV", "prod").strip() or "prod",
        superadmin_chat_id=_opt_int_env("SUPERADMIN_CHAT_ID"),
        superadmin_topic_id=_opt_int_env("SUPERADMIN_TOPIC_ID"),
    )
