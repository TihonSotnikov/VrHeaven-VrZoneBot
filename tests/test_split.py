"""Деление цены заказа: фиксированное вознаграждение администратора,
процентная доля владельца, остаток VR Heaven поглощает округления."""

from decimal import Decimal

from helpers import create_admin, create_owner, make_order

from pricing import SALARY_LADDER, ladder_amount, split


def cents(x: float) -> Decimal:
    return Decimal(repr(x))


def test_split_sums_exactly_to_price_for_all_cases():
    prices = [0.01, 10, 199.99, 300, 599.5, 790, 12345.67]
    for price in prices:
        for admin_amount in SALARY_LADDER:
            for owner_pct in (0, 0.01, 12.5, 30, 33.3, 99.99):
                admin_share, owner_share, vr_share = split(price, admin_amount, owner_pct)
                assert admin_share == admin_amount
                assert owner_share == round(price * owner_pct / 100, 2)
                assert (cents(admin_share) + cents(owner_share) + cents(vr_share)
                        == cents(round(price, 2))), (price, admin_amount, owner_pct)


def test_split_rounds_owner_share_to_cents():
    admin_share, owner_share, vr_share = split(790, 100, 33.3)
    assert admin_share == 100.0
    assert owner_share == 263.07                     # 790 × 33,3% = 263,07
    assert round(admin_share + owner_share + vr_share, 2) == 790


def test_direct_admin_split_has_no_owner_share():
    admin_share, owner_share, vr_share = split(600, 50, 0)
    assert (admin_share, owner_share, vr_share) == (50.0, 0.0, 550.0)


def test_vr_remainder_can_go_negative_on_cheap_late_orders():
    """Лесенка не зависит от цены: 5-й заказ на 160 ₽ даёт вознаграждение
    250 ₽ — остаток VR Heaven отрицательный, но сумма частей точна."""
    admin_share, owner_share, vr_share = split(160, ladder_amount(5), 30)
    assert admin_share == 250.0
    assert owner_share == 48.0
    assert vr_share == -138.0
    assert round(admin_share + owner_share + vr_share, 2) == 160


def test_split_cents_exact_over_adversarial_grid():
    """Копеечная точность на плотной сетке цен и долей владельца."""
    prices = [Decimal("0.01"), Decimal("9.99"), Decimal("10"), Decimal("99.95"),
              Decimal("100.05"), Decimal("123.45"), Decimal("999.99"),
              Decimal("1000"), Decimal("54321.09")]
    pcts = [0, 0.01, 1, 3.33, 7, 7.77, 12.5, 33.3, 49.99, 66.67, 99.99]
    for price in prices:
        for admin_amount in SALARY_LADDER:
            for owner_pct in pcts:
                a, o, v = split(float(price), admin_amount, owner_pct)
                assert cents(a) + cents(o) + cents(v) == price, (
                    price, admin_amount, owner_pct, a, o, v)


async def test_order_shares_frozen_at_creation(db):
    """Смена процента владельца не переписывает прошлые заказы."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    order_id = await make_order(db, admin, owner, price=1000, admin_share=50,
                                series_pos=1)
    async with db.write() as tx:
        await db.set_user_percent(tx, owner["id"], 50)
    order = await db.get_order(order_id)
    assert order["admin_share"] == 50.0 and order["series_pos"] == 1
    assert order["owner_percent"] == 30 and order["owner_share"] == 300.0
