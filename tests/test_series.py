"""12-часовые серии вознаграждения: границы сбросов, индивидуальное время,
ночные смены и экран управления сбросами."""

from datetime import UTC, datetime

from helpers import (
    bind,
    create_admin,
    fake_cb,
    fake_msg,
    make_order,
    make_state,
    set_window,
    window_text,
)

from handlers.staff import order_duration, order_payment
from handlers.vrheaven import EditResetSG, series_reset_ask, series_reset_set

ADMIN_CHAT = 1
VR_CHAT = 999
WINDOW = 50


def _freeze(monkeypatch, moment: datetime) -> None:
    """Замораживает «сейчас» в расчёте серий (reports.series_start_utc_iso)."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr("reports.datetime", Frozen)


async def _seed(db, admin, created_utc: datetime, price=300.0):
    order_id = await make_order(db, admin, price=price)
    async with db.write() as tx:
        await tx.execute(
            "UPDATE orders SET created_at = ? WHERE id = ?",
            (created_utc.astimezone(UTC).isoformat(timespec="seconds"),
             order_id))
    return order_id


async def _place(db, ui, config, state):
    await order_duration(fake_cb("no:d:1:30", message_id=WINDOW), state, db, ui, config)
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)


async def _admin(db):
    admin = await create_admin(db)
    await bind(db, admin, ADMIN_CHAT)
    await set_window(db, ADMIN_CHAT, WINDOW)
    async with db.write() as tx:
        await db.set_setting(tx, "discount_enabled", 0)
    return admin


async def test_orders_before_the_reset_do_not_count(db, ui, config, monkeypatch):
    """Сброс в 09:00: заказ в 08:59 принадлежит прошлой серии."""
    admin = await _admin(db)
    tz = config.tz
    await _seed(db, admin, datetime(2026, 8, 12, 8, 59, tzinfo=tz))
    _freeze(monkeypatch, datetime(2026, 8, 12, 9, 1, tzinfo=tz))
    await _place(db, ui, config, make_state(db, ADMIN_CHAT))
    new_order = await db.get_order(2)
    assert new_order["series_pos"] == 1 and new_order["admin_share"] == 50.0


async def test_orders_after_the_reset_continue_the_series(db, ui, config, monkeypatch):
    admin = await _admin(db)
    tz = config.tz
    await _seed(db, admin, datetime(2026, 8, 12, 9, 30, tzinfo=tz))
    _freeze(monkeypatch, datetime(2026, 8, 12, 10, 0, tzinfo=tz))
    await _place(db, ui, config, make_state(db, ADMIN_CHAT))
    assert (await db.get_order(2))["series_pos"] == 2


async def test_individual_reset_shifts_the_boundary(db, ui, config, monkeypatch):
    admin = await _admin(db)
    async with db.write() as tx:
        await db.set_user_series_reset(tx, admin["id"], 6 * 60)
    tz = config.tz
    await _seed(db, admin, datetime(2026, 8, 12, 5, 59, tzinfo=tz))
    _freeze(monkeypatch, datetime(2026, 8, 12, 6, 1, tzinfo=tz))
    await _place(db, ui, config, make_state(db, ADMIN_CHAT))
    assert (await db.get_order(2))["series_pos"] == 1


async def test_night_shift_series_crosses_midnight(db, ui, config, monkeypatch):
    admin = await _admin(db)
    tz = config.tz
    await _seed(db, admin, datetime(2026, 8, 12, 22, 0, tzinfo=tz))
    _freeze(monkeypatch, datetime(2026, 8, 13, 2, 0, tzinfo=tz))
    await _place(db, ui, config, make_state(db, ADMIN_CHAT))
    assert (await db.get_order(2))["series_pos"] == 2


async def test_cancelled_order_does_not_hold_its_step(db, ui, config, monkeypatch):
    admin = await _admin(db)
    tz = config.tz
    seeded = await _seed(db, admin, datetime(2026, 8, 12, 10, 0, tzinfo=tz))
    async with db.write() as tx:
        await db.cancel_order(tx, seeded, for_self=False)
    _freeze(monkeypatch, datetime(2026, 8, 12, 11, 0, tzinfo=tz))
    await _place(db, ui, config, make_state(db, ADMIN_CHAT))
    assert (await db.get_order(2))["series_pos"] == 1


async def test_reset_screen_accepts_time_pair_and_dash(db, ui):
    admin = await create_admin(db)
    await set_window(db, VR_CHAT, WINDOW)
    state = make_state(db, VR_CHAT)
    await series_reset_ask(fake_cb(f"ad:reset:{admin['id']}", chat_id=VR_CHAT,
                                   message_id=WINDOW), state, db, ui)
    assert await state.get_state() == EditResetSG.value.state

    await series_reset_set(fake_msg("6:00 18:00", chat_id=VR_CHAT), state, db, ui)
    assert (await db.get_user(admin["id"]))["series_reset_min"] == 360

    await series_reset_ask(fake_cb(f"ad:reset:{admin['id']}", chat_id=VR_CHAT,
                                   message_id=WINDOW), state, db, ui)
    await series_reset_set(fake_msg("—", chat_id=VR_CHAT), state, db, ui)
    assert (await db.get_user(admin["id"]))["series_reset_min"] is None


async def test_reset_screen_rejects_a_wrong_pair(db, ui):
    admin = await create_admin(db)
    await set_window(db, VR_CHAT, WINDOW)
    state = make_state(db, VR_CHAT)
    await series_reset_ask(fake_cb(f"ad:reset:{admin['id']}", chat_id=VR_CHAT,
                                   message_id=WINDOW), state, db, ui)
    await series_reset_set(fake_msg("9:00 20:00", chat_id=VR_CHAT), state, db, ui)
    assert (await db.get_user(admin["id"]))["series_reset_min"] is None
    assert "разницей ровно 12 часов" in await window_text(db, VR_CHAT)


async def test_statistics_show_the_series_and_the_next_step(db, ui, config, monkeypatch):
    import reports
    admin = await _admin(db)
    tz = config.tz
    await _seed(db, admin, datetime(2026, 8, 12, 10, 0, tzinfo=tz))
    _freeze(monkeypatch, datetime(2026, 8, 12, 12, 0, tzinfo=tz))
    html = (await reports.admin_period(db, admin, tz)).to_html(rich=False)
    assert "Серия с 09:00: заказов 1" in html
    assert "следующий заказ — 100 ₽" in html
