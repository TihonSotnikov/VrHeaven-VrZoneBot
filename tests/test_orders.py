"""Оформление заказа: цена, лесенка, чек, уведомления и защита экрана."""

from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import VR_ADMIN_IDS
from helpers import (
    bind,
    button_texts,
    create_admin,
    create_owner,
    drain,
    fake_cb,
    make_state,
    set_window,
    window_text,
)

from handlers.staff import (
    NewOrderSG,
    order_drop,
    order_headsets,
    order_new,
    order_payment,
    order_promo,
    order_std,
)
from pricing import ladder_amount

ADMIN_CHAT = 1
OWNER_CHAT = 201
WINDOW = 50


async def _confirm_screen(db, ui, config, state, *, chat=ADMIN_CHAT):
    from handlers.staff import order_duration
    await order_duration(fake_cb("no:d:1:30", chat_id=chat, message_id=WINDOW),
                         state, db, ui, config)


async def _place_order(db, ui, config, state, *, chat=ADMIN_CHAT):
    await _confirm_screen(db, ui, config, state, chat=chat)
    await order_payment(fake_cb("no:ok", chat_id=chat, message_id=WINDOW),
                        state, db, ui, config)


async def _admin_with_owner(db, *, percent=30, discount=False):
    """Клуб с владельцем и администратором.

    Скидка по умолчанию выключена: иначе цена зависела бы от того, в какой
    день и час запускают тесты.
    """
    owner = await create_owner(db, percent=percent)
    admin = await create_admin(db, owner)
    await bind(db, admin, ADMIN_CHAT)
    await bind(db, owner, OWNER_CHAT)
    async with db.write() as tx:
        await db.set_setting(tx, "discount_enabled", int(discount))
    return owner, admin


async def test_order_kind_screen_is_skipped_without_promos(db, ui, config, bot):
    """Экран с единственным вариантом — чистое трение на каждом заказе."""
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_new(fake_cb("no:new", message_id=WINDOW), state, db, ui)
    assert "Сколько шлемов" in await window_text(db, ADMIN_CHAT)


async def test_order_kind_screen_appears_when_promos_exist(db, ui, config):
    await _admin_with_owner(db)
    async with db.write() as tx:
        await db.create_promo(tx, "День рождения", 700)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_new(fake_cb("no:new", message_id=WINDOW), state, db, ui)
    text = await window_text(db, ADMIN_CHAT)
    assert "Выберите тип заказа" in text
    buttons = await button_texts(db, ADMIN_CHAT)
    assert any("День рождения" in b for b in buttons)


async def test_headsets_screen_has_back_only_when_there_is_where_to_go(db, ui):
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_std(fake_cb("no:std", message_id=WINDOW), state, db, ui)
    assert "Назад" not in await button_texts(db, ADMIN_CHAT)
    async with db.write() as tx:
        await db.create_promo(tx, "3=4", 600)
    await order_std(fake_cb("no:std", message_id=WINDOW), state, db, ui)
    assert "Назад" in await button_texts(db, ADMIN_CHAT)


async def test_standard_order_records_shares_and_receipt(db, ui, config, bot, worker):
    owner, admin = await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_order(db, ui, config, state)

    order = await db.get_order(1)
    assert order["price"] == 300 and order["series_pos"] == 1
    assert order["admin_share"] == ladder_amount(1)
    assert order["owner_share"] == 90.0
    assert order["client_token"] and order["quoted_at"]

    await drain(worker)
    receipts = [t for c, _, t in bot.sent
                if c == ADMIN_CHAT and t.startswith("<b>Заказ №1</b>")]
    assert receipts and "300 ₽" in receipts[0]
    # чек не показывает вознаграждение: экран держат перед клиентом
    assert "вознаграждение" not in receipts[0].lower()


async def test_admin_sees_earned_and_next_step(db, ui, config):
    """Требование продукта: после заказа видно, сколько заработал сейчас
    и сколько будет за следующий заказ серии."""
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_order(db, ui, config, state)
    text = await window_text(db, ADMIN_CHAT)
    assert "Ваше вознаграждение: 50 ₽" in text
    assert "Следующий заказ серии: 100 ₽" in text
    assert "Отменить заказ №1" in await button_texts(db, ADMIN_CHAT)


async def test_owner_and_super_admins_get_records(db, ui, config, bot, worker):
    owner, admin = await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_order(db, ui, config, state)
    await drain(worker)

    owner_texts = [t for c, _, t in bot.sent if c == OWNER_CHAT]
    assert owner_texts and "Ваша доля: 90 ₽" in owner_texts[0]
    assert "Остаток" not in owner_texts[0]        # владельцу — только его доля

    for admin_id in VR_ADMIN_IDS:
        texts = [t for c, _, t in bot.sent if c == admin_id]
        assert texts and "Остаток VR Heaven: 160 ₽" in texts[0]


async def test_negative_remainder_notification_is_delivered(db, ui, config, bot, worker):
    """Дешёвый заказ на пятой ступени даёт отрицательный остаток —
    именно на этом раньше молча ломались уведомления супер-админам."""
    owner, admin = await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    for _ in range(5):
        await _place_order(db, ui, config, state)
    await drain(worker)
    order = await db.get_order(5)
    remainder = round(order["price"] - order["admin_share"] - order["owner_share"], 2)
    assert remainder < 0
    texts = [t for c, _, t in bot.sent if c in VR_ADMIN_IDS and "№5" in t]
    assert texts and "−" in texts[0]


async def test_ladder_progresses_and_promo_counts_as_a_regular_order(db, ui, config):
    owner, admin = await _admin_with_owner(db)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "3=4", 600)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_order(db, ui, config, state)
    await order_promo(fake_cb(f"no:promo:{promo_id}", message_id=WINDOW),
                      state, db, ui)
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)
    await _place_order(db, ui, config, state)

    shares = [o["admin_share"] for o in await db.export_orders()]
    assert shares == [50.0, 100.0, 150.0]
    promo_order = await db.get_order(2)
    assert promo_order["kind"] == "promo" and promo_order["promo_name"] == "3=4"
    assert promo_order["discount_percent"] == 0


async def test_double_tap_records_one_order_and_one_receipt(db, ui, config, bot, worker):
    """Повторное нажатие «Оплата получена» не может удвоить продажу."""
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _confirm_screen(db, ui, config, state)
    data = await state.get_data()
    cb = fake_cb("no:ok", message_id=WINDOW)
    await order_payment(cb, state, db, ui, config)
    # второе нажатие приходит с тем же ключом оформления
    await state.set_state(NewOrderSG.confirm)
    await state.set_data(data)
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)

    assert len(await db.export_orders()) == 1
    await drain(worker)
    receipts = [t for c, _, t in bot.sent
                if c == ADMIN_CHAT and t.startswith("<b>Заказ №1</b>")]
    assert len(receipts) == 1


async def test_lost_state_does_not_record_a_silent_order(db, ui, config):
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await state.set_state(NewOrderSG.confirm)     # состояние без данных
    cb = fake_cb("no:ok", message_id=WINDOW)
    await order_payment(cb, state, db, ui, config)
    assert await db.export_orders() == []
    cb.answer.assert_awaited()
    assert "не записан" in cb.answer.await_args.args[0]


async def test_state_survives_restart(db, ui, config):
    """Состояние живёт в базе: перезапуск не уносит незавершённый заказ."""
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    await _confirm_screen(db, ui, config, make_state(db, ADMIN_CHAT))
    fresh = make_state(db, ADMIN_CHAT)            # как после перезапуска
    assert await fresh.get_state() == NewOrderSG.confirm.state
    await order_payment(fake_cb("no:ok", message_id=WINDOW), fresh, db, ui, config)
    assert len(await db.export_orders()) == 1


async def test_explicit_drop_does_not_record_anything(db, ui, config):
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _confirm_screen(db, ui, config, state)
    await order_drop(fake_cb("no:drop", message_id=WINDOW), state, db, ui)
    assert await db.export_orders() == []
    assert "не оформлен" in await window_text(db, ADMIN_CHAT)
    assert await state.get_state() is None


async def test_malformed_callbacks_cannot_affect_price(db, ui, config):
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    for data in ("no:h:9", "no:h:abc", "no:h:", "no:d:1:45",
                 "no:d:99999999999999999999:30"):
        cb = fake_cb(data, message_id=WINDOW)
        if data.startswith("no:h"):
            await order_headsets(cb, state, db, ui)
        else:
            from handlers.staff import order_duration
            await order_duration(cb, state, db, ui, config)
        cb.answer.assert_awaited()
    assert await state.get_state() is None
    assert await db.export_orders() == []


async def test_suspended_admin_cannot_start_an_order(db, ui, config):
    owner, admin = await _admin_with_owner(db)
    async with db.write() as tx:
        await db.set_user_active(tx, admin["id"], False)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_new(fake_cb("no:new", message_id=WINDOW), state, db, ui)
    assert "приостановлено" in await window_text(db, ADMIN_CHAT)


async def test_suspended_owner_keeps_the_link_but_earns_nothing(db, ui, config):
    """Связь заказа с клубом — факт; право на долю — отдельный факт."""
    owner, admin = await _admin_with_owner(db)
    async with db.write() as tx:
        await db.set_user_active(tx, owner["id"], False)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_order(db, ui, config, state)
    order = await db.get_order(1)
    assert order["owner_id"] == owner["id"]
    assert order["owner_share"] == 0 and order["owner_percent"] == 0
    assert order["owner_suspended"] == 1


async def test_discount_from_settings_reaches_the_price(db, ui, config, monkeypatch):
    owner, admin = await _admin_with_owner(db, discount=True)
    async with db.write() as tx:
        await db.set_setting(tx, "discount_percent", 20)
    monday_11 = datetime(2026, 7, 13, 11, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return monday_11.astimezone(tz) if tz else monday_11

    monkeypatch.setattr("handlers.staff.datetime", Frozen)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _confirm_screen(db, ui, config, state)
    text = await window_text(db, ADMIN_CHAT)
    assert "Скидка: −20%" in text and "К оплате: 240 ₽" in text


async def test_archived_promo_button_is_refused(db, ui, config):
    await _admin_with_owner(db)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "3=4", 600)
        await db.archive_promo(tx, promo_id)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    cb = fake_cb(f"no:promo:{promo_id}", message_id=WINDOW)
    await order_promo(cb, state, db, ui)
    assert cb.answer.await_args.args[0] == "Акция недоступна"
    assert await state.get_state() is None
