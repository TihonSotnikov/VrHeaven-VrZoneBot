"""Проверка деловых инвариантов над файлом базы.

Эти утверждения о деньгах не зависят от кода бота, поэтому их запускают и
по рабочей базе, и по резервной копии, и во время разбора происшествий.
"""

import sqlite3

from helpers import create_admin, create_owner, make_order

from invariants import check_database


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
