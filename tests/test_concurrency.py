"""Настоящий диспетчер, настоящие одновременные апдейты.

Прежний тест «двойного нажатия» моделировал диспетчер и утверждал
поведение, которого у настоящего диспетчера нет — то есть подтверждал
ровно ту ошибку, от которой должен был защищать. Здесь апдейты идут
через собранный Dispatcher с настоящими роутерами, настоящим хранилищем
состояния и настоящей изоляцией событий.
"""

import asyncio
from datetime import UTC, datetime

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from helpers import bind, create_admin, create_owner, drain, set_window

from fsm_storage import SQLiteStorage
from handlers import common, staff, vrheaven

ADMIN_CHAT = 1
OWNER_CHAT = 201
VR_CHAT = 999
WINDOW = 50


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


def _callback(data: str, *, chat_id: int, update_id: int, message_id: int = WINDOW):
    user = User(id=chat_id, is_bot=False, first_name="Тест")
    chat = Chat(id=chat_id, type="private")
    message = Message(message_id=message_id, date=datetime.now(UTC),
                      chat=chat, from_user=User(id=9, is_bot=True, first_name="Бот"))
    query = CallbackQuery(id=str(update_id), from_user=user, chat_instance="ci",
                          message=message, data=data)
    return Update(update_id=update_id, callback_query=query)


def _message(text: str, *, chat_id: int, update_id: int):
    user = User(id=chat_id, is_bot=False, first_name="Тест")
    chat = Chat(id=chat_id, type="private")
    return Update(update_id=update_id,
                  message=Message(message_id=update_id + 500,
                                  date=datetime.now(UTC), chat=chat,
                                  from_user=user, text=text))


async def _club(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await bind(db, admin, ADMIN_CHAT)
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, ADMIN_CHAT, WINDOW)
    async with db.write() as tx:
        await db.set_setting(tx, "discount_enabled", 0)
    return owner, admin


async def test_double_tap_on_payment_records_one_order(db, ui, config, bot, worker):
    """Два одновременных «Оплата получена» — один заказ и один чек."""
    await _club(db)
    dp = _dispatcher(db, ui, config)
    await dp.feed_update(bot, _callback("no:d:1:30", chat_id=ADMIN_CHAT, update_id=1))
    await asyncio.gather(
        dp.feed_update(bot, _callback("no:ok", chat_id=ADMIN_CHAT, update_id=2)),
        dp.feed_update(bot, _callback("no:ok", chat_id=ADMIN_CHAT, update_id=3)),
    )
    assert len(await db.export_orders()) == 1
    await drain(worker)
    receipts = [t for t in bot.records(ADMIN_CHAT) if t.startswith("<b>Заказ №1</b>")]
    assert len(receipts) == 1
    assert bot.other_calls == []                     # ни одной несостоявшейся отправки
    assert len(bot.answers) == 3                     # каждое нажатие получило ответ


async def test_two_devices_of_one_account_get_distinct_ladder_steps(
        db, ui, config, bot, worker):
    """Мульти-аккаунт — штатный режим: смена и подмена в одном кабинете."""
    owner, admin = await _club(db)
    second_chat = 2
    await bind(db, admin, second_chat)
    await set_window(db, second_chat, 60)
    dp = _dispatcher(db, ui, config)
    await asyncio.gather(
        dp.feed_update(bot, _callback("no:d:1:30", chat_id=ADMIN_CHAT, update_id=1)),
        dp.feed_update(bot, _callback("no:d:1:30", chat_id=second_chat,
                                      update_id=2, message_id=60)),
    )
    await asyncio.gather(
        dp.feed_update(bot, _callback("no:ok", chat_id=ADMIN_CHAT, update_id=3)),
        dp.feed_update(bot, _callback("no:ok", chat_id=second_chat, update_id=4,
                                      message_id=60)),
    )
    orders = await db.export_orders()
    assert sorted(o["series_pos"] for o in orders) == [1, 2]
    assert sorted(o["admin_share"] for o in orders) == [50.0, 100.0]


async def test_concurrent_payout_confirmations_yield_one_payout(
        db, ui, config, bot, worker):
    owner, admin = await _club(db)
    from helpers import make_order
    await make_order(db, admin, owner, price=300)
    await set_window(db, VR_CHAT, WINDOW)
    dp = _dispatcher(db, ui, config)
    await asyncio.gather(
        dp.feed_update(bot, _callback(f"po:ok:{owner['id']}", chat_id=VR_CHAT,
                                      update_id=1)),
        dp.feed_update(bot, _callback(f"po:ok:{owner['id']}", chat_id=VR_CHAT,
                                      update_id=2)),
    )
    assert len(await db.payouts_for_user(owner["id"])) == 1


async def test_concurrent_cancel_and_payout_stay_consistent(db, ui, config, bot):
    owner, admin = await _club(db)
    from helpers import make_order
    order_id = await make_order(db, admin, owner, price=300)
    await set_window(db, VR_CHAT, WINDOW)
    dp = _dispatcher(db, ui, config)
    await asyncio.gather(
        dp.feed_update(bot, _callback(f"sc:ok:{order_id}", chat_id=ADMIN_CHAT,
                                      update_id=1)),
        dp.feed_update(bot, _callback(f"po:ok:{admin['id']}", chat_id=VR_CHAT,
                                      update_id=2)),
    )
    order = await db.get_order(order_id)
    if order["cancelled_at"] is not None:
        assert order["admin_payout_id"] is None      # отменённое не выплачивается
    else:
        assert order["admin_payout_id"] is not None


async def test_two_records_to_one_chat_leave_a_single_window(
        db, ui, config, bot, worker):
    """Две одновременные Записи не могут удалить одно окно дважды."""
    import notify
    await _club(db)
    async with db.write() as tx:
        await notify.to_chat(db, tx, ADMIN_CHAT, "Первая", kind="test",
                             dedup="a")
        await notify.to_chat(db, tx, ADMIN_CHAT, "Вторая", kind="test",
                             dedup="b")
    await drain(worker)
    assert len(bot.interactive(ADMIN_CHAT)) == 1


async def test_malformed_callback_never_escapes_as_an_exception(
        db, ui, config, bot):
    """Самодельный клиент может прислать что угодно — кнопка обязана
    ответить, а не оставить спиннер."""
    await _club(db)
    dp = _dispatcher(db, ui, config)
    for index, data in enumerate([
        "ad:card:abc", "ad:card:", "ac:o:99999999999999999999",
        "sc:o:", "no:p:1:30:x", "po:ok:-", "st:set:неизвестно", "пусто",
    ], start=1):
        await dp.feed_update(bot, _callback(data, chat_id=VR_CHAT, update_id=index))
    assert len(bot.answers) == 8


async def test_group_chat_is_refused_with_an_explanation(db, ui, config, bot):
    dp = _dispatcher(db, ui, config)
    chat = Chat(id=-100500, type="supergroup")
    user = User(id=VR_CHAT, is_bot=False, first_name="Тест")
    update = Update(update_id=1, message=Message(
        message_id=1, date=datetime.now(UTC), chat=chat,
        from_user=user, text="/start"))
    await dp.feed_update(bot, update)
    assert bot.other_calls == ["SendMessage"] or bot.sent
    assert await db.get_window(-100500) is None
