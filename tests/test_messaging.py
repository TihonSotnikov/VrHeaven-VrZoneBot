"""Модель сообщений: одно Окно и постоянные Записи.

Инварианты, закреплённые здесь, — это ровно те свойства, потеря которых
раньше стоила пользователю резервной копии и незавершённой продажи:

* кнопки есть ровно у одного сообщения в чате;
* бот удаляет только своё Окно и сообщение пользователя;
* новое Окно отправляется до удаления прежнего, а не после;
* Запись не удаляется никогда.
"""

import pytest
from aiogram.exceptions import TelegramBadRequest
from helpers import set_window, window_text

import keyboards as kb
from markup import Report, Table
from messaging import document_payload

CHAT = 1


async def test_first_window_is_sent_and_remembered(db, ui, bot):
    await ui.window(CHAT, "экран", kb.to_staff_menu_kb())
    assert len(bot.sent) == 1
    row = await db.get_window(CHAT)
    assert row["message_id"] == bot.sent[0][1]
    assert row["text"] == "экран"


async def test_repeated_window_edits_in_place(db, ui, bot):
    await ui.window(CHAT, "первый", kb.to_staff_menu_kb())
    message_id = (await db.get_window(CHAT))["message_id"]
    await ui.window(CHAT, "второй", kb.to_staff_menu_kb())
    assert len(bot.sent) == 1
    assert bot.edited == [(CHAT, message_id, "второй")]
    assert await window_text(db, CHAT) == "второй"


async def test_not_modified_is_not_an_error(db, ui, bot):
    await ui.window(CHAT, "экран", kb.to_staff_menu_kb())
    bot.edit_error = "Bad Request: message is not modified"
    await ui.window(CHAT, "экран", kb.to_staff_menu_kb())
    assert bot.deleted == []
    assert len(bot.sent) == 1


async def test_lost_window_is_replaced_send_first_then_delete(db, ui, bot):
    await ui.window(CHAT, "первый", kb.to_staff_menu_kb())
    old_id = (await db.get_window(CHAT))["message_id"]
    bot.edit_error = "Bad Request: message to edit not found"
    await ui.window(CHAT, "второй", kb.to_staff_menu_kb())
    new_id = (await db.get_window(CHAT))["message_id"]
    assert new_id != old_id
    assert bot.sent[-1] == (CHAT, new_id, "второй")
    assert (CHAT, old_id) in bot.deleted


async def test_failed_send_keeps_previous_window(db, ui, bot):
    """Отправка сначала, удаление потом: неудача оставляет рабочий экран."""
    await ui.window(CHAT, "рабочий экран", kb.to_staff_menu_kb())
    old_id = (await db.get_window(CHAT))["message_id"]
    bot.edit_error = "Bad Request: message to edit not found"
    bot.send_error_chats = {CHAT}
    with pytest.raises(TelegramBadRequest):
        await ui.window(CHAT, "новый экран", kb.to_staff_menu_kb())
    assert bot.deleted == []
    assert (await db.get_window(CHAT))["message_id"] == old_id
    assert await window_text(db, CHAT) == "рабочий экран"


async def test_tap_on_foreign_message_never_deletes_it(db, ui, bot):
    """Нажали кнопку на устаревшем сообщении — оно может быть Записью,
    удалять его нельзя ни при каких условиях."""
    await set_window(db, CHAT, 500, "текущий экран")
    await ui.window(CHAT, "новый экран", kb.to_staff_menu_kb(),
                    source_message_id=42)
    assert (CHAT, 42) not in bot.deleted
    assert bot.edited[-1][1] == 500          # правим окно, а не нажатое


async def test_window_adopts_tapped_message_when_pointer_is_lost(db, ui, bot):
    await ui.window(CHAT, "экран", kb.to_staff_menu_kb(), source_message_id=77)
    assert bot.edited == [(CHAT, 77, "экран")]
    assert (await db.get_window(CHAT))["message_id"] == 77


async def test_record_has_no_buttons_and_survives(db, ui, bot):
    await ui.window(CHAT, "экран", kb.to_staff_menu_kb())
    record_id = await ui.send_record(CHAT, {"text": "Заказ №1 · 300 ₽"})
    assert bot.last_markup is None
    assert (CHAT, record_id) not in bot.deleted


async def test_reanchor_moves_window_below_record_keeping_content(db, ui, bot):
    """Уведомление приходит — экран оплаты появляется под ним нетронутым."""
    await ui.window(CHAT, "<b>К оплате: 300 ₽</b>", kb.payment_kb("no:std"))
    old_id = (await db.get_window(CHAT))["message_id"]
    await ui.send_record(CHAT, {"text": "Вам начислен бонус"})
    await ui.reanchor(CHAT)
    row = await db.get_window(CHAT)
    assert row["message_id"] != old_id
    assert row["text"] == "<b>К оплате: 300 ₽</b>"
    assert bot.sent[-1][2] == "<b>К оплате: 300 ₽</b>"
    assert (CHAT, old_id) in bot.deleted
    assert bot.last_markup is not None        # кнопка «Оплата получена» на месте


async def test_reanchor_failure_keeps_old_window(db, ui, bot):
    await ui.window(CHAT, "экран", kb.to_staff_menu_kb())
    old_id = (await db.get_window(CHAT))["message_id"]
    bot.send_error_chats = {CHAT}
    await ui.reanchor(CHAT)
    assert (await db.get_window(CHAT))["message_id"] == old_id
    assert bot.deleted == []


async def test_legacy_window_is_disarmed_not_deleted(db, ui, bot):
    """Окно от прежней версии бота не удаляется: под ним может лежать
    документ резервной копии. У него снимаются кнопки."""
    async with db.write() as tx:
        await db.save_window(tx, CHAT, 321, text="", markup_json=None, rich=False)
    await ui.reanchor(CHAT)
    assert bot.deleted == []
    assert bot.markup_edits == [(CHAT, 321, None)]
    assert await db.get_window(CHAT) is None
    assert bot.sent == []


async def test_legacy_window_is_not_deleted_when_the_screen_changes(db, ui, bot):
    """Тот же запрет на пути обычной отрисовки: нередактируемый документ
    заменяется новым Окном, но сам остаётся в чате."""
    async with db.write() as tx:
        await db.save_window(tx, CHAT, 321, text="", markup_json=None, rich=False)
    bot.edit_error = "Bad Request: there is no text in the message to edit"
    await ui.window(CHAT, "меню", kb.to_staff_menu_kb())
    assert bot.deleted == []
    assert bot.markup_edits == [(CHAT, 321, None)]
    assert (await db.get_window(CHAT))["message_id"] == bot.sent[-1][1]


async def test_document_record_is_sent_with_caption(db, ui, bot):
    await ui.send_record(CHAT, {
        "text": "<b>Резервная копия базы</b>",
        "document": document_payload("backup.db.gz", b"data"),
    })
    chat_id, _, caption, document = bot.documents[0]
    assert chat_id == CHAT and document.filename == "backup.db.gz"
    assert "Резервная копия" in caption
    assert bot.deleted == []


async def test_document_record_survives_next_screen(db, ui, bot):
    """Документ бэкапа больше не становится окном и не удаляется."""
    await ui.window(CHAT, "меню", kb.to_vrheaven_menu_kb())
    await ui.send_record(CHAT, {
        "text": "копия", "document": document_payload("b.db.gz", b"x")})
    document_id = bot.documents[0][1]
    await ui.reanchor(CHAT)
    await ui.window(CHAT, "другой экран", kb.to_vrheaven_menu_kb())
    assert (CHAT, document_id) not in bot.deleted


async def test_rich_report_is_sent_as_native_table(db, ui, bot):
    report = Report("Сводка").add(Table(["Логин", "Сумма"], [["adm1", "1 500"]]))
    await ui.window(CHAT, report, kb.to_vrheaven_menu_kb())
    assert len(bot.rich_sent) == 1
    assert "<table>" in bot.rich_sent[0][2]


async def test_rich_failure_demotes_to_monospace(db, ui, bot):
    """Нативные таблицы — свежая часть Bot API. Отказ сервера переводит
    бота на моноширинные таблицы, а не оставляет его без отчётов."""
    bot.rich_error = "Bad Request: rich messages are not supported"
    report = Report("Сводка").add(Table(["A"], [["1"]]))
    await ui.window(CHAT, report, kb.to_vrheaven_menu_kb())
    assert ui.rich_enabled is False
    assert "<pre>" in bot.sent[-1][2]
    assert "<table>" not in bot.sent[-1][2]


async def test_only_the_window_carries_buttons(db, ui, bot):
    """Инвариант в одну строку: кнопки есть ровно у одного живого сообщения."""
    await ui.window(CHAT, "меню", kb.staff_admin_menu_kb())
    for text in ("Заказ №1", "Заказ №2", "Вам начислен бонус"):
        await ui.send_record(CHAT, {"text": text})
    await ui.reanchor(CHAT)
    await ui.window(CHAT, "статистика", kb.to_staff_menu_kb())
    interactive = bot.interactive(CHAT)
    assert len(interactive) == 1
    assert interactive[0] == (await db.get_window(CHAT))["message_id"]
