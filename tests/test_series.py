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


async def test_free_session_stays_out_of_the_series_even_after_a_cancellation(
        db, ui, config, monkeypatch):
    """Бесплатный сеанс не занимает ступень и не мешает счёту после отмены:
    проверка серий над файлом базы обязана остаться чистой."""
    from handlers.staff import order_free15
    from invariants import check_database

    await _admin(db)
    async with db.write() as tx:
        await db.set_setting(tx, "free15_enabled", 1)
    tz = config.tz
    _freeze(monkeypatch, datetime(2026, 8, 12, 11, 0, tzinfo=tz))
    state = make_state(db, ADMIN_CHAT)
    await _place(db, ui, config, state)                       # №1, ступень 1
    await order_free15(fake_cb("no:free", message_id=WINDOW), state, db, ui)
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)
    await _place(db, ui, config, state)                       # №3, ступень 2
    async with db.write() as tx:
        await db.cancel_order(tx, 1, for_self=False)          # ступень 1 освободилась
    # живым остался один платный заказ, поэтому следующий снова второй —
    # ступень 2 достаётся двум заказам одной серии, и это законно (SPEC §3)
    await _place(db, ui, config, state)

    positions = [o["series_pos"] for o in await db.export_orders()]
    assert positions == [1, None, 2, 2]
    assert check_database(db.path) == []


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
    assert "Следующий заказ: 100 ₽" in html


# ------------------- Граница серии живёт в заказе, а не в настройке

async def test_order_records_the_series_boundary_it_was_counted_in(db, ui, config,
                                                                   monkeypatch):
    """Место в лесенке без границы серии проверить нечем — заказ несёт обе."""
    admin = await create_admin(db, handle="adm1")
    await bind(db, admin, ADMIN_CHAT)
    await set_window(db, ADMIN_CHAT, WINDOW)
    _freeze(monkeypatch, datetime(2026, 8, 21, 14, 0, tzinfo=UTC))
    state = make_state(db, ADMIN_CHAT)
    await order_duration(fake_cb("no:d:1:30", message_id=WINDOW), state, db, ui, config)
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)
    order = await db.get_order(1)
    assert order["series_pos"] == 1
    assert order["series_since"], "заказ обязан нести границу своей серии"
    # Граница — момент последнего сброса до оформления, не время заказа
    assert order["series_since"] < order["created_at"]


async def test_changing_reset_time_does_not_reinterpret_past_orders(db):
    """Раньше смена времени сбросов задним числом сливала соседние серии,
    инварианты объявляли это нарушением, и копия базы переставала сниматься."""
    from invariants import check_database

    admin = await create_admin(db, handle="adm1")
    # 08:00 и 10:00 UTC — по умолчанию (сбросы 09:00/21:00) это разные серии,
    # каждый заказ первый в своей
    await make_order(db, admin, price=300, series_pos=1,
                     created_at="2026-08-20T08:00:00+00:00")
    await make_order(db, admin, price=300, series_pos=1,
                     created_at="2026-08-20T10:00:00+00:00")
    assert check_database(db.path) == []

    async with db.write() as tx:
        await db.set_user_series_reset(tx, admin["id"], 7 * 60)
    assert check_database(db.path) == [], (
        "настройка изменилась — прошлые заказы обязаны остаться как были")


async def test_a_reset_moved_mid_series_is_not_a_violation(db, ui, config, monkeypatch):
    """Сброс сдвинут посреди серии: у соседних заказов границы разные, а
    считались они друг за другом.

    Проверка, складывающая заказы по совпадению границ, объявляла такую
    пару нарушением — и тревога о расхождении приходила с каждой копией
    базы, вечно, хотя ступень выдана ровно по правилу.
    """
    from invariants import check_database

    admin = await _admin(db)
    tz = config.tz
    async with db.write() as tx:
        await db.set_user_series_reset(tx, admin["id"], 6 * 60)
    # заказ серии со сбросом в 06:00, оформленный в 10:00
    await make_order(
        db, admin, price=300, series_pos=1,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=tz)
        .astimezone(UTC).isoformat(timespec="seconds"),
        series_since=datetime(2026, 8, 12, 6, 0, tzinfo=tz)
        .astimezone(UTC).isoformat(timespec="seconds"))
    # VR Heaven переносит сбросы на 09:00: граница новой серии — 09:00,
    # и заказ 10:00 попадает в неё же
    async with db.write() as tx:
        await db.set_user_series_reset(tx, admin["id"], 9 * 60)
    _freeze(monkeypatch, datetime(2026, 8, 12, 11, 0, tzinfo=tz))
    await _place(db, ui, config, make_state(db, ADMIN_CHAT))

    first, second = await db.get_order(1), await db.get_order(2)
    assert second["series_pos"] == 2, "заказ продолжает живую серию"
    assert first["series_since"] != second["series_since"]
    assert check_database(db.path) == []


async def test_cancelling_a_middle_order_is_not_a_violation(db):
    """Отмена освобождает ступень (SPEC §3): следующий заказ законно
    получает место живого заказа. Раньше проверка объявляла это порчей —
    и одна законная отмена навсегда останавливала снятие копий базы."""
    from invariants import check_database

    admin = await create_admin(db, handle="adm1")
    boundary = "2026-08-20T02:00:00+00:00"
    first = await make_order(db, admin, price=300, series_pos=1,
                             created_at="2026-08-20T08:00:00+00:00",
                             series_since=boundary)
    await make_order(db, admin, price=300, series_pos=2,
                     created_at="2026-08-20T08:10:00+00:00", series_since=boundary)
    async with db.write() as tx:
        await db.cancel_order(tx, first, for_self=False)
    # неотменённых в серии осталось 1 — следующий заказ идёт вторым,
    # ровно как живой заказ №2
    await make_order(db, admin, price=300, series_pos=2,
                     created_at="2026-08-20T08:20:00+00:00", series_since=boundary)
    assert check_database(db.path) == []


async def test_the_whole_series_may_be_cancelled_and_started_over(db):
    """Отменены все заказы серии — счёт начинается заново с первой ступени."""
    from invariants import check_database

    admin = await create_admin(db, handle="adm1")
    boundary = "2026-08-20T02:00:00+00:00"
    for pos in (1, 2, 3):
        order_id = await make_order(
            db, admin, price=300, series_pos=pos, series_since=boundary,
            created_at=f"2026-08-20T08:0{pos}:00+00:00")
        async with db.write() as tx:
            await db.cancel_order(tx, order_id, for_self=False)
    await make_order(db, admin, price=300, series_pos=1, series_since=boundary,
                     created_at="2026-08-20T08:30:00+00:00")
    assert check_database(db.path) == []


async def test_a_step_above_the_series_length_is_reported(db):
    """Ступень выше, чем заказов в серии, отменой не объясняется:
    отмена место освобождает, а не выдаёт новое."""
    from invariants import check_database

    admin = await create_admin(db, handle="adm1")
    boundary = "2026-08-20T02:00:00+00:00"
    await make_order(db, admin, price=300, series_pos=1,
                     created_at="2026-08-20T08:00:00+00:00", series_since=boundary)
    await make_order(db, admin, price=300, series_pos=3,
                     created_at="2026-08-20T08:10:00+00:00", series_since=boundary)
    assert any("не сходится" in p for p in check_database(db.path))


async def test_two_orders_of_one_series_still_cannot_share_a_step(db):
    """Проверка не ослабла: настоящая поломка по-прежнему видна."""
    from invariants import check_database

    admin = await create_admin(db, handle="adm1")
    boundary = "2026-08-20T02:00:00+00:00"
    await make_order(db, admin, price=300, series_pos=1,
                     created_at="2026-08-20T08:00:00+00:00", series_since=boundary)
    await make_order(db, admin, price=300, series_pos=1,
                     created_at="2026-08-20T10:00:00+00:00", series_since=boundary)
    assert any("одно место" in p for p in check_database(db.path))


async def test_legacy_orders_without_a_boundary_are_not_judged(db):
    """У заказов прежней схемы границы нет и взять её неоткуда — их
    нельзя ни сравнивать между собой, ни объявлять нарушением."""
    from invariants import check_database

    admin = await create_admin(db, handle="adm1")
    await make_order(db, admin, price=300, series_pos=1,
                     created_at="2026-08-20T08:00:00+00:00", series_since=None)
    await make_order(db, admin, price=300, series_pos=1,
                     created_at="2026-08-20T10:00:00+00:00", series_since=None)
    assert check_database(db.path) == []
