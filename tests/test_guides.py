"""Инструкции «Как пользоваться» для каждой группы пользователей."""


from helpers import bind, create_admin, create_owner, drain, fake_cb, make_state, set_window

import handlers.staff as staff_handlers
import handlers.vrheaven as vr_handlers
from handlers.common import GUIDE_TITLES, guide_bytes

CHAT = 1
VR_CHAT = 999
WINDOW = 50


def test_every_role_has_a_guide(config):
    for role in ("superadmin", "owner", "admin"):
        title, filename, data = guide_bytes(config, role)
        assert title and filename.endswith(".md")
        text = data.decode()
        assert "@VrHeaven" in text or role == "superadmin"
        assert len(text) < 5000, "инструкция должна быть короткой"


def test_guides_match_the_interface_wording(config):
    _, _, admin_text = guide_bytes(config, "admin")
    text = admin_text.decode()
    for phrase in ("Новый заказ", "Оплата получена", "К оплате",
                   "Моя статистика", "15 минут"):
        assert phrase in text
    _, _, owner_text = guide_bytes(config, "owner")
    for phrase in ("Текущий период", "Мои администраторы", "Экспорт данных"):
        assert phrase in owner_text.decode()
    _, _, vr_text = guide_bytes(config, "superadmin")
    for phrase in ("Сводка", "Выплата", "Настройки", "Доступ VR Heaven"):
        assert phrase in vr_text.decode()


async def test_admin_receives_the_guide_as_a_file(db, ui, config, bot, worker):
    admin = await create_admin(db)
    await bind(db, admin, CHAT)
    await set_window(db, CHAT, WINDOW)
    cb = fake_cb("guide", chat_id=CHAT, message_id=WINDOW)
    await staff_handlers.guide(cb, make_state(db, CHAT), db, ui, config)
    await drain(worker)
    assert bot.documents[0][3].filename == GUIDE_TITLES["admin"][1]
    assert cb.answer.await_args.args[0] == "Инструкция отправлена в чат"


async def test_owner_receives_the_owner_guide(db, ui, config, bot, worker):
    owner = await create_owner(db)
    await bind(db, owner, CHAT)
    await set_window(db, CHAT, WINDOW)
    await staff_handlers.guide(fake_cb("guide", chat_id=CHAT, message_id=WINDOW),
                               make_state(db, CHAT), db, ui, config)
    await drain(worker)
    assert bot.documents[0][3].filename == GUIDE_TITLES["owner"][1]


async def test_super_admin_receives_the_panel_guide(db, ui, config, bot, worker):
    await set_window(db, VR_CHAT, WINDOW)
    await vr_handlers.guide(fake_cb("guide", chat_id=VR_CHAT, message_id=WINDOW),
                            db, ui, config)
    await drain(worker)
    assert bot.documents[0][3].filename == GUIDE_TITLES["superadmin"][1]


def test_every_menu_offers_the_guide():
    import keyboards as kb
    for markup in (kb.vrheaven_menu_kb(), kb.staff_admin_menu_kb(),
                   kb.staff_owner_menu_kb()):
        actions = {b.callback_data for row in markup.inline_keyboard for b in row}
        assert "guide" in actions


def test_staff_menus_offer_support():
    import keyboards as kb
    for markup in (kb.staff_admin_menu_kb(), kb.staff_owner_menu_kb()):
        actions = {b.callback_data for row in markup.inline_keyboard for b in row}
        assert "help" in actions


def test_guide_is_utf8_with_a_signature(config):
    """Bot API не передаёт кодировку: aiogram кладёт документ в форму как
    application/octet-stream, и просмотрщик угадывает её сам. Android
    угадывает UTF-8, iOS — однобайтовую, и кириллица превращается в
    «ÐšÐ°Ðº». Подпись UTF-8 — единственный сигнал, который доезжает."""
    for role in ("superadmin", "owner", "admin"):
        _, filename, data = guide_bytes(config, role)
        assert filename.endswith(".md"), "инструкция остаётся Markdown"
        assert data.startswith(b"\xef\xbb\xbf"), f"{role}: нет подписи UTF-8"
        assert not data[3:].startswith(b"\xef\xbb\xbf"), f"{role}: подпись дважды"
        text = data.decode("utf-8-sig")          # строго: невалидный UTF-8 упадёт
        assert "Как" in text or "VR Heaven" in text
        # Android и любой другой читатель без подписи видит тот же текст
        assert data.decode("utf-8").lstrip("﻿") == text


def test_guide_signature_survives_a_source_file_that_already_has_one(tmp_path, config):
    """Двойного BOM не появится, в каком бы виде ни лежал файл в guides/."""
    from dataclasses import replace
    (tmp_path / "admin.md").write_text("﻿# Инструкция\n", encoding="utf-8")
    _, _, data = guide_bytes(replace(config, guides_dir=str(tmp_path)), "admin")
    assert data == "﻿# Инструкция\n".encode()
