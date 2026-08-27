"""Акции: создание, изменение цены, удаление и уникальность имени."""

from helpers import (
    button_texts,
    create_admin,
    create_owner,
    fake_cb,
    fake_msg,
    make_order,
    make_state,
    set_window,
    window_text,
)

from errors import STALE_BUTTON
from handlers.vrheaven import (
    promo_add_name,
    promo_add_price,
    promo_add_start,
    promo_delete_confirm,
    promo_open,
    promo_price_apply,
    promo_price_ask,
    promo_price_set,
    promos_menu,
)

VR_CHAT = 999
WINDOW = 50


async def _add(db, ui, name="День рождения", price="700"):
    state = make_state(db, VR_CHAT)
    await promo_add_start(fake_cb("pr:add", chat_id=VR_CHAT, message_id=WINDOW),
                          state, ui)
    await promo_add_name(fake_msg(name, chat_id=VR_CHAT), state, db, ui)
    await promo_add_price(fake_msg(price, chat_id=VR_CHAT), state, db, ui)
    return state


async def _ask_price(db, ui, promo, state):
    await promo_price_ask(fake_cb(f"pr:price:{promo['id']}", chat_id=VR_CHAT,
                                  message_id=WINDOW), state, db, ui)


async def test_promo_is_created_and_listed(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui)
    promos = await db.list_promos()
    assert [(p["name"], p["price"]) for p in promos] == [("День рождения", 700.0)]
    assert any("День рождения" in b for b in await button_texts(db, VR_CHAT))


async def test_promo_name_validation(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    state = make_state(db, VR_CHAT)
    await promo_add_start(fake_cb("pr:add", chat_id=VR_CHAT, message_id=WINDOW),
                          state, ui)
    await promo_add_name(fake_msg("  ", chat_id=VR_CHAT), state, db, ui)
    assert "Введите название ещё раз" in await window_text(db, VR_CHAT)
    await promo_add_name(fake_msg("я" * 41, chat_id=VR_CHAT), state, db, ui)
    assert await db.list_promos() == []


async def test_duplicate_name_is_refused_case_insensitively(db, ui):
    """SQLite lower() кириллицу не сворачивает — сравнение идёт casefold()."""
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "День Рождения", "700")
    state = make_state(db, VR_CHAT)
    await promo_add_start(fake_cb("pr:add", chat_id=VR_CHAT, message_id=WINDOW),
                          state, ui)
    await promo_add_name(fake_msg("день рождения", chat_id=VR_CHAT), state, db, ui)
    assert "уже существует" in await window_text(db, VR_CHAT)
    assert len(await db.list_promos()) == 1


async def test_promo_price_validation(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    state = make_state(db, VR_CHAT)
    await promo_add_start(fake_cb("pr:add", chat_id=VR_CHAT, message_id=WINDOW),
                          state, ui)
    await promo_add_name(fake_msg("3=4", chat_id=VR_CHAT), state, db, ui)
    await promo_add_price(fake_msg("бесплатно", chat_id=VR_CHAT), state, db, ui)
    assert await db.list_promos() == []
    await promo_add_price(fake_msg("600", chat_id=VR_CHAT), state, db, ui)
    assert len(await db.list_promos()) == 1


async def test_tapping_a_promo_opens_it_rather_than_deleting(db, ui):
    """Кнопка со списком читается как «открыть» — она и открывает."""
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4", "600")
    promo = (await db.list_promos())[0]
    await promo_open(fake_cb(f"pr:open:{promo['id']}", chat_id=VR_CHAT,
                             message_id=WINDOW), make_state(db, VR_CHAT), db, ui)
    buttons = await button_texts(db, VR_CHAT)
    assert "Изменить цену" in buttons and "Удалить акцию" in buttons
    assert (await db.get_promo(promo["id"]))["archived_at"] is None


async def test_promo_price_can_be_edited_without_changing_its_id(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4", "600")
    promo = (await db.list_promos())[0]
    state = make_state(db, VR_CHAT)
    await promo_price_ask(fake_cb(f"pr:price:{promo['id']}", chat_id=VR_CHAT,
                                  message_id=WINDOW), state, db, ui)
    await promo_price_set(fake_msg("750", chat_id=VR_CHAT), state, db, ui)
    updated = await db.get_promo(promo["id"])
    assert updated["id"] == promo["id"] and updated["price"] == 750.0


async def test_tripled_promo_price_requires_confirmation(db, ui):
    """Та же защита, что у цен сеансов: цену акции платит клиент
    у прилавка, и лишний ноль не должен переписать её молча."""
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4", "600")
    promo = (await db.list_promos())[0]
    state = make_state(db, VR_CHAT)
    await _ask_price(db, ui, promo, state)
    await promo_price_set(fake_msg("6000", chat_id=VR_CHAT), state, db, ui)
    assert (await db.get_promo(promo["id"]))["price"] == 600.0
    text = await window_text(db, VR_CHAT)
    assert "Было: 600 ₽" in text and "Станет: 6\u00a0000 ₽" in text

    await promo_price_apply(fake_cb("pr:priceok", chat_id=VR_CHAT,
                                    message_id=WINDOW), state, db, ui)
    assert (await db.get_promo(promo["id"]))["price"] == 6000.0
    log = await db.export_audit()
    assert log[-1]["action"] == "promo.price"


async def test_cancelling_the_tripled_promo_price_leaves_it_alone(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4", "600")
    promo = (await db.list_promos())[0]
    state = make_state(db, VR_CHAT)
    await _ask_price(db, ui, promo, state)
    await promo_price_set(fake_msg("60", chat_id=VR_CHAT), state, db, ui)
    assert "Подтвердите изменение" in await window_text(db, VR_CHAT)
    # Отмена ведёт на карточку акции и закрывает сценарий
    await promo_open(fake_cb(f"pr:open:{promo['id']}", chat_id=VR_CHAT,
                             message_id=WINDOW), state, db, ui)
    assert (await db.get_promo(promo["id"]))["price"] == 600.0
    assert await state.get_state() is None


async def test_confirming_a_price_for_a_deleted_promo_is_safe(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4", "600")
    promo = (await db.list_promos())[0]
    state = make_state(db, VR_CHAT)
    await _ask_price(db, ui, promo, state)
    await promo_price_set(fake_msg("6000", chat_id=VR_CHAT), state, db, ui)
    await promo_delete_confirm(fake_cb(f"pr:delok:{promo['id']}", chat_id=VR_CHAT,
                                       message_id=WINDOW), db, ui)
    cb = fake_cb("pr:priceok", chat_id=VR_CHAT, message_id=WINDOW)
    await promo_price_apply(cb, state, db, ui)
    assert (await db.get_promo(promo["id"]))["price"] == 600.0
    assert cb.answer.await_args.args[0] == STALE_BUTTON


async def test_delete_archives_and_keeps_order_history(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await _add(db, ui, "3=4", "600")
    promo = (await db.list_promos())[0]
    await make_order(db, admin, owner, price=600, kind="promo", headsets=None,
                     minutes=None, promo_id=promo["id"], promo_name="3=4")
    await promo_delete_confirm(fake_cb(f"pr:delok:{promo['id']}", chat_id=VR_CHAT,
                                       message_id=WINDOW), db, ui)
    assert await db.list_promos() == []
    order = await db.get_order(1)
    assert order["promo_name"] == "3=4" and order["promo_id"] == promo["id"]


async def test_double_delete_is_safe(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4", "600")
    promo = (await db.list_promos())[0]
    await promo_delete_confirm(fake_cb(f"pr:delok:{promo['id']}", chat_id=VR_CHAT,
                                       message_id=WINDOW), db, ui)
    cb = fake_cb(f"pr:delok:{promo['id']}", chat_id=VR_CHAT, message_id=WINDOW)
    await promo_delete_confirm(cb, db, ui)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_deleted_promo_name_is_reusable(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4", "600")
    promo = (await db.list_promos())[0]
    await promo_delete_confirm(fake_cb(f"pr:delok:{promo['id']}", chat_id=VR_CHAT,
                                       message_id=WINDOW), db, ui)
    await _add(db, ui, "3=4", "650")
    assert [(p["name"], p["price"]) for p in await db.list_promos()] == [("3=4", 650.0)]


async def test_promo_screen_states_the_rules(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await promos_menu(fake_cb("pr:menu", chat_id=VR_CHAT, message_id=WINDOW),
                      make_state(db, VR_CHAT), db, ui)
    text = await window_text(db, VR_CHAT)
    assert "скидка на неё не действует" in text
    assert "Действующих акций сейчас нет" in text
