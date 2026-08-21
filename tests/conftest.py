import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from zoneinfo import ZoneInfo

import pytest
from helpers import FakeBot

from config import Config
from db import Database
from messaging import Messenger
from outbox import OutboxWorker
from utils import hash_password

VR_ADMIN_IDS = frozenset({999, 998})


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def password_hash():
    return hash_password("secret")


@pytest.fixture
def config(tmp_path):
    """Каталоги данных — во временной папке теста: рабочий каталог чист."""
    return Config(
        bot_token="test-token",
        admin_ids=VR_ADMIN_IDS,
        tz=ZoneInfo("Europe/Moscow"),
        db_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path),
        backup_dir=str(tmp_path / "backups"),
        guides_dir=str(Path(__file__).parent.parent / "guides"),
    )


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def ui(bot, db):
    return Messenger(bot, db)


@pytest.fixture
def worker(bot, db, ui, config):
    """Работник очереди Записей; тесты сливают очередь helpers.drain()."""
    instance = OutboxWorker(bot, db, ui, config)
    ui.on_enqueue = instance.wake
    return instance
