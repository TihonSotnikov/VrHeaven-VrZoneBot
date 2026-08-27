"""Транзакции, идемпотентность и журнал действий.

Проверяется главное свойство денежного слоя: операция целиком видна или
целиком не видна, а место в лесенке считается под тем же локом, что и
вставка заказа.
"""

import asyncio

import pytest
from helpers import create_admin, create_owner, make_order

from db import Actor
from pricing import ladder_amount
from utils import utcnow_iso

SINCE = "2000-01-01T00:00:00+00:00"


async def _order(db, admin, owner=None, *, token, price=300.0):
    async with db.write() as tx:
        return await db.create_order(
            tx, client_token=token, admin_id=admin["id"], admin_percent=0,
            owner_id=owner["id"] if owner else None,
            owner_percent=owner["percent"] if owner else 0,
            owner_suspended=False, kind="standard", headsets=1, minutes=30,
            promo_id=None, promo_name=None, base_price=price,
            discount_percent=0, price=price, quoted_at=utcnow_iso(),
            series_since_iso=SINCE,
        )


async def test_rollback_leaves_nothing_behind(db):
    admin = await create_admin(db)
    with pytest.raises(RuntimeError):
        async with db.write() as tx:
            await db.create_bonus(tx, admin["id"], 500, "тест")
            raise RuntimeError("сбой посреди операции")
    assert await db.admin_unpaid_bonuses(admin["id"]) == []


async def test_nested_transaction_is_refused(db):
    """Вложенный BEGIN в SQLite молча ничего не делает — ловим это явно."""
    with pytest.raises(RuntimeError, match="Вложенная транзакция"):
        async with db.write():
            async with db.write():
                pass


async def test_repeated_token_creates_exactly_one_order(db):
    """Повтор отправки формы не может создать второй заказ."""
    admin = await create_admin(db)
    first, created_first = await _order(db, admin, token="t-1")
    second, created_second = await _order(db, admin, token="t-1")
    assert created_first is True and created_second is False
    assert first["id"] == second["id"]
    assert len(await db.export_orders()) == 1


async def test_concurrent_orders_get_distinct_ladder_steps(db):
    """Два устройства одной учётной записи дают ступени 1 и 2, а не 1 и 1."""
    admin = await create_admin(db)
    results = await asyncio.gather(
        _order(db, admin, token="a", price=300),
        _order(db, admin, token="b", price=500),
    )
    positions = sorted(order["series_pos"] for order, _ in results)
    shares = sorted(order["admin_share"] for order, _ in results)
    assert positions == [1, 2]
    assert shares == [ladder_amount(1), ladder_amount(2)]


async def test_concurrent_same_token_orders_collapse_into_one(db):
    admin = await create_admin(db)
    results = await asyncio.gather(
        _order(db, admin, token="same"),
        _order(db, admin, token="same"),
    )
    ids = {order["id"] for order, _ in results}
    assert len(ids) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_payout_and_self_cancel_cannot_both_win(db):
    """Самоотмена проверяет выплату в самом UPDATE: гонка не проскакивает."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    order_id = await make_order(db, admin, owner, price=300)

    async def cancel():
        async with db.write() as tx:
            return await db.cancel_order(tx, order_id, for_self=True)

    async def pay():
        async with db.write() as tx:
            return await db.create_payout(tx, admin)

    cancelled, payout = await asyncio.gather(cancel(), pay())
    order = await db.get_order(order_id)
    if cancelled:
        assert order["admin_payout_id"] is None
    else:
        assert payout is not None and order["admin_payout_id"] is not None


async def test_vrheaven_cancel_of_paid_order_is_allowed(db):
    """Надзорная отмена разрешена и на выплаченном заказе."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    async with db.write() as tx:
        assert await db.cancel_order(tx, order_id, for_self=False)
    async with db.write() as tx:
        assert not await db.cancel_order(tx, order_id, for_self=False)


async def test_self_cancel_of_paid_order_is_blocked(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    async with db.write() as tx:
        assert not await db.cancel_order(tx, order_id, for_self=True)


async def test_concurrent_payouts_produce_single_payout(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)

    async def pay():
        async with db.write() as tx:
            return await db.create_payout(tx, admin)

    first, second = await asyncio.gather(pay(), pay())
    assert (first is None) != (second is None)
    assert len(await db.payouts_for_user(admin["id"])) == 1


async def test_audit_is_written_in_the_same_transaction(db):
    admin = await create_admin(db)
    with pytest.raises(RuntimeError):
        async with db.write() as tx:
            bonus_id = await db.create_bonus(tx, admin["id"], 100, "за смену")
            await db.audit(tx, Actor.superadmin(999), "bonus.create", "bonus",
                           bonus_id, after={"amount": 100})
            raise RuntimeError("откат")
    assert await db.export_audit() == []
    assert await db.admin_unpaid_bonuses(admin["id"]) == []


async def test_audit_records_actor_and_change(db):
    admin = await create_admin(db)
    async with db.write() as tx:
        await db.audit(tx, Actor.superadmin(999), "user.percent", "user",
                       admin["id"], before={"percent": 0}, after={"percent": 10})
    rows = await db.audit_for_entity("user", admin["id"])
    assert len(rows) == 1
    assert rows[0]["actor_kind"] == "superadmin" and rows[0]["actor_tg_id"] == 999
    assert "10" in rows[0]["after_json"]


async def test_wal_and_foreign_keys_enabled(db):
    assert (await db.fetchone("PRAGMA journal_mode"))[0] == "wal"
    assert (await db.fetchone("PRAGMA foreign_keys"))[0] == 1


async def test_active_handle_uniqueness_enforced_by_database(db):
    """Уникальность логина держит база, а не только проверка в хендлере."""
    import sqlite3
    await create_owner(db, "dup")
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as tx:
            await db.create_user(tx, "owner", "dup", "Второй", "", "x")


async def test_promo_name_uniqueness_is_cyrillic_aware(db):
    """SQLite lower() не сворачивает кириллицу — сравниваем casefold()."""
    import sqlite3
    async with db.write() as tx:
        await db.create_promo(tx, "День Рождения")
    assert await db.promo_name_taken("день рождения") is True
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as tx:
            await db.create_promo(tx, "день рождения")


async def test_archived_promo_name_is_reusable(db):
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "День рождения")
        await db.archive_promo(tx, promo_id)
    assert await db.promo_name_taken("ДЕНЬ РОЖДЕНИЯ") is False
    async with db.write() as tx:
        await db.create_promo(tx, "ДЕНЬ РОЖДЕНИЯ")
