"""Долговечная доставка Записей: очередь, повторы, мёртвые чаты.

Раньше уведомление, не ушедшее с первой попытки, исчезало навсегда и
молча. Здесь оно остаётся строкой в таблице до тех пор, пока не будет
доставлено или пока о нём не сообщат человеку.
"""

from datetime import timedelta

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramServerError,
)
from helpers import bind, create_admin, drain, set_window

import notify
from messaging import document_payload
from utils import utcnow, utcnow_iso

CHAT = 501


async def _enqueue(db, chat_id, text, *, kind="test", dedup=None, document=None):
    async with db.write() as tx:
        return await db.enqueue_record(tx, chat_id=chat_id, kind=kind, text=text,
                                       dedup_key=dedup, document=document)


async def test_record_is_delivered_and_marked_sent(db, worker, bot):
    await _enqueue(db, CHAT, "Заказ №1 · 300 ₽")
    assert await drain(worker) == 1
    assert bot.sent[0][0] == CHAT
    stats = await db.outbox_stats()
    assert (stats["sent"], stats["pending"]) == (1, 0)


async def test_duplicate_dedup_key_delivers_once(db, worker, bot):
    await _enqueue(db, CHAT, "Заказ №1", dedup="order:1:chat:501")
    await _enqueue(db, CHAT, "Заказ №1", dedup="order:1:chat:501")
    await drain(worker)
    assert len(bot.sent) == 1


async def test_records_are_enqueued_inside_the_business_transaction(db, worker, bot):
    """Откат бизнес-операции уносит и её уведомления."""
    admin = await create_admin(db)
    await bind(db, admin, CHAT)
    try:
        async with db.write() as tx:
            await db.create_bonus(tx, admin["id"], 500, "за смену")
            await notify.to_user(db, tx, admin, "Вам начислен бонус", kind="bonus")
            raise RuntimeError("сбой")
    except RuntimeError:
        pass
    assert await drain(worker) == 0
    assert bot.sent == []


async def test_retry_after_is_honoured_then_delivered(db, worker, bot):
    calls = {"n": 0}
    original = bot.send_message

    async def flaky(chat_id, text, reply_markup=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TelegramRetryAfter(method=None, message="Too Many Requests",
                                     retry_after=0)
        return await original(chat_id, text, reply_markup=reply_markup, **kwargs)

    bot.send_message = flaky
    await _enqueue(db, CHAT, "отчёт")
    await worker.drain()
    assert bot.sent == []
    row = (await db.fetchall("SELECT * FROM outbox"))[0]
    assert row["status"] == "pending" and row["attempts"] == 1
    # переносим срок и повторяем — сообщение уходит
    async with db.write() as tx:
        await tx.execute("UPDATE outbox SET next_attempt_at = ?", (utcnow_iso(),))
    await worker.drain()
    assert len(bot.sent) == 1


async def test_transient_failure_is_retried_with_backoff(db, worker, bot):
    async def failing(chat_id, text, reply_markup=None, **kwargs):
        raise TelegramServerError(method=None, message="Internal Server Error")

    bot.send_message = failing
    await _enqueue(db, CHAT, "отчёт")
    await worker.drain()
    row = (await db.fetchall("SELECT * FROM outbox"))[0]
    assert row["status"] == "pending"
    assert row["next_attempt_at"] > utcnow_iso()
    assert "Internal Server Error" in row["last_error"]


async def test_permanent_failure_unbinds_the_chat_once(db, worker, bot, config):
    """Заблокировавший бота пользователь отвязывается, а не тонет в журнале."""
    admin = await create_admin(db)
    await bind(db, admin, CHAT)
    await set_window(db, CHAT, 10)

    async def forbidden(chat_id, text, reply_markup=None, **kwargs):
        raise TelegramForbiddenError(method=None, message="Forbidden: bot was blocked")

    bot.send_message = forbidden
    await _enqueue(db, CHAT, "уведомление")
    await worker.drain()
    row = (await db.fetchall("SELECT * FROM outbox WHERE kind = 'test'"))[0]
    assert row["status"] == "dropped"
    assert await db.chats_for_user(admin["id"]) == []
    assert await db.get_window(CHAT) is None
    log = await db.export_audit()
    assert [r["action"] for r in log] == ["chat.unbind"]


async def test_max_attempts_raise_an_alert_to_super_admins(db, worker, bot, config):
    async def failing(chat_id, text, reply_markup=None, **kwargs):
        raise TelegramServerError(method=None, message="Internal Server Error")

    bot.send_message = failing
    await _enqueue(db, CHAT, "отчёт")
    for _ in range(9):
        async with db.write() as tx:
            await tx.execute(
                "UPDATE outbox SET next_attempt_at = ? WHERE kind = 'test'",
                (utcnow_iso(),))
        await worker.drain()
    row = (await db.fetchall("SELECT * FROM outbox WHERE kind = 'test'"))[0]
    assert row["status"] == "failed"
    alerts = await db.fetchall("SELECT * FROM outbox WHERE kind = 'alert'")
    assert {a["chat_id"] for a in alerts} == set(config.admin_ids)


async def test_window_is_reanchored_after_a_burst_of_records(db, worker, ui, bot):
    """Несколько Записей подряд переставляют Окно один раз, а не трижды."""
    import keyboards as kb
    await ui.window(CHAT, "<b>К оплате: 300 ₽</b>", kb.payment_kb("no:std"))
    first_id = (await db.get_window(CHAT))["message_id"]
    before = len(bot.sent)
    for i in range(3):
        await _enqueue(db, CHAT, f"Запись {i}")
    await drain(worker)
    row = await db.get_window(CHAT)
    assert row["text"] == "<b>К оплате: 300 ₽</b>"
    anchors = [t for _, _, t in bot.sent[before:] if t == "<b>К оплате: 300 ₽</b>"]
    assert len(anchors) == 1          # одна перестановка на всю пачку
    assert (CHAT, first_id) in bot.deleted


async def test_document_record_is_delivered_as_file(db, worker, bot):
    await _enqueue(db, CHAT, "Выгрузка", kind="export",
                   document=document_payload("orders.csv", b"id;price\n"))
    await drain(worker)
    assert bot.documents[0][3].filename == "orders.csv"


async def test_missing_file_is_dropped_not_retried_forever(db, worker, bot):
    from messaging import file_payload
    await _enqueue(db, CHAT, "копия", kind="backup",
                   document=file_payload("/нет/такого/файла.db.gz", "b.db.gz"))
    await worker.drain()
    row = (await db.fetchall("SELECT * FROM outbox WHERE kind = 'backup'"))[0]
    assert row["status"] == "dropped"


async def test_purge_keeps_only_recent_delivered_rows(db, worker):
    await _enqueue(db, CHAT, "старое")
    await drain(worker)
    async with db.write() as tx:
        await tx.execute("UPDATE outbox SET created_at = ?",
                         ((utcnow() - timedelta(days=60)).isoformat(),))
        removed = await db.purge_sent_records(
            tx, (utcnow() - timedelta(days=30)).isoformat())
    assert removed == 1
