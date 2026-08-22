"""Запуск и останов: то, что раньше не выполнялось ни в одном тесте.

Здесь проверяются два механизма, которые видно только целиком: блокировка
каталога данных (второй экземпляр на одной базе — драка за getUpdates и за
файл) и рабочий цикл очереди Записей (тесты сливали очередь вызовом
drain(), сам цикл с пробуждением и остановом не запускался никогда).
"""

import asyncio

import pytest
from helpers import bind, create_admin, set_window

import notify
from main import SingleInstance


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
