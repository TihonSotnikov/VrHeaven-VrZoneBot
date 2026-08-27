"""Акции — призы колеса фортуны: у акции есть только название.

Денежной величины у акции нет ни в интерфейсе, ни в схеме, ни в коде.
Здесь это проверяется с обеих сторон: чего в акции нет и что в ней есть.
"""

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

from handlers.vrheaven import (
    promo_add_name,
    promo_add_start,
    promo_delete_confirm,
    promo_open,
    promos_menu,
)

VR_CHAT = 999
WINDOW = 50


async def _add(db, ui, name="День рождения"):
    """Заведение акции целиком: название — и всё."""
    state = make_state(db, VR_CHAT)
    await promo_add_start(fake_cb("pr:add", chat_id=VR_CHAT, message_id=WINDOW),
                          state, ui)
    await promo_add_name(fake_msg(name, chat_id=VR_CHAT), state, db, ui)
    return state


async def test_promo_is_created_from_a_name_alone(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    state = await _add(db, ui)
    promos = await db.list_promos()
    assert [p["name"] for p in promos] == ["День рождения"]
    # сценарий закончился на названии: ни второго шага, ни ввода суммы
    assert await state.get_state() is None
    assert "День рождения" in await button_texts(db, VR_CHAT)


async def test_promo_row_has_no_money_column(db, ui):
    """Величины нет в самой схеме: её нельзя прочитать и нельзя записать."""
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4")
    columns = {r[1] for r in await db.fetchall("PRAGMA table_info(promos)")}
    assert columns == {"id", "name", "name_folded", "archived_at", "created_at"}
    promo = (await db.list_promos())[0]
    assert set(promo.keys()) == columns
    assert not any("price" in c or "value" in c or "amount" in c for c in columns)


async def test_promo_name_validation(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    state = make_state(db, VR_CHAT)
    await promo_add_start(fake_cb("pr:add", chat_id=VR_CHAT, message_id=WINDOW),
                          state, ui)
    await promo_add_name(fake_msg("  ", chat_id=VR_CHAT), state, db, ui)
    assert "Введите название ещё раз" in await window_text(db, VR_CHAT)
    assert await db.list_promos() == []
    await promo_add_name(fake_msg("я" * 41, chat_id=VR_CHAT), state, db, ui)
    assert await db.list_promos() == []
    await promo_add_name(fake_msg("День рождения", chat_id=VR_CHAT), state, db, ui)
    assert len(await db.list_promos()) == 1


async def test_duplicate_name_is_refused_case_insensitively(db, ui):
    """SQLite lower() кириллицу не сворачивает — сравнение идёт casefold()."""
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "День Рождения")
    state = make_state(db, VR_CHAT)
    await promo_add_start(fake_cb("pr:add", chat_id=VR_CHAT, message_id=WINDOW),
                          state, ui)
    await promo_add_name(fake_msg("день рождения", chat_id=VR_CHAT), state, db, ui)
    assert "уже существует" in await window_text(db, VR_CHAT)
    assert len(await db.list_promos()) == 1


async def test_creation_is_audited_without_any_money(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4")
    entry = (await db.export_audit())[-1]
    assert entry["action"] == "promo.create"
    assert entry["after_json"] == '{"name": "3=4"}'


async def test_tapping_a_promo_opens_it_rather_than_deleting(db, ui):
    """Кнопка со списком читается как «открыть» — она и открывает."""
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4")
    promo = (await db.list_promos())[0]
    await promo_open(fake_cb(f"pr:open:{promo['id']}", chat_id=VR_CHAT,
                             message_id=WINDOW), make_state(db, VR_CHAT), db, ui)
    buttons = await button_texts(db, VR_CHAT)
    # править нечего: у акции есть только имя
    assert buttons == ["Удалить акцию", "К списку акций"]
    assert (await db.get_promo(promo["id"]))["archived_at"] is None


async def test_promo_card_shows_a_name_and_no_money(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "День рождения")
    promo = (await db.list_promos())[0]
    await promo_open(fake_cb(f"pr:open:{promo['id']}", chat_id=VR_CHAT,
                             message_id=WINDOW), make_state(db, VR_CHAT), db, ui)
    text = await window_text(db, VR_CHAT)
    assert "День рождения" in text and "Никаких денег у акции нет" in text
    assert "₽" not in text


async def test_delete_archives_and_keeps_order_history(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await _add(db, ui, "3=4")
    promo = (await db.list_promos())[0]
    await make_order(db, admin, owner, price=300, promo_id=promo["id"],
                     promo_name="3=4")
    await promo_delete_confirm(fake_cb(f"pr:delok:{promo['id']}", chat_id=VR_CHAT,
                                       message_id=WINDOW), db, ui)
    assert await db.list_promos() == []
    order = await db.get_order(1)
    # снимок названия — единственное, что связывает заказ с удалённой акцией
    assert order["promo_name"] == "3=4" and order["promo_id"] == promo["id"]
    assert order["price"] == 300.0


async def test_double_delete_is_safe(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4")
    promo = (await db.list_promos())[0]
    await promo_delete_confirm(fake_cb(f"pr:delok:{promo['id']}", chat_id=VR_CHAT,
                                       message_id=WINDOW), db, ui)
    cb = fake_cb(f"pr:delok:{promo['id']}", chat_id=VR_CHAT, message_id=WINDOW)
    await promo_delete_confirm(cb, db, ui)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_deleted_promo_name_is_reusable(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4")
    promo = (await db.list_promos())[0]
    await promo_delete_confirm(fake_cb(f"pr:delok:{promo['id']}", chat_id=VR_CHAT,
                                       message_id=WINDOW), db, ui)
    await _add(db, ui, "3=4")
    assert [p["name"] for p in await db.list_promos()] == ["3=4"]


async def test_promo_screen_states_the_rules(db, ui):
    """Экран акций обязан называть акцию призом без денег: именно отсюда
    супер-админ понимает, что на расчёты она не влияет."""
    await set_window(db, VR_CHAT, WINDOW)
    await promos_menu(fake_cb("pr:menu", chat_id=VR_CHAT, message_id=WINDOW),
                      make_state(db, VR_CHAT), db, ui)
    text = await window_text(db, VR_CHAT)
    assert "приз колеса фортуны" in text
    assert "Денег у акции нет" in text
    assert "скидка" not in text.lower() and "₽" not in text
    assert "Действующих акций сейчас нет" in text


async def test_the_promo_list_shows_names_only(db, ui):
    await set_window(db, VR_CHAT, WINDOW)
    await _add(db, ui, "3=4")
    await _add(db, ui, "День рождения")
    await promos_menu(fake_cb("pr:menu", chat_id=VR_CHAT, message_id=WINDOW),
                      make_state(db, VR_CHAT), db, ui)
    buttons = await button_texts(db, VR_CHAT)
    assert buttons[:2] == ["3=4", "День рождения"]
    assert not any("₽" in b for b in buttons)
