"""Панель VR Heaven в теме группы супер-админов.

Трое делят одно Окно в одной теме форума и удаляют личные чаты с ботом.
Privacy mode выключен: боту видно каждое сообщение каждой темы этой
группы, поэтому молчание вне своей темы — не мелочь, а требование.

Тема нигде не хранится: она выводится из конфигурации по chat_id, и
проверять её приходится в единственном месте — на самой отправке.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User
from conftest import VR_ADMIN_IDS
from helpers import create_admin, drain

import notify
from delivery import CHAT_RATE, GROUP_CHAT_RATE, ThrottleMiddleware
from fsm_storage import SQLiteStorage
from handlers import common, staff, vrheaven
from handlers.common import GROUP_TEXT
from messaging import Messenger
from outbox import OutboxWorker

GROUP = -1003779364996
TOPIC = 539
OTHER_TOPIC = 12
OTHER_GROUP = -100999
VR_ID = 999
STRANGER = 4242


@pytest.fixture
def group_config(config):
    return replace(config, superadmin_chat_id=GROUP, superadmin_topic_id=TOPIC)


@pytest.fixture
def group_ui(bot, db, group_config):
    return Messenger(bot, db, config=group_config)


@pytest.fixture
def group_worker(bot, db, group_ui, group_config):
    instance = OutboxWorker(bot, db, group_ui, group_config)
    group_ui.on_enqueue = instance.wake
    return instance


def _dispatcher(db, ui, config):
    # Роутеры — модульные объекты: отцепляем их от диспетчера прошлого теста
    for router in (common.guard_router, vrheaven.router, staff.router):
        router._parent_router = None
    dp = Dispatcher(storage=SQLiteStorage(db), events_isolation=SimpleEventIsolation())
    dp["db"] = db
    dp["ui"] = ui
    dp["config"] = config
    dp.include_router(common.guard_router)
    dp.include_router(vrheaven.router)
    dp.include_router(staff.router)
    return dp


def _update(text: str, *, chat_id: int = GROUP, thread_id: int | None = TOPIC,
            from_id: int = VR_ID, update_id: int = 1,
            chat_type: str = "supergroup") -> Update:
    return Update(update_id=update_id, message=Message(
        message_id=update_id + 500, date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type), message_thread_id=thread_id,
        from_user=User(id=from_id, is_bot=False, first_name="Тест"), text=text))


# ------------------------------------------------------------- Маршрутизация

async def test_start_in_the_configured_topic_opens_the_panel(
        db, bot, group_ui, group_config):
    dp = _dispatcher(db, group_ui, group_config)
    await dp.feed_update(bot, _update("/start"))

    assert "Панель VR Heaven" in bot.texts(GROUP)[0]
    assert await db.get_window(GROUP) is not None
    # без message_thread_id Окно ушло бы в «General», а не в тему панели
    assert bot.threads(GROUP) == [TOPIC]


async def test_the_bot_stays_silent_in_every_other_topic_of_the_group(
        db, bot, group_ui, group_config):
    dp = _dispatcher(db, group_ui, group_config)
    await dp.feed_update(bot, _update("привет", thread_id=OTHER_TOPIC))
    await dp.feed_update(bot, _update("/start", thread_id=None, update_id=2))

    assert bot.sent == [] and bot.answers == []
    assert await db.get_window(GROUP) is None


async def test_a_stranger_in_the_panel_topic_gets_nothing(
        db, bot, group_ui, group_config):
    """Фильтр супер-админа не ослаблен темой: чужой в ней получает то же
    молчание, что и в любой другой."""
    await create_admin(db, handle="chuzhoy")
    dp = _dispatcher(db, group_ui, group_config)
    await dp.feed_update(bot, _update("/start", from_id=STRANGER))

    assert bot.sent == [] and bot.answers == []


async def test_other_groups_still_get_the_refusal(db, bot, group_ui, group_config):
    dp = _dispatcher(db, group_ui, group_config)
    await dp.feed_update(bot, _update("/start", chat_id=OTHER_GROUP,
                                      thread_id=None))
    assert bot.texts(OTHER_GROUP) == [GROUP_TEXT]


async def test_without_the_setting_the_group_is_refused_as_before(db, bot, ui, config):
    """Не задана группа — поведение сегодняшнее: только личные чаты."""
    dp = _dispatcher(db, ui, config)
    await dp.feed_update(bot, _update("/start"))
    assert bot.texts(GROUP) == [GROUP_TEXT]
    assert await db.get_window(GROUP) is None


async def test_the_private_panel_keeps_working_as_a_fallback(
        db, bot, group_ui, group_config):
    """Личный чат остаётся запасным входом: группа его не отменяет."""
    dp = _dispatcher(db, group_ui, group_config)
    await dp.feed_update(bot, _update("/start", chat_id=VR_ID, thread_id=None,
                                      chat_type="private"))

    assert "Панель VR Heaven" in bot.texts(VR_ID)[0]
    assert bot.threads(VR_ID) == [None]     # в личном чате темы нет


# ------------------------------------------------------- Записи и документы

async def test_super_admin_records_go_to_the_topic_once(
        db, bot, group_ui, group_config, group_worker):
    """Одна Запись в общую тему вместо трёх в личные чаты — ради этого
    группа и заводится: личные чаты супер-админы удаляют."""
    async with db.write() as tx:
        sent = await notify.to_super_admins(db, tx, group_config, "тревога",
                                            kind="alert", dedup="test:1")
    assert sent == 1
    await drain(group_worker)
    assert bot.texts(GROUP) == ["тревога"]
    assert bot.threads(GROUP) == [TOPIC]
    for tg_id in VR_ADMIN_IDS:
        assert bot.texts(tg_id) == []


async def test_documents_and_reanchoring_carry_the_topic(
        db, bot, group_ui, group_config, group_worker):
    """Документ и переставленное Окно — такие же сообщения в тему: без
    message_thread_id они разъехались бы по форуму."""
    from messaging import document_payload
    async with db.write() as tx:
        await db.save_window(tx, GROUP, 77, text="панель", markup_json=None,
                             rich=False)
        await notify.to_chat(db, tx, GROUP, "копия базы", kind="backup",
                             document=document_payload("dump.db", b"x"))
    await drain(group_worker)

    assert [chat for chat, _, _, _ in bot.documents] == [GROUP]
    # документ и переставленное следом Окно — оба в теме панели
    assert bot.threads(GROUP) == [TOPIC, TOPIC]


# ---------------------------------------------------------------- Транспорт

async def test_the_group_chat_is_paced_slower_than_a_private_one():
    """Групповой чат Telegram ограничивает жёстче личного, а вся переписка
    панели сходится теперь в один такой чат."""
    assert GROUP_CHAT_RATE < CHAT_RATE

    middleware = ThrottleMiddleware(global_rate=1000, chat_rate=1000, group_rate=20)

    async def make_request(bot, method):
        return "ok"

    async def burst(chat_id: int) -> float:
        started = asyncio.get_running_loop().time()
        for _ in range(10):
            await middleware(make_request, None, SendMessage(chat_id=chat_id, text="x"))
        return asyncio.get_running_loop().time() - started

    assert await burst(GROUP) > await burst(VR_ID)
