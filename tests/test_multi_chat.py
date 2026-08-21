"""Мульти-аккаунт и вход: несколько устройств у одной учётной записи."""

from helpers import (
    bind,
    create_admin,
    create_owner,
    drain,
    fake_cb,
    fake_msg,
    make_state,
    set_window,
    window_text,
)

from handlers.staff import (
    _login_block,
    login_handle,
    login_password,
    login_start,
)

CHAT_1 = 101
CHAT_2 = 102
WINDOW = 50


async def _login(db, ui, chat_id, handle="own1", password="secret"):
    state = make_state(db, chat_id)
    await login_start(fake_cb("slogin", chat_id=chat_id, message_id=WINDOW),
                      state, ui)
    await login_handle(fake_msg(handle, chat_id=chat_id), state, ui)
    await login_password(fake_msg(password, chat_id=chat_id), state, db, ui)
    return state


async def test_second_device_logs_in_and_the_first_is_warned(db, ui, bot, worker):
    owner = await create_owner(db, password="secret")
    await bind(db, owner, CHAT_1)
    await set_window(db, CHAT_1, WINDOW)
    await _login(db, ui, CHAT_2)
    await drain(worker)
    assert await db.chats_for_user(owner["id"]) == [CHAT_1, CHAT_2]
    warnings = bot.records(CHAT_1)
    assert len(warnings) == 1 and "нового устройства" in warnings[0]


async def test_relogin_from_the_same_device_does_not_warn(db, ui, bot, worker):
    owner = await create_owner(db, password="secret")
    await bind(db, owner, CHAT_1)
    await _login(db, ui, CHAT_1)
    await drain(worker)
    assert bot.records(CHAT_1) == []


async def test_rebinding_moves_only_that_chat(db, ui):
    first = await create_owner(db, "own1", password="secret")
    second = await create_owner(db, "own2", password="secret")
    await bind(db, first, CHAT_1)
    await bind(db, first, CHAT_2)
    await _login(db, ui, CHAT_2, handle="own2")
    assert await db.chats_for_user(first["id"]) == [CHAT_1]
    assert await db.chats_for_user(second["id"]) == [CHAT_2]


async def test_unknown_handle_is_indistinguishable_from_a_wrong_password(db, ui):
    """Бот не отвечает на вопрос «существует ли такой логин»."""
    await create_owner(db, "own1", password="secret")
    _login_block.clear()
    await _login(db, ui, CHAT_1, handle="не-существует", password="secret")
    unknown = await window_text(db, CHAT_1)
    _login_block.clear()
    await _login(db, ui, CHAT_1, handle="own1", password="неверный")
    wrong = await window_text(db, CHAT_1)
    assert unknown == wrong == (
        "<b>Вход в кабинет</b>\n\nЛогин или пароль не подходят. "
        "Введите логин ещё раз")


async def test_suspended_account_learns_the_real_reason(db, ui):
    """С верным паролем человек узнаёт настоящую причину, а не ищет опечатку."""
    owner = await create_owner(db, "own1", password="secret")
    async with db.write() as tx:
        await db.set_user_active(tx, owner["id"], False)
    _login_block.clear()
    await _login(db, ui, CHAT_1)
    text = await window_text(db, CHAT_1)
    assert "приостановлена" in text and "@VrHeaven" in text
    assert await db.chats_for_user(owner["id"]) == []


async def test_repeated_failures_cool_down_the_chat(db, ui):
    await create_owner(db, "own1", password="secret")
    _login_block.clear()
    for _ in range(5):
        await _login(db, ui, CHAT_1, password="неверный")
    await _login(db, ui, CHAT_1, password="secret")
    assert await db.get_user_by_chat(CHAT_1) is None
    assert "Слишком много попыток" in await window_text(db, CHAT_1)
    _login_block.clear()


async def test_login_is_written_to_the_audit_log(db, ui):
    owner = await create_owner(db, "own1", password="secret")
    _login_block.clear()
    await _login(db, ui, CHAT_1)
    rows = await db.audit_for_entity("user", owner["id"])
    assert rows and rows[0]["action"] == "user.login"


async def test_suspended_admin_sees_the_state_in_the_menu(db, ui):
    admin = await create_admin(db, handle="adm1", password="secret")
    await bind(db, admin, CHAT_1)
    async with db.write() as tx:
        await db.set_user_active(tx, admin["id"], False)
    from handlers.staff import staff_menu
    await set_window(db, CHAT_1, WINDOW)
    await staff_menu(fake_cb("sm", chat_id=CHAT_1, message_id=WINDOW),
                     make_state(db, CHAT_1), db, ui)
    assert "Статус: приостановлен" in await window_text(db, CHAT_1)
