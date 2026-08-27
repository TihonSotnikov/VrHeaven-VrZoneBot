"""Плановые задачи: расписание, отчёты дня выплат, копии, сводка, уборка."""

from datetime import timedelta

from conftest import VR_ADMIN_IDS
from helpers import (
    bind,
    create_admin,
    create_owner,
    drain,
    make_order,
    set_window,
)

import backup as bk
import errors
from fsm_storage import SQLiteStorage
from scheduler import (
    backup_job,
    digest_job,
    housekeeping_job,
    payout_day_job,
    restore_check_job,
    setup_scheduler,
)
from utils import utcnow

OWNER_CHAT = 201
ADMIN_CHAT = 101


def test_jobs_are_scheduled_in_the_configured_timezone(db, ui, config):
    scheduler = setup_scheduler(db, ui, config)
    jobs = {job.func.__name__: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"payout_day_job", "backup_job", "digest_job",
                         "restore_check_job", "housekeeping_job"}
    payout = {f.name: str(f) for f in jobs["payout_day_job"].trigger.fields}
    assert payout["day"] == "1,15" and payout["hour"] == "10"
    backup = {f.name: str(f) for f in jobs["backup_job"].trigger.fields}
    assert backup["day"] == "*" and backup["hour"] == "3"
    for job in jobs.values():
        assert job.misfire_grace_time == 3600
        assert str(job.trigger.timezone) == str(config.tz)


async def test_payout_day_reports_reach_everyone_with_a_share(
        db, ui, config, bot, worker):
    owner = await create_owner(db)
    earning = await create_admin(db, owner, "earning")
    idle = await create_admin(db, owner, "idle")
    await bind(db, owner, OWNER_CHAT)
    await bind(db, earning, ADMIN_CHAT)
    await bind(db, idle, 102)
    await make_order(db, earning, owner, price=790, admin_share=79)

    await payout_day_job(db, ui, config)
    await drain(worker)
    reached = {c for c, _, _ in bot.sent}
    assert VR_ADMIN_IDS <= reached
    assert {OWNER_CHAT, ADMIN_CHAT} <= reached
    assert 102 not in reached                       # без заказов — без отчёта

    owner_report = bot.records(OWNER_CHAT)[0]
    assert "К выплате на " in owner_report
    assert "Выплата проводится вручную" in owner_report
    # период закрывает выплата, а не календарь — обещания «период закрыт» нет
    assert "Учётный период закрыт" not in owner_report
    # приписка ровно одна: сегодняшняя заменяет обычную про календарь,
    # иначе внизу стояли бы две фразы об одном и том же
    import reports as rp
    assert rp.PERIOD_NOTE not in owner_report
    assert rp.PERIOD_NOTE not in bot.records(ADMIN_CHAT)[0]
    vr_report = bot.records(next(iter(VR_ADMIN_IDS)))[0]
    assert "Остаток VR Heaven: 474 ₽" in vr_report


async def test_payout_day_is_idempotent_within_a_day(db, ui, config, bot, worker):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await bind(db, owner, OWNER_CHAT)
    await make_order(db, admin, owner, price=300)
    await payout_day_job(db, ui, config)
    await payout_day_job(db, ui, config)
    await drain(worker)
    assert len(bot.records(OWNER_CHAT)) == 1


async def test_backup_job_sends_a_document_that_is_never_deleted(
        db, ui, config, bot, worker):
    admin_id = next(iter(VR_ADMIN_IDS))
    await set_window(db, admin_id, 50)
    await backup_job(db, ui, config)
    await drain(worker)
    assert {d[0] for d in bot.documents} == set(VR_ADMIN_IDS)
    document_id = next(d[1] for d in bot.documents if d[0] == admin_id)
    assert (admin_id, document_id) not in bot.deleted
    assert bk.list_backups(config.backup_dir)
    log = await db.export_audit()
    assert log[-1]["action"] == "backup.create"


async def test_backup_job_keeps_the_file_when_telegram_fails(db, ui, config, bot, worker):
    bot.send_error_chats = set(VR_ADMIN_IDS)
    await backup_job(db, ui, config)
    await worker.drain()
    assert len(bk.list_backups(config.backup_dir)) == 1


async def test_backup_failure_alerts_super_admins(db, ui, config, bot, worker, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("диск переполнен")

    monkeypatch.setattr(bk, "create_backup", broken)
    await backup_job(db, ui, config)
    await drain(worker)
    alerts = bot.records(next(iter(VR_ADMIN_IDS)))
    assert alerts and "Резервная копия не создана" in alerts[0]
    assert "диск переполнен" in alerts[0]


async def test_restore_check_reports_success(db, ui, config, bot, worker):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    await bk.create_backup(config, bk.REASON_DAILY)
    await restore_check_job(db, ui, config)
    await drain(worker)
    text = bot.records(next(iter(VR_ADMIN_IDS)))[0]
    assert "Проверка резервной копии: ОК" in text and "заказов: 1" in text


async def test_restore_check_alerts_when_there_are_no_backups(db, ui, config, bot, worker):
    await restore_check_job(db, ui, config)
    await drain(worker)
    assert "Копий нет вовсе" in bot.records(next(iter(VR_ADMIN_IDS)))[0]


async def test_daily_digest_puts_delivery_and_errors_in_front_of_a_human(
        db, ui, config, bot, worker):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    errors.COUNTERS["errors"] = 3
    await digest_job(db, ui, config)
    await drain(worker)
    text = bot.records(next(iter(VR_ADMIN_IDS)))[0]
    assert "Сводка дня" in text
    assert "Ошибок в работе: 3" in text
    assert "Доставка:" in text
    assert "Копий базы нет" in text
    assert errors.COUNTERS["errors"] == 0


async def test_housekeeping_clears_abandoned_state_and_delivered_records(
        db, ui, config):
    storage = SQLiteStorage(db)
    from aiogram.fsm.storage.base import StorageKey
    key = StorageKey(bot_id=1, chat_id=7, user_id=7)
    await storage.set_data(key, {"x": 1})
    old = (utcnow() - timedelta(days=60)).isoformat(timespec="seconds")
    async with db.write() as tx:
        await tx.execute("UPDATE fsm_state SET updated_at = ?", (old,))
        await db.save_window(tx, 4242, 1, text="забытое", markup_json=None,
                             rich=False)
        await tx.execute("UPDATE chat_windows SET updated_at = ?", (old,))
        await db.enqueue_record(tx, chat_id=1, kind="test", text="доставлено")
        await tx.execute("UPDATE outbox SET status = 'sent', created_at = ?", (old,))

    await housekeeping_job(db, ui, config, storage)
    assert await storage.get_data(key) == {}
    assert await db.get_window(4242) is None
    assert await db.fetchall("SELECT * FROM outbox") == []


async def test_housekeeping_keeps_super_admin_windows(db, ui, config):
    admin_id = next(iter(VR_ADMIN_IDS))
    old = (utcnow() - timedelta(days=90)).isoformat(timespec="seconds")
    async with db.write() as tx:
        await db.save_window(tx, admin_id, 1, text="меню", markup_json=None,
                             rich=False)
        await tx.execute("UPDATE chat_windows SET updated_at = ?", (old,))
    await housekeeping_job(db, ui, config, None)
    assert await db.get_window(admin_id) is not None
