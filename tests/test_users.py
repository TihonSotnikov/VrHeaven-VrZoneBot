"""Учётные записи, периоды и выплаты на уровне данных."""

from decimal import Decimal

from helpers import bind, create_admin, create_owner, make_order

from db import DEFAULT_PERCENTS, SETTINGS_DEFAULTS


def cents(x: float) -> Decimal:
    return Decimal(repr(round(x, 2)))


async def test_default_percents_thirty_owner_zero_admin(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    assert DEFAULT_PERCENTS == {"owner": 30, "admin": 0}
    assert owner["percent"] == 30 and admin["percent"] == 0


async def test_explicit_percent_overrides_default(db):
    owner = await create_owner(db, percent=12.5)
    assert owner["percent"] == 12.5


async def test_handle_belongs_only_to_an_active_account(db):
    owner = await create_owner(db, "club")
    assert await db.handle_available("club") is False
    async with db.write() as tx:
        await db.set_user_active(tx, owner["id"], False)
    assert await db.handle_available("club") is True
    async with db.write() as tx:
        await db.set_user_active(tx, owner["id"], True)
    assert await db.handle_available("club") is False
    async with db.write() as tx:
        await db.delete_user(tx, owner["id"])
    assert await db.handle_available("club") is True


async def test_reissued_handle_keeps_old_history(db):
    first = await create_owner(db, "club")
    admin = await create_admin(db, first)
    await make_order(db, admin, first, price=300)
    async with db.write() as tx:
        await db.delete_user(tx, admin["id"])
        await db.delete_user(tx, first["id"])
    second = await create_owner(db, "club")
    assert second["id"] != first["id"]
    orders = await db.export_orders()
    assert len(orders) == 1 and orders[0]["owner_handle"] == "club"


async def test_deleted_user_loses_access_on_every_device(db):
    owner = await create_owner(db)
    await bind(db, owner, 101)
    await bind(db, owner, 102)
    async with db.write() as tx:
        await db.delete_user(tx, owner["id"])
    assert await db.get_user_by_chat(101) is None
    assert await db.chats_for_user(owner["id"]) == []


async def test_shares_are_paid_independently(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        payout = await db.create_payout(tx, admin)
    assert payout[1] == 50.0
    order = await db.get_order(1)
    assert order["admin_payout_id"] is not None
    assert order["owner_payout_id"] is None
    assert (await db.owner_unpaid_total(owner["id"]))["due_sum"] == 90.0


async def test_second_payout_covers_only_new_orders(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    await make_order(db, admin, owner, price=500, series_pos=2)
    async with db.write() as tx:
        second = await db.create_payout(tx, admin)
    assert second[1] == 100.0 and second[2] == 1


async def test_payout_amount_is_cent_exact_over_messy_orders(db):
    owner = await create_owner(db, percent=33.3)
    admin = await create_admin(db, owner)
    prices = [0.01, 9.99, 123.45, 999.99, 54321.09]
    for index, price in enumerate(prices, start=1):
        await make_order(db, admin, owner, price=price, series_pos=index)
    async with db.write() as tx:
        payout = await db.create_payout(tx, owner)
    expected = sum(cents(round(p * 33.3 / 100, 2)) for p in prices)
    assert cents(payout[1]) == expected


async def test_payout_that_closes_nothing_is_not_created(db):
    """Пустых записей о выплате не бывает: не закрыв ни одной строки,
    выплата удаляет саму себя."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        assert await db.create_payout(tx, owner) is not None
    async with db.write() as tx:
        assert await db.create_payout(tx, owner) is None
    assert len(await db.payouts_for_user(owner["id"])) == 1


async def test_periods_are_isolated_between_accounts(db):
    owner = await create_owner(db)
    other = await create_owner(db, "own2")
    mine = await create_admin(db, owner, "mine")
    theirs = await create_admin(db, other, "theirs")
    await make_order(db, mine, owner, price=300)
    await make_order(db, theirs, other, price=500)
    assert (await db.admin_unpaid_total(mine["id"]))["orders_count"] == 1
    assert (await db.owner_unpaid_total(owner["id"]))["share_sum"] == 90.0
    assert (await db.owner_unpaid_total(other["id"]))["share_sum"] == 150.0


async def test_cancelled_order_leaves_calculations_but_stays_in_export(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.cancel_order(tx, order_id, for_self=False)
    assert (await db.admin_unpaid_total(admin["id"]))["orders_count"] == 0
    exported = await db.export_orders()
    assert len(exported) == 1 and exported[0]["cancelled_at"] is not None


async def test_deleted_owner_leaves_admins_working(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    async with db.write() as tx:
        await db.delete_user(tx, owner["id"])
    await make_order(db, admin, price=300)
    assert (await db.admin_unpaid_total(admin["id"]))["due_sum"] == 50.0


async def test_vrheaven_remainder_covers_orders_with_any_open_side(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)          # 300 − 50 − 90 = 160
    async with db.write() as tx:
        await db.create_payout(tx, admin)                  # доля админа закрыта
    total = await db.vrheaven_unpaid_total()
    assert total["orders_count"] == 1 and total["share_sum"] == 160.0
    async with db.write() as tx:
        await db.create_payout(tx, owner)
    assert (await db.vrheaven_unpaid_total())["orders_count"] == 0


async def test_unpayable_share_of_a_deleted_beneficiary_stays_with_vrheaven(db):
    """Доля удалённого получателя не будет выплачена никогда — значит,
    она остаётся у VR Heaven, а не исчезает из отчёта."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.delete_user(tx, owner["id"])
    total = await db.vrheaven_unpaid_total()
    assert total["orders_count"] == 1
    assert total["share_sum"] == 250.0                     # 300 − 50


async def test_settings_defaults_and_overrides(db):
    settings = await db.get_settings()
    assert settings["price_1_30"] == SETTINGS_DEFAULTS["price_1_30"]
    async with db.write() as tx:
        await db.set_setting(tx, "price_1_30", 350)
    assert (await db.get_settings())["price_1_30"] == 350


async def test_owner_admins_summary_aggregates_current_period(db):
    owner = await create_owner(db)
    first = await create_admin(db, owner, "a1")
    second = await create_admin(db, owner, "a2")
    await make_order(db, first, owner, price=300)
    await make_order(db, first, owner, price=500, series_pos=2)
    rows = {r["handle"]: r for r in await db.owner_admins_summary(owner["id"])}
    assert rows["a1"]["orders_count"] == 2 and rows["a1"]["turnover"] == 800
    assert rows["a2"]["orders_count"] == 0
    assert second["handle"] in rows


# -------- Остаток VR Heaven: доля уходит, если выплачена или будет выплачена

async def test_paid_share_leaves_the_remainder_even_if_the_recipient_is_deleted(db):
    """Удаление получателя не возвращает VR Heaven деньги, которые он
    уже отдал: доля выплачена — значит, её в остатке нет."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)      # 300 − 50 − 90 = 160
    async with db.write() as tx:
        await db.create_payout(tx, admin)              # 50 ₽ реально выплачены
        await db.delete_user(tx, admin["id"])
    total = await db.vrheaven_unpaid_total()
    assert total["orders_count"] == 1
    assert cents(total["share_sum"]) == cents(160)


async def test_unpaid_share_of_a_deleted_direct_admin_stays_visible(db):
    """У прямого администратора владельца нет: если его долю списать
    вместе с ним, заказ исчезал из сводки целиком, вместе с остатком."""
    admin = await create_admin(db)                     # без владельца
    await make_order(db, admin, price=300)
    async with db.write() as tx:
        await db.delete_user(tx, admin["id"])
    total = await db.vrheaven_unpaid_total()
    assert total["orders_count"] == 1
    assert cents(total["share_sum"]) == cents(300)     # платить некому — всё у VR Heaven


async def test_fully_paid_order_leaves_the_summary(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
        await db.create_payout(tx, owner)
    assert (await db.vrheaven_unpaid_total())["orders_count"] == 0


async def test_settlement_is_offered_when_bonus_cancels_the_share(db):
    """Доля 250 и удержание −250 дают ноль, но строки настоящие: без
    выплаты они остаются открытыми навсегда."""
    admin = await create_admin(db)
    await make_order(db, admin, price=1000, series_pos=5)     # доля 250
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], -250.0, "удержание")
    total = await db.admin_unpaid_total(admin["id"])
    assert cents(total["due_sum"]) == cents(0)
    assert db.is_payable(total)
    async with db.write() as tx:
        result = await db.create_payout(tx, admin)
    assert result is not None and cents(result[1]) == cents(0)
    assert not db.is_payable(await db.admin_unpaid_total(admin["id"]))


async def test_nothing_accrued_is_still_not_payable(db):
    admin = await create_admin(db)
    owner = await create_owner(db)
    assert not db.is_payable(await db.admin_unpaid_total(admin["id"]))
    assert not db.is_payable(await db.owner_unpaid_total(owner["id"]))


async def test_net_withholding_is_carried_forward_not_paid(db):
    """Удержание больше начисленного не выплачивается: минус переносится."""
    admin = await create_admin(db)
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], -300.0, "удержание")
    total = await db.admin_unpaid_total(admin["id"])
    assert cents(total["due_sum"]) == cents(-300)
    assert not db.is_payable(total)


# ---------------- Заказ при приостановленном владельце обязан закрываться

async def _order_under_a_suspended_owner(db):
    """Ровно то, что пишет бот: связь с клубом сохранена, доля 0,
    пометка owner_suspended (SPEC §7)."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    async with db.write() as tx:
        await db.set_user_active(tx, owner["id"], False)
    order_id = await make_order(db, admin, owner, price=300, owner_percent=0)
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET owner_suspended = 1 WHERE id = ?",
                         (order_id,))
    return owner, admin, order_id


async def test_order_of_a_suspended_owner_keeps_the_club_and_closes(db):
    """Доля нулевая, но заказ настоящий и принадлежит клубу. Пока период
    владельца считался неоплачиваемым, такой заказ оставался открытым
    навсегда и вечно висел в остатке VR Heaven."""
    owner, admin, order_id = await _order_under_a_suspended_owner(db)
    order = await db.get_order(order_id)
    assert order["owner_id"] == owner["id"] and order["owner_suspended"] == 1
    assert cents(order["owner_share"]) == cents(0)

    total = await db.owner_unpaid_total(owner["id"])
    assert total["orders_count"] == 1 and cents(total["due_sum"]) == cents(0)
    assert db.is_payable(total), "настоящая строка обязана поддаваться закрытию"
    async with db.write() as tx:
        result = await db.create_payout(tx, owner)
    assert result is not None and cents(result[1]) == cents(0) and result[2] == 1
    assert (await db.get_order(order_id))["owner_payout_id"] is not None


async def test_suspended_owner_order_leaves_the_vrheaven_remainder(db):
    """Обе стороны закрыты — заказ уходит из сводки текущего периода."""
    owner, admin, order_id = await _order_under_a_suspended_owner(db)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    total = await db.vrheaven_unpaid_total()
    assert total["orders_count"] == 1
    assert cents(total["share_sum"]) == cents(250)          # 300 − 50 − 0
    async with db.write() as tx:
        await db.create_payout(tx, owner)
    assert (await db.vrheaven_unpaid_total())["orders_count"] == 0


async def test_mixed_period_closes_zero_and_paying_orders_together(db):
    """Смешанный период: заказ при приостановке и заказ после неё."""
    owner, admin, _ = await _order_under_a_suspended_owner(db)
    async with db.write() as tx:
        await db.set_user_active(tx, owner["id"], True)
    await make_order(db, admin, owner, price=300, series_pos=2)   # доля 90
    total = await db.owner_unpaid_total(owner["id"])
    assert total["orders_count"] == 2 and cents(total["due_sum"]) == cents(90)
    async with db.write() as tx:
        result = await db.create_payout(tx, owner)
    assert cents(result[1]) == cents(90) and result[2] == 2


async def test_offsetting_bonuses_alone_still_close_the_period(db):
    """Бонус и равное ему удержание без заказов: сумма ноль, строки
    настоящие. Раньше такой период не закрывался ничем."""
    admin = await create_admin(db)
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], 300.0, "премия")
        await db.create_bonus(tx, admin["id"], -300.0, "удержание")
    total = await db.admin_unpaid_total(admin["id"])
    assert total["bonus_count"] == 2 and cents(total["due_sum"]) == cents(0)
    assert db.is_payable(total)
    async with db.write() as tx:
        result = await db.create_payout(tx, admin)
    assert result is not None and cents(result[1]) == cents(0)
    assert await db.admin_unpaid_bonuses(admin["id"]) == []
