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

from errors import STALE_BUTTON
from handlers.staff import (
    NewOrderSG,
    order_drop,
    order_duration,
    order_free15,
    order_headsets,
    order_new,
    order_payment,
    order_promo,
    order_std,
)
from pricing import FREE15_LABEL, KIND_FREE15, ladder_amount

ADMIN_CHAT = 1
OWNER_CHAT = 201
WINDOW = 50


async def _confirm_screen(db, ui, config, state, *, chat=ADMIN_CHAT):
    await order_duration(fake_cb("no:d:1:30", chat_id=chat, message_id=WINDOW),
                         state, db, ui, config)


async def _place_order(db, ui, config, state, *, chat=ADMIN_CHAT):
    await _confirm_screen(db, ui, config, state, chat=chat)
    await order_payment(fake_cb("no:ok", chat_id=chat, message_id=WINDOW),
                        state, db, ui, config)


async def _place_without_promo(db, ui, config, state, *, chat=ADMIN_CHAT):
    """Тот же заказ, когда действующие акции есть: шаг акции проходится
    кнопкой «Без акции»."""
    await _confirm_screen(db, ui, config, state, chat=chat)
    await order_promo(fake_cb("no:p:1:30:0", chat_id=chat, message_id=WINDOW),
                      state, db, ui, config)
    await order_payment(fake_cb("no:ok", chat_id=chat, message_id=WINDOW),
                        state, db, ui, config)


async def _admin_with_owner(db, *, percent=30, discount=False, free15=False):
    """Клуб с владельцем и администратором.

    Скидка по умолчанию выключена: иначе цена зависела бы от того, в какой
    день и час запускают тесты. Бесплатный сеанс тоже выключен: он добавил
    бы первый экран каждому заказу, где он не проверяется.
    """
    owner = await create_owner(db, percent=percent)
    admin = await create_admin(db, owner)
    await bind(db, admin, ADMIN_CHAT)
    await bind(db, owner, OWNER_CHAT)
    async with db.write() as tx:
        await db.set_setting(tx, "discount_enabled", int(discount))
        await db.set_setting(tx, "free15_enabled", int(free15))
    return owner, admin


async def test_order_kind_screen_is_skipped_without_a_free_session(db, ui, config, bot):
    """Экран с единственным вариантом — чистое трение на каждом заказе."""
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_new(fake_cb("no:new", message_id=WINDOW), state, db, ui)
    assert "Сколько шлемов" in await window_text(db, ADMIN_CHAT)


async def test_promos_no_longer_make_a_kind_screen(db, ui, config):
    """Акция принадлежит заказу: на первом шаге её больше не выбирают."""
    await _admin_with_owner(db)
    async with db.write() as tx:
        await db.create_promo(tx, "День рождения")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_new(fake_cb("no:new", message_id=WINDOW), state, db, ui)
    assert "Сколько шлемов" in await window_text(db, ADMIN_CHAT)
    assert not any("День рождения" in b for b in await button_texts(db, ADMIN_CHAT))


async def test_order_kind_screen_appears_when_the_free_session_is_on(db, ui, config):
    await _admin_with_owner(db, free15=True)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_new(fake_cb("no:new", message_id=WINDOW), state, db, ui)
    assert "Выберите тип заказа" in await window_text(db, ADMIN_CHAT)
    buttons = await button_texts(db, ADMIN_CHAT)
    assert buttons[:2] == ["Стандартный сеанс", FREE15_LABEL]


async def test_headsets_screen_has_back_only_when_there_is_where_to_go(db, ui):
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_std(fake_cb("no:std", message_id=WINDOW), state, db, ui)
    assert "Назад" not in await button_texts(db, ADMIN_CHAT)
    async with db.write() as tx:
        await db.set_setting(tx, "free15_enabled", 1)
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
    # Итог периода называется так же, как на экранах владельца
    assert "К выплате: 90 ₽" in owner_texts[0]
    assert "Накоплено" not in owner_texts[0]
    # Деньги владельца — первыми, детали заказа под ними
    assert owner_texts[0].index("Ваша доля") < owner_texts[0].index("Администратор")
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


async def test_ladder_progresses_and_a_promo_order_counts_as_a_regular_one(
        db, ui, config):
    """Акция не меняет в заказе ни цену, ни вознаграждение: лесенка идёт
    своим чередом, а заказ с акцией занимает в ней обычное место."""
    owner, admin = await _admin_with_owner(db)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "3=4")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_without_promo(db, ui, config, state)
    await order_duration(fake_cb("no:d:1:30", message_id=WINDOW),
                         state, db, ui, config)
    await order_promo(fake_cb(f"no:p:1:30:{promo_id}", message_id=WINDOW),
                      state, db, ui, config)
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)
    await _place_without_promo(db, ui, config, state)

    shares = [o["admin_share"] for o in await db.export_orders()]
    assert shares == [50.0, 100.0, 150.0]
    promo_order = await db.get_order(2)
    assert promo_order["kind"] == "standard" and promo_order["promo_name"] == "3=4"
    assert promo_order["promo_id"] == promo_id
    assert promo_order["headsets"] == 1 and promo_order["minutes"] == 30
    # цена — обычная цена сеанса; у приза денег нет и вносить в неё нечего
    assert promo_order["price"] == 300.0 and promo_order["base_price"] == 300.0
    assert promo_order["discount_percent"] == 0
    # вознаграждение — ступень серии
    assert promo_order["admin_share"] == ladder_amount(2)


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
                 "no:d:99999999999999999999:30",
                 "no:p:1:30:x", "no:p:9:30:1", "no:p:1:45:1", "no:p:1"):
        cb = fake_cb(data, message_id=WINDOW)
        if data.startswith("no:h"):
            await order_headsets(cb, state, db, ui)
        elif data.startswith("no:d"):
            await order_duration(cb, state, db, ui, config)
        else:
            await order_promo(cb, state, db, ui, config)
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
    """Акцию удалили, пока экран висел: заказ не оформляется, а сеанс
    остаётся собранным — администратор выбирает заново на том же шаге."""
    await _admin_with_owner(db)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "3=4")
        await db.create_promo(tx, "День рождения")
        await db.archive_promo(tx, promo_id)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    cb = fake_cb(f"no:p:1:30:{promo_id}", message_id=WINDOW)
    await order_promo(cb, state, db, ui, config)
    assert cb.answer.await_args.args[0] == "Акция недоступна"
    assert await state.get_state() is None
    assert "Выберите акцию" in await window_text(db, ADMIN_CHAT)


# -------------------------------------------------- Акция принадлежит заказу

async def test_promo_step_only_after_headsets_and_duration(db, ui, config):
    """Акции показываются последним шагом — когда сеанс уже собран."""
    await _admin_with_owner(db)
    async with db.write() as tx:
        await db.create_promo(tx, "День рождения")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_std(fake_cb("no:std", message_id=WINDOW), state, db, ui)
    assert not any("День рождения" in b for b in await button_texts(db, ADMIN_CHAT))
    await order_headsets(fake_cb("no:h:1", message_id=WINDOW), state, db, ui)
    assert not any("День рождения" in b for b in await button_texts(db, ADMIN_CHAT))
    await order_duration(fake_cb("no:d:1:60", message_id=WINDOW),
                         state, db, ui, config)
    text = await window_text(db, ADMIN_CHAT)
    assert "1 шлем · 60 мин" in text and "Выберите акцию" in text
    # правило названо прямо на экране выбора — здесь приз легче всего
    # принять за скидку
    assert "На цену сеанса приз не влияет" in text
    buttons = await button_texts(db, ADMIN_CHAT)
    assert buttons[0] == "Без акции"
    assert "Акция · День рождения" in buttons
    assert not any("₽" in b for b in buttons)


async def test_short_session_never_offers_a_promo(db, ui, config):
    """Порог — 30 минут: на 15-минутном сеансе шага акции нет вовсе."""
    await _admin_with_owner(db)
    async with db.write() as tx:
        await db.create_promo(tx, "День рождения")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_duration(fake_cb("no:d:1:15", message_id=WINDOW),
                         state, db, ui, config)
    assert "К оплате: 200 ₽" in await window_text(db, ADMIN_CHAT)
    # устаревшая кнопка акции на 15 минутах не проходит проверку
    cb = fake_cb("no:p:1:15:1", message_id=WINDOW)
    await order_promo(cb, state, db, ui, config)
    assert cb.answer.await_args.args[0] == STALE_BUTTON


async def test_promo_step_is_skipped_when_there_are_no_promos(db, ui, config):
    await _admin_with_owner(db)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_duration(fake_cb("no:d:2:60", message_id=WINDOW),
                         state, db, ui, config)
    assert "К оплате: 800 ₽" in await window_text(db, ADMIN_CHAT)
    assert await state.get_state() == NewOrderSG.confirm.state


async def test_without_a_promo_the_order_keeps_the_standard_price(db, ui, config):
    owner, admin = await _admin_with_owner(db)
    async with db.write() as tx:
        await db.create_promo(tx, "3=4")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_duration(fake_cb("no:d:1:30", message_id=WINDOW),
                         state, db, ui, config)
    await order_promo(fake_cb("no:p:1:30:0", message_id=WINDOW),
                      state, db, ui, config)
    assert "К оплате: 300 ₽" in await window_text(db, ADMIN_CHAT)
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)
    order = await db.get_order(1)
    assert order["price"] == 300.0
    assert order["promo_id"] is None and order["promo_name"] is None


async def test_promo_order_is_summarised_everywhere(db, ui, config, bot, worker):
    """Выбранная акция едет в чек, уведомления, отчёты и выгрузку."""
    owner, admin = await _admin_with_owner(db)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "День рождения")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_duration(fake_cb("no:d:2:60", message_id=WINDOW),
                         state, db, ui, config)
    await order_promo(fake_cb(f"no:p:2:60:{promo_id}", message_id=WINDOW),
                      state, db, ui, config)
    text = await window_text(db, ADMIN_CHAT)
    # к оплате — обычная цена 2 шлемов на 60 минут, приз назван отдельно
    assert "К оплате: 800 ₽" in text
    assert "Приз: День рождения" in text
    assert "На цену сеанса приз не влияет" in text
    assert text.index("К оплате") < text.index("Приз:")
    # у приза нет денег: единственная сумма на экране — цена сеанса
    assert text.count("₽") == 1
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)
    assert (await db.get_order(1))["price"] == 800.0
    await drain(worker)

    label = "2 шлема · 60 мин · Акция · День рождения"
    receipts = [t for c, _, t in bot.sent
                if c == ADMIN_CHAT and t.startswith("<b>Заказ №1</b>")]
    assert receipts and label in receipts[0]
    owner_texts = [t for c, _, t in bot.sent if c == OWNER_CHAT]
    assert owner_texts and label in owner_texts[0]
    for admin_id in VR_ADMIN_IDS:
        texts = [t for c, _, t in bot.sent if c == admin_id]
        assert texts and label in texts[0]

    import export as xp
    from reports import admin_period
    csv = xp.orders_csv(await db.export_orders(), config.tz).decode("utf-8-sig")
    assert label in csv and "День рождения" in csv
    owner_csv = xp.owner_orders_csv(
        await db.export_orders(owner["id"]), config.tz).decode("utf-8-sig")
    assert label in owner_csv
    report = await admin_period(db, admin, config.tz)
    assert label in report.to_html(rich=False)


async def _place_with_promo(db, ui, config, state, promo_id, *,
                            headsets=1, minutes=30, chat=ADMIN_CHAT):
    """Заказ с призом: шлемы, длительность, приз, оплата."""
    await order_duration(fake_cb(f"no:d:{headsets}:{minutes}", chat_id=chat,
                                 message_id=WINDOW), state, db, ui, config)
    await order_promo(fake_cb(f"no:p:{headsets}:{minutes}:{promo_id}",
                              chat_id=chat, message_id=WINDOW),
                      state, db, ui, config)
    await order_payment(fake_cb("no:ok", chat_id=chat, message_id=WINDOW),
                        state, db, ui, config)


async def test_a_promo_carries_no_money_at_all(db, ui, config):
    """У акции нет денежной величины — ни в строке, ни в кнопке, ни на
    экране. Взять её в расчёт нечем: брать нечего.

    Это проверка «чего нет»: подпорка к остальным, которые проверяют,
    что цифры заказа не меняются.
    """
    await _admin_with_owner(db)
    async with db.write() as tx:
        await db.create_promo(tx, "Наклейка")
    promo = (await db.list_promos())[0]
    assert set(promo.keys()) == {"id", "name", "name_folded", "archived_at",
                                 "created_at"}
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_duration(fake_cb("no:d:1:30", message_id=WINDOW),
                         state, db, ui, config)
    assert not any("₽" in b for b in await button_texts(db, ADMIN_CHAT))
    assert "₽" not in await window_text(db, ADMIN_CHAT)


async def test_promo_changes_nothing_in_the_accounting_of_an_order(db, ui, config):
    """Главный инвариант: заказ с призом и такой же заказ без приза
    совпадают во всех цифрах — цене, базе, скидке, вознаграждении, доле
    владельца и остатке VR Heaven. Различается только записанный приз.

    Ступень лесенки сравнивается отдельно: она растёт от порядка заказов,
    а не от приза, поэтому заказы ставятся парами в разных сериях.
    """
    owner, admin = await _admin_with_owner(db)
    async with db.write() as tx:
        first = await db.create_promo(tx, "Наклейка")
        second = await db.create_promo(tx, "Сертификат")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_without_promo(db, ui, config, state)
    await _place_with_promo(db, ui, config, state, first)
    await _place_with_promo(db, ui, config, state, second)

    plain, one, two = [await db.get_order(i) for i in (1, 2, 3)]
    money = ("price", "base_price", "discount_percent", "owner_share",
             "owner_percent", "admin_percent", "kind", "headsets", "minutes")
    for order in (one, two):
        for field in money:
            assert order[field] == plain[field], field
        # остаток VR Heaven = цена − вознаграждение − доля владельца;
        # вознаграждение задаёт лесенка и оно сравнивается ниже, а от
        # приза зависела бы ровно эта часть — она совпадает
        assert (round(order["price"] - order["owner_share"], 2)
                == round(plain["price"] - plain["owner_share"], 2))
    assert plain["price"] == 300.0 and plain["owner_share"] == 90.0
    # приз записан и различается, ступень растёт обычным порядком
    assert [o["promo_name"] for o in (plain, one, two)] == [
        None, "Наклейка", "Сертификат"]
    assert [o["promo_id"] for o in (plain, one, two)] == [None, first, second]
    assert [o["admin_share"] for o in (plain, one, two)] == [
        ladder_amount(1), ladder_amount(2), ladder_amount(3)]
    assert [o["series_pos"] for o in (plain, one, two)] == [1, 2, 3]


async def test_the_discount_reaches_a_prize_order_too(db, ui, config, monkeypatch):
    """Приз не отменяет скидку и не подменяет базовую цену: заказ с призом
    считается той же цепочкой база → скидка → округление."""
    owner, admin = await _admin_with_owner(db, discount=True)
    async with db.write() as tx:
        await db.set_setting(tx, "discount_percent", 20)
        prize = await db.create_promo(tx, "Сертификат")
    monday_11 = datetime(2026, 7, 13, 11, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return monday_11.astimezone(tz) if tz else monday_11

    monkeypatch.setattr("handlers.staff.datetime", Frozen)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_with_promo(db, ui, config, state, prize)
    await _place_without_promo(db, ui, config, state)

    with_prize, without = await db.get_order(1), await db.get_order(2)
    for order in (with_prize, without):
        assert order["base_price"] == 300.0        # база сеанса как есть
        assert order["discount_percent"] == 20.0   # скидка не потеряна
        assert order["price"] == 240.0
    assert with_prize["promo_name"] == "Сертификат"


async def test_price_matches_the_same_order_without_a_prize_everywhere(
        db, ui, config):
    """Инвариант по всем сочетаниям, где приз вообще предлагается:
    цена заказа с призом равна цене того же заказа без приза."""
    owner, admin = await _admin_with_owner(db)
    async with db.write() as tx:
        prize = await db.create_promo(tx, "Шлем в подарок")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    combos = [(h, m) for h in (1, 2) for m in (30, 60)]
    for headsets, minutes in combos:
        await _place_with_promo(db, ui, config, state, prize,
                                headsets=headsets, minutes=minutes)
        await _place_with_promo(db, ui, config, state, 0,
                                headsets=headsets, minutes=minutes)

    orders = await db.export_orders()
    expected = {(1, 30): 300.0, (1, 60): 500.0, (2, 30): 500.0, (2, 60): 800.0}
    for with_prize, without in zip(orders[::2], orders[1::2], strict=True):
        combo = (with_prize["headsets"], with_prize["minutes"])
        assert with_prize["promo_name"] == "Шлем в подарок"
        assert without["promo_name"] is None
        assert with_prize["price"] == without["price"] == expected[combo], combo
        assert with_prize["owner_share"] == without["owner_share"], combo


async def test_a_prize_is_not_a_free_session(db, ui, config):
    """Приз и бесплатные 15 минут — разные вещи: приз оставляет обычный
    заказ обычным, бесплатный сеанс обнуляет и цену, и лесенку."""
    owner, admin = await _admin_with_owner(db, free15=True)
    async with db.write() as tx:
        free_prize = await db.create_promo(tx, "Наклейка")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_with_promo(db, ui, config, state, free_prize)
    await _place_free15(db, ui, config, state)

    with_prize, free = await db.get_order(1), await db.get_order(2)
    assert with_prize["price"] == 300.0 and with_prize["admin_share"] == 50.0
    assert with_prize["owner_share"] == 90.0 and with_prize["series_pos"] == 1
    assert with_prize["promo_name"] == "Наклейка"
    assert free["price"] == 0.0 and free["admin_share"] == 0.0
    assert free["owner_share"] == 0.0 and free["series_pos"] is None
    assert free["kind"] == KIND_FREE15 and free["promo_id"] is None


async def test_promo_does_not_change_the_admin_reward(db, ui, config):
    """Ступень лесенки одна и та же с призом и без него — и цена тоже."""
    owner, admin = await _admin_with_owner(db)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "3=4")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_with_promo(db, ui, config, state, promo_id)
    await _place_without_promo(db, ui, config, state)

    with_promo, without = await db.get_order(1), await db.get_order(2)
    assert with_promo["price"] == without["price"] == 300.0
    assert with_promo["admin_share"] == ladder_amount(1)
    assert without["admin_share"] == ladder_amount(2)
    # доля владельца — процент от обычной цены, приз её не двигает
    assert with_promo["owner_share"] == without["owner_share"] == 90.0


# ------------------------------------------------------ 15 минут бесплатно

async def _place_free15(db, ui, config, state, *, chat=ADMIN_CHAT):
    await order_free15(fake_cb("no:free", chat_id=chat, message_id=WINDOW),
                       state, db, ui)
    await order_payment(fake_cb("no:ok", chat_id=chat, message_id=WINDOW),
                        state, db, ui, config)


async def test_free15_goes_straight_to_confirmation(db, ui, config):
    await _admin_with_owner(db, free15=True)
    async with db.write() as tx:
        await db.create_promo(tx, "День рождения")
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_free15(fake_cb("no:free", message_id=WINDOW), state, db, ui)
    text = await window_text(db, ADMIN_CHAT)
    assert FREE15_LABEL in text and "К оплате: 0 ₽" in text
    assert "шлем" not in text and "День рождения" not in text
    assert await state.get_state() == NewOrderSG.confirm.state
    assert "Оформить заказ" in await button_texts(db, ADMIN_CHAT)


async def test_free15_order_is_free_for_everyone(db, ui, config):
    owner, admin = await _admin_with_owner(db, free15=True)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_free15(db, ui, config, state)
    order = await db.get_order(1)
    assert order["kind"] == KIND_FREE15
    assert order["price"] == 0 and order["admin_share"] == 0
    assert order["owner_share"] == 0 and order["owner_id"] == owner["id"]
    assert order["headsets"] is None and order["minutes"] is None
    assert order["promo_id"] is None
    # места в серии бесплатный сеанс не занимает
    assert order["series_pos"] is None and order["series_since"] is None
    assert "вознаграждение" in (await window_text(db, ADMIN_CHAT)).lower()


async def test_free15_does_not_advance_the_ladder(db, ui, config):
    """Бесплатный сеанс не должен дарить ступень: следующий платный
    заказ остаётся первым в серии."""
    await _admin_with_owner(db, free15=True)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_free15(db, ui, config, state)
    await _place_order(db, ui, config, state)
    await _place_free15(db, ui, config, state)
    await _place_order(db, ui, config, state)

    orders = await db.export_orders()
    assert [o["series_pos"] for o in orders] == [None, 1, None, 2]
    assert [o["admin_share"] for o in orders] == [0.0, 50.0, 0.0, 100.0]


async def test_free15_is_unavailable_while_disabled(db, ui, config):
    """Выключенный тип не показывается и не оформляется устаревшей кнопкой."""
    await _admin_with_owner(db)                      # free15_enabled = 0
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await order_new(fake_cb("no:new", message_id=WINDOW), state, db, ui)
    assert FREE15_LABEL not in await button_texts(db, ADMIN_CHAT)
    cb = fake_cb("no:free", message_id=WINDOW)
    await order_free15(cb, state, db, ui)
    assert "недоступен" in cb.answer.await_args.args[0]
    assert await state.get_state() is None
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)
    assert await db.export_orders() == []


async def test_cancelling_a_free_session_promises_nothing_back(db, ui, config,
                                                              bot, worker):
    """«Вознаграждение 0 ₽ снято» противоречило бы экрану оформления:
    снимать было нечего, и удержания за такой заказ не появляется."""
    from handlers.staff import cancel_confirm

    owner, admin = await _admin_with_owner(db, free15=True)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_free15(db, ui, config, state)
    await cancel_confirm(fake_cb("sc:ok:1", message_id=WINDOW),
                         state, db, ui, config)
    assert (await db.get_order(1))["cancelled_at"] is not None
    assert await db.admin_unpaid_bonuses(admin["id"]) == []
    await drain(worker)
    for text in [await window_text(db, ADMIN_CHAT), *bot.records(OWNER_CHAT)]:
        assert "снято" not in text
    for admin_id in VR_ADMIN_IDS:
        for text in bot.records(admin_id):
            assert "снято" not in text


async def test_free15_is_represented_in_reports_and_exports(db, ui, config,
                                                            bot, worker):
    owner, admin = await _admin_with_owner(db, free15=True)
    await set_window(db, ADMIN_CHAT, WINDOW)
    state = make_state(db, ADMIN_CHAT)
    await _place_free15(db, ui, config, state)
    await drain(worker)

    receipts = [t for c, _, t in bot.sent
                if c == ADMIN_CHAT and t.startswith("<b>Заказ №1</b>")]
    assert receipts and FREE15_LABEL in receipts[0]
    for admin_id in VR_ADMIN_IDS:
        texts = [t for c, _, t in bot.sent if c == admin_id]
        assert texts and FREE15_LABEL in texts[0]
        assert "№None" not in texts[0]

    import export as xp
    from reports import admin_period, vrheaven_summary
    csv = xp.orders_csv(await db.export_orders(), config.tz).decode("utf-8-sig")
    assert FREE15_LABEL in csv
    owner_csv = xp.owner_orders_csv(
        await db.export_orders(owner["id"]), config.tz).decode("utf-8-sig")
    assert FREE15_LABEL in owner_csv
    assert FREE15_LABEL in (await admin_period(db, admin, config.tz)).to_html(rich=False)
    assert "Заказов за период: 1" in (await vrheaven_summary(db)).to_html(rich=False)


async def test_a_free15_only_period_still_closes_with_a_payout(db, ui, config):
    """Заказ с нулями — настоящая строка, и период обязан ею закрываться:
    иначе бесплатный сеанс тянется в каждой следующей сводке вечно.
    Тот же случай, что доля 0 у приостановленного владельца (SPEC §4)."""
    owner, admin = await _admin_with_owner(db, free15=True)
    await set_window(db, ADMIN_CHAT, WINDOW)
    await _place_free15(db, ui, config, make_state(db, ADMIN_CHAT))

    total = await db.admin_unpaid_total(admin["id"])
    assert total["orders_count"] == 1 and total["due_sum"] == 0
    assert db.is_payable(total), "настоящая строка обязана поддаваться закрытию"
    async with db.write() as tx:
        payout = await db.create_payout(tx, admin)
    assert payout[1] == 0.0 and payout[2] == 1
    assert (await db.get_order(1))["admin_payout_id"] is not None
