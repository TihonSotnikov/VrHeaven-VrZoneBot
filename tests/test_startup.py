"""Запуск и останов: то, что раньше не выполнялось ни в одном тесте.

Здесь проверяются два механизма, которые видно только целиком: блокировка
каталога данных (второй экземпляр на одной базе — драка за getUpdates и за
файл) и рабочий цикл очереди Записей (тесты сливали очередь вызовом
drain(), сам цикл с пробуждением и остановом не запускался никогда).
"""

import asyncio

import pytest
from helpers import bind, create_admin, make_order, set_window

import backup as bk
import notify
from main import SingleInstance, boot_backup


def test_second_instance_on_the_same_data_directory_refuses_to_start(tmp_path):
    first = SingleInstance(str(tmp_path))
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="уже запущен"):
            SingleInstance(str(tmp_path)).acquire()
    finally:
        first.release()


def test_the_lock_is_released_for_the_next_process(tmp_path):
    first = SingleInstance(str(tmp_path))
    first.acquire()
    first.release()
    second = SingleInstance(str(tmp_path))
    second.acquire()          # не должно бросить
    second.release()


def test_different_data_directories_do_not_block_each_other(tmp_path):
    one, two = SingleInstance(str(tmp_path / "a")), SingleInstance(str(tmp_path / "b"))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    one.acquire()
    two.acquire()
    one.release()
    two.release()


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("не дождались")


async def test_worker_loop_delivers_on_wake_and_stops_cleanly(db, ui, bot, worker):
    """Рабочий цикл, а не drain(): Запись уходит по сигналу из транзакции."""
    admin = await create_admin(db)
    await bind(db, admin, 1)
    await set_window(db, 1, 50)
    worker.start()
    try:
        async with db.write() as tx:
            await notify.to_user(db, tx, admin, "<b>Чек</b>", kind="order_receipt",
                                 dedup="order:1:receipt")
        ui.wake()
        await _wait_for(lambda: bool(bot.records(1)))
        assert bot.records(1) == ["<b>Чек</b>"]
        row = await db.fetchone("SELECT status FROM outbox WHERE id = 1")
        assert row["status"] == "sent"
    finally:
        await worker.stop()
    assert worker._task is None


async def test_worker_loop_survives_a_failing_batch(db, ui, bot, worker):
    """Сбой доставки не уносит цикл: следующая Запись всё равно уходит."""
    admin = await create_admin(db)
    await bind(db, admin, 1)
    await set_window(db, 1, 50)
    bot.send_error_chats = {77}
    async with db.write() as tx:
        await notify.to_chat(db, tx, 77, "в мёртвый чат", kind="alert", dedup="dead")
        await notify.to_user(db, tx, admin, "живому", kind="order_receipt", dedup="ok")
    worker.start()
    try:
        ui.wake()
        await _wait_for(lambda: "живому" in bot.records(1))
    finally:
        await worker.stop()
    dead = await db.fetchone("SELECT status FROM outbox WHERE chat_id = 77")
    assert dead["status"] == "dropped"


# ------------------------------------------------ Копия не срывается о деньги

async def _series_with_a_cancelled_order(db):
    """Законная серия, в которой отменён заказ из середины: следующий
    заказ занял освободившуюся ступень (SPEC §3)."""
    admin = await create_admin(db)
    boundary = "2026-08-20T02:00:00+00:00"
    first = await make_order(db, admin, price=300, series_pos=1,
                             series_since=boundary,
                             created_at="2026-08-20T08:00:00+00:00")
    await make_order(db, admin, price=300, series_pos=2, series_since=boundary,
                     created_at="2026-08-20T08:10:00+00:00")
    async with db.write() as tx:
        await db.cancel_order(tx, first, for_self=False)
    await make_order(db, admin, price=300, series_pos=2, series_since=boundary,
                     created_at="2026-08-20T08:20:00+00:00")
    return admin


async def test_boot_snapshot_survives_a_cancelled_order(db, config):
    """Копия перед запуском — то состояние, к которому можно вернуться.
    Одна законная отмена делала её невозможной навсегда: проверка считала
    освободившуюся ступень порчей, копия не публиковалась, обновить бота
    было нечем."""
    await _series_with_a_cancelled_order(db)
    await boot_backup(config)
    published = bk.list_backups(config.backup_dir)
    assert published, "копия обязана появиться"
    assert published[0].reason == bk.REASON_BOOT


async def test_a_discrepancy_at_boot_is_written_to_the_journal(db, config,
                                                              caplog):
    """Копия снимается, а расхождение не пропадает: молчаливое «всё
    хорошо» здесь опаснее отказа, потому что разбирать будет некому."""
    import logging

    from main import boot_backup
    admin = await create_admin(db)
    await make_order(db, admin, price=300)
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET owner_share = 1")
    with caplog.at_level(logging.ERROR):
        await boot_backup(config)
    assert bk.list_backups(config.backup_dir), "копия обязана появиться"
    assert any("деловые инварианты не сходятся" in r.getMessage()
               for r in caplog.records)


async def test_pre_migration_snapshot_is_taken_despite_a_discrepancy(db, config):
    """Снимок перед миграцией — единственная точка отката схемы. Деньги,
    которые не сходятся, его не отменяют: без снимка мигрировать нельзя,
    а расхождение уезжает вместе с копией тревогой."""
    admin = await create_admin(db)
    await make_order(db, admin, price=300)
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET owner_share = 1")
    info = await bk.create_backup(config, bk.REASON_PRE_MIGRATION)
    assert info.problems, "расхождение обязано остаться видимым"
    assert bk.list_backups(config.backup_dir), "снимок обязан появиться"
