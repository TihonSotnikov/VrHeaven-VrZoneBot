"""Проверка деловых инвариантов над файлом базы.

Эти утверждения о деньгах не зависят от кода бота, поэтому их запускают и
по рабочей базе, и по резервной копии, и во время разбора происшествий.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

from helpers import create_admin, create_owner, make_order

from invariants import (
    EXIT_BROKEN_FILE,
    EXIT_BUSINESS,
    EXIT_OK,
    check_database,
    inspect_database,
)


async def _seeded(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=790, admin_share=50, series_pos=1)
    await make_order(db, admin, owner, price=300, admin_share=100, series_pos=2)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    return owner, admin


async def test_healthy_database_has_no_findings(db):
    await _seeded(db)
    assert check_database(db.path) == []


async def test_broken_owner_share_is_reported(db):
    await _seeded(db)
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET owner_share = 1 WHERE id = 1")
    problems = check_database(db.path)
    assert any("доля владельца" in p for p in problems)


async def test_ladder_mismatch_is_reported(db):
    await _seeded(db)
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET admin_share = 999 WHERE id = 1")
    problems = check_database(db.path)
    assert any("не соответствует ступени" in p for p in problems)


async def test_payout_that_does_not_reconcile_is_reported(db):
    await _seeded(db)
    async with db.write() as tx:
        await tx.execute("UPDATE payouts SET amount = amount + 100")
    problems = check_database(db.path)
    assert any("по строкам" in p for p in problems)


async def test_duplicate_series_step_is_reported(db):
    """Ровно та поломка, которую раньше давала гонка двух устройств."""
    await _seeded(db)
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET series_pos = 1 WHERE id = 2")
        await tx.execute("UPDATE orders SET admin_share = 50 WHERE id = 2")
    problems = check_database(db.path)
    assert any("одно место" in p for p in problems)


async def test_duplicate_active_handle_is_reported(db):
    await create_owner(db, "club")
    async with db.write() as tx:
        await tx.execute("DROP INDEX idx_users_active_handle")
        await db.create_user(tx, "owner", "club", "Двойник", "", "x")
    problems = check_database(db.path)
    assert any("логин «club»" in p for p in problems)


async def test_share_without_owner_is_reported(db):
    admin = await create_admin(db)
    await make_order(db, admin, price=300)
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET owner_share = 50 WHERE id = 1")
    problems = check_database(db.path)
    assert any("без владельца" in p for p in problems)


def test_checker_runs_over_an_arbitrary_file(tmp_path):
    """Скрипт применим к любому файлу, а не только к живой базе бота."""
    path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, handle TEXT, role TEXT,
            is_active INTEGER DEFAULT 1, deleted_at TEXT, series_reset_min INTEGER);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, price REAL, admin_share REAL,
            owner_share REAL, owner_percent REAL, series_pos INTEGER,
            owner_id INTEGER, cancelled_at TEXT, created_at TEXT, admin_id INTEGER,
            client_token TEXT);
        CREATE TABLE payouts (id INTEGER PRIMARY KEY, user_id INTEGER,
            amount REAL, orders_count INTEGER);
        CREATE TABLE bonuses (id INTEGER PRIMARY KEY, payout_id INTEGER, amount REAL);
        CREATE TABLE promos (id INTEGER PRIMARY KEY, name TEXT, archived_at TEXT);
    """)
    conn.commit()
    conn.close()
    assert check_database(path) == []


async def test_money_off_the_kopeck_grid_is_reported(db):
    """Прежняя проверка сравнивала значение само с собой и не могла
    сработать ни при какой порче данных."""
    owner, admin = await _seeded(db)
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET price = 300.005 WHERE id = 2")
    problems = check_database(db.path)
    assert any("не кратно копейке" in p for p in problems)


async def test_kopeck_check_tolerates_ordinary_float_noise(db):
    """0.1 + 0.2 != 0.3 не должно объявляться порчей данных."""
    admin = await create_admin(db)
    await make_order(db, admin, price=round(0.1 + 0.2, 2) + 1000)
    assert not any("копейке" in p for p in check_database(db.path))


async def test_bonus_and_payout_amounts_are_checked_too(db):
    owner, admin = await _seeded(db)
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], 500, "за смену")
        await tx.execute("UPDATE bonuses SET amount = 500.001")
        await tx.execute("UPDATE payouts SET amount = amount + 0.001")
    problems = check_database(db.path)
    assert any(p.startswith("бонус") and "копейке" in p for p in problems)
    assert any(p.startswith("выплата") and "копейке" in p for p in problems)


async def test_a_payout_of_offsetting_bonuses_is_not_called_empty(db):
    """Период из бонуса и равного ему удержания закрывается законно
    (SPEC §4), а выплата по нему выходит нулевой при нулевой сумме
    бонусов. Пустой она от этого не становится: закрыты две настоящие
    строки. Проверка по сумме объявляла такую выплату пустой, и одна
    законная выплата навсегда сажала на суточную тревогу о расхождении."""
    admin = await create_admin(db)
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], 500, "премия")
        await db.create_bonus(tx, admin["id"], -500, "удержание")
    async with db.write() as tx:
        payout = await db.create_payout(tx, admin)
    assert payout is not None and payout[1] == 0
    assert check_database(db.path) == []


async def test_a_payout_that_closed_nothing_is_still_called_empty(db):
    """Проверка не ослабла: выплата, не закрывшая ни одной строки,
    по-прежнему видна."""
    admin = await create_admin(db)
    async with db.write() as tx:
        await tx.execute(
            "INSERT INTO payouts (user_id, amount, orders_count, created_at)"
            " VALUES (?, 0, 0, ?)", (admin["id"], "2026-08-20T08:00:00+00:00"))
    assert any("пустая" in p for p in check_database(db.path))


# ------------------- Пригодность файла отдельно от денежных расхождений

async def test_money_discrepancy_leaves_the_file_usable(db):
    """Расхождение в деньгах и непригодный файл — разные беды с разными
    последствиями: по первой копия снимается и разворачивается, по второй
    нет. Раньше они были неотличимы, и одна спорная строка в учёте
    останавливала копии, выкладку и запуск."""
    await _seeded(db)
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET owner_share = 1 WHERE id = 1")
    report = inspect_database(db.path)
    assert report.business and not report.integrity
    assert report.problems == [*report.integrity, *report.business]


def _run_checker(path: str) -> subprocess.CompletedProcess:
    root = Path(__file__).parent.parent
    return subprocess.run([sys.executable, str(root / "invariants.py"), path],
                          capture_output=True, text=True, cwd=root)


async def test_checker_exit_code_separates_the_two_kinds(db):
    """Выкладка и откат читают код возврата, а не текст: деньги, которые
    не сходятся, не должны останавливать ни то, ни другое."""
    await _seeded(db)
    assert _run_checker(db.path).returncode == EXIT_OK
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET owner_share = 1 WHERE id = 1")
    done = _run_checker(db.path)
    assert done.returncode == EXIT_BUSINESS
    assert "доля владельца" in done.stdout


def test_checker_exit_code_for_an_unusable_file(tmp_path):
    path = tmp_path / "garbage.db"
    path.write_bytes("это не база данных".encode() * 100)
    assert _run_checker(str(path)).returncode == EXIT_BROKEN_FILE
