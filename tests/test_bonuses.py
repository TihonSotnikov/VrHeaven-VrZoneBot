"""Бонусы и удержания администраторам: сумма, обязательный комментарий,
уведомление, отмена и попадание в выплату."""

from helpers import (
    bind,
    create_admin,
    create_owner,
    drain,
    fake_cb,
    fake_msg,
    make_order,
    make_state,
    set_window,
    window_text,
)

from handlers.vrheaven import (
    AddBonusSG,
    bonus_amount_step,
    bonus_ask_amount,
    bonus_cancel_confirm,
    bonus_comment_step,
    payout_confirm,
    payout_pick,
)

VR_CHAT = 999
ADMIN_CHAT = 1
WINDOW = 50


async def _admin(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await bind(db, admin, ADMIN_CHAT)
    await set_window(db, VR_CHAT, WINDOW)
    return owner, admin


async def _award(db, ui, admin, amount="500", comment="за ночную смену"):
    state = make_state(db, VR_CHAT)
    await bonus_ask_amount(fake_cb(f"ad:bonus:{admin['id']}", chat_id=VR_CHAT,
                                   message_id=WINDOW), state, db, ui)
    await bonus_amount_step(fake_msg(amount, chat_id=VR_CHAT), state, db, ui)
    await bonus_comment_step(fake_msg(comment, chat_id=VR_CHAT), state, db, ui)
    return state


async def test_bonus_needs_both_amount_and_reason(db, ui, bot, worker):
    """Требование продукта: у бонуса всегда есть сумма и внятная причина."""
    owner, admin = await _admin(db)
    state = await _award(db, ui, admin)
    bonuses = await db.admin_unpaid_bonuses(admin["id"])
    assert len(bonuses) == 1
    assert bonuses[0]["amount"] == 500 and bonuses[0]["comment"] == "за ночную смену"
    assert await state.get_state() is None

    await drain(worker)
    texts = bot.records(ADMIN_CHAT)
    assert texts and "Вам начислен бонус" in texts[0]
    assert "+500 ₽" in texts[0] and "за ночную смену" in texts[0]


async def test_empty_comment_is_refused(db, ui):
    owner, admin = await _admin(db)
    state = make_state(db, VR_CHAT)
    await bonus_ask_amount(fake_cb(f"ad:bonus:{admin['id']}", chat_id=VR_CHAT,
                                   message_id=WINDOW), state, db, ui)
    await bonus_amount_step(fake_msg("500", chat_id=VR_CHAT), state, db, ui)
    await bonus_comment_step(fake_msg("   ", chat_id=VR_CHAT), state, db, ui)
    assert await db.admin_unpaid_bonuses(admin["id"]) == []
    assert "Комментарий обязателен" in await window_text(db, VR_CHAT)
    assert await state.get_state() == AddBonusSG.comment.state


async def test_overlong_comment_is_refused(db, ui):
    owner, admin = await _admin(db)
    state = make_state(db, VR_CHAT)
    await bonus_ask_amount(fake_cb(f"ad:bonus:{admin['id']}", chat_id=VR_CHAT,
                                   message_id=WINDOW), state, db, ui)
    await bonus_amount_step(fake_msg("500", chat_id=VR_CHAT), state, db, ui)
    await bonus_comment_step(fake_msg("я" * 500, chat_id=VR_CHAT), state, db, ui)
    assert await db.admin_unpaid_bonuses(admin["id"]) == []
    assert "не длиннее" in await window_text(db, VR_CHAT)


async def test_amount_validation(db, ui):
    owner, admin = await _admin(db)
    state = make_state(db, VR_CHAT)
    await bonus_ask_amount(fake_cb(f"ad:bonus:{admin['id']}", chat_id=VR_CHAT,
                                   message_id=WINDOW), state, db, ui)
    for bad in ("ноль", "0", "999999999"):
        await bonus_amount_step(fake_msg(bad, chat_id=VR_CHAT), state, db, ui)
        assert await state.get_state() == AddBonusSG.amount.state


async def test_deduction_is_a_negative_bonus(db, ui, bot, worker):
    """Удержание остаётся в журнале, а не уходит «мимо кассы»."""
    owner, admin = await _admin(db)
    await _award(db, ui, admin, amount="-300", comment="недостача по кассе")
    bonuses = await db.admin_unpaid_bonuses(admin["id"])
    assert bonuses[0]["amount"] == -300
    await drain(worker)
    texts = bot.records(ADMIN_CHAT)
    assert "Удержание из выплаты" in texts[0] and "−300 ₽" in texts[0]


async def test_bonus_is_paid_together_with_orders(db, ui):
    owner, admin = await _admin(db)
    await make_order(db, admin, owner, price=300)
    await _award(db, ui, admin, amount="500")
    total = await db.admin_unpaid_total(admin["id"])
    assert total["share_sum"] == 50.0 and total["bonus_sum"] == 500.0
    assert total["due_sum"] == 550.0
    async with db.write() as tx:
        payout = await db.create_payout(tx, admin)
    assert payout[1] == 550.0 and payout[3] == 500.0
    assert await db.admin_unpaid_bonuses(admin["id"]) == []


async def test_unpaid_bonus_can_be_cancelled_paid_cannot(db, ui, bot, worker):
    owner, admin = await _admin(db)
    await _award(db, ui, admin)
    bonus = (await db.admin_unpaid_bonuses(admin["id"]))[0]
    await bonus_cancel_confirm(
        fake_cb(f"ad:bdelok:{bonus['id']}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui)
    assert await db.admin_unpaid_bonuses(admin["id"]) == []
    await drain(worker)
    assert any("Бонус отменён" in t for t in bot.records(ADMIN_CHAT))

    await _award(db, ui, admin, amount="200", comment="вторая смена")
    second = (await db.admin_unpaid_bonuses(admin["id"]))[0]
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    cb = fake_cb(f"ad:bdelok:{second['id']}", chat_id=VR_CHAT, message_id=WINDOW)
    await bonus_cancel_confirm(cb, db, ui)
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert (await db.get_bonus(second["id"]))["cancelled_at"] is None


async def test_bonus_only_admin_is_payable(db, ui):
    owner, admin = await _admin(db)
    await _award(db, ui, admin)
    total = await db.admin_unpaid_total(admin["id"])
    assert total["orders_count"] == 0 and total["due_sum"] == 500.0
    async with db.write() as tx:
        payout = await db.create_payout(tx, admin)
    assert payout[1] == 500.0 and payout[2] == 0


async def test_bonuses_reduce_the_vrheaven_remainder_in_the_summary(db, ui):
    import reports
    owner, admin = await _admin(db)
    await make_order(db, admin, owner, price=300)
    await _award(db, ui, admin, amount="100")
    report = await reports.vrheaven_summary(db)
    html = report.to_html(rich=False)
    assert "Остаток VR Heaven: 60 ₽" in html      # 160 − 100
    assert "в том числе бонусы: 100 ₽" in html


async def test_a_suspended_admin_keeps_his_deduction_in_the_summary(db, ui):
    """Приостановленный администратор с открытым удержанием обязан
    остаться в сводке. Прежнее условие `bonus_sum > 0` выбрасывало такую
    строку из таблицы, а вместе с ней и из остатка VR Heaven внизу —
    остаток считается по показанным строкам, и 500 ₽ долга исчезали
    из обеих цифр разом."""
    import reports
    owner, admin = await _admin(db)
    # ни одного открытого заказа: строку держит только само удержание
    await _award(db, ui, admin, amount="-500", comment="недостача")
    async with db.write() as tx:
        await db.set_user_active(tx, admin["id"], False)

    rows = await db.admins_unpaid_summary()
    shown = [r for r in rows if not r["is_active"]]
    assert shown and shown[0]["orders_count"] == 0
    assert shown[0]["bonus_sum"] == -500.0

    html = (await reports.vrheaven_summary(db)).to_html(rich=False)
    assert admin["handle"] in html, "строка обязана остаться на виду"
    # удержание увеличивает остаток ровно так же, как бонус его уменьшает
    assert "Остаток VR Heaven: 500 ₽" in html          # 0 − (−500)


async def test_a_suspended_admin_without_any_rows_stays_hidden(db, ui):
    """Проверка не ослабла: приостановленному, за которым ничего не
    числится, в сводке по-прежнему делать нечего."""
    import reports
    owner, admin = await _admin(db)
    async with db.write() as tx:
        await db.set_user_active(tx, admin["id"], False)
    html = (await reports.vrheaven_summary(db)).to_html(rich=False)
    assert admin["handle"] not in html


async def test_a_cancelled_paid_order_keeps_its_debt_in_the_remainder(db, ui,
                                                                     config):
    """Долг за отменённый выплаченный заказ — обычное удержание, и в
    сводке он ведёт себя как удержание: остаётся видимым и учитывается
    в остатке VR Heaven даже у приостановленного администратора."""
    import reports
    from handlers.vrheaven import order_cancel_confirm
    owner, admin = await _admin(db)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    async with db.write() as tx:
        await db.set_user_active(tx, admin["id"], False)
    html = (await reports.vrheaven_summary(db)).to_html(rich=False)
    assert admin["handle"] in html and "−50 ₽" in html


async def test_bonus_shows_in_admin_statistics(db, ui):
    import reports
    owner, admin = await _admin(db)
    await _award(db, ui, admin, comment="за инициативу")
    html = (await reports.admin_period(db, admin, __import__("zoneinfo").ZoneInfo(
        "Europe/Moscow"))).to_html(rich=False)
    assert "за инициативу" in html and "К выплате: 500 ₽" in html


async def test_bonus_export(db, ui):
    from zoneinfo import ZoneInfo

    import export as xp
    owner, admin = await _admin(db)
    await _award(db, ui, admin, comment="за смену")
    data = xp.bonuses_csv(await db.export_bonuses(), ZoneInfo("Europe/Moscow"))
    text = data.decode("utf-8-sig")
    assert "за смену" in text and "к выплате" in text


# ------------------- Удержание больше начисленного: выплаты не бывает

async def test_deduction_over_the_period_blocks_the_payout(db, ui):
    """Итог меньше нуля выплатой не бывает (SPEC §4). Раньше выплата
    записывалась с отрицательной суммой и закрывала период — деньги,
    которых администратор не получал, объявлялись выплаченными."""
    owner, admin = await _admin(db)
    await make_order(db, admin, owner, price=300)          # доля 50 ₽
    await _award(db, ui, admin, amount="-500", comment="недостача")
    total = await db.admin_unpaid_total(admin["id"])
    assert total["due_sum"] == -450.0 and not db.is_payable(total)
    async with db.write() as tx:
        assert await db.create_payout(tx, admin) is None


async def test_a_refused_payout_leaves_every_row_open(db, ui):
    """Удержание переносится в следующий период целиком: ни одна строка
    не помечена, пустой выплаты в базе нет."""
    owner, admin = await _admin(db)
    order_id = await make_order(db, admin, owner, price=300)
    await _award(db, ui, admin, amount="-500", comment="недостача")
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    assert await db.payouts_for_user(admin["id"]) == []
    assert (await db.get_order(order_id))["admin_payout_id"] is None
    assert len(await db.admin_unpaid_bonuses(admin["id"])) == 1

    # начислили столько, что итог снова неотрицателен — период закрывается
    await _award(db, ui, admin, amount="500", comment="премия")
    async with db.write() as tx:
        payout = await db.create_payout(tx, admin)
    assert payout[1] == 50.0, "перенесённое удержание учтено в новом периоде"
    assert (await db.get_order(order_id))["admin_payout_id"] is not None


async def test_stale_confirmation_screen_cannot_write_a_negative_payout(db, ui):
    """Экран подтверждения собран до удержания, нажат после. Защита живёт
    в самой записи выплаты, а не только на экране."""
    owner, admin = await _admin(db)
    await make_order(db, admin, owner, price=300)
    cb = fake_cb(f"po:p:{admin['id']}", chat_id=VR_CHAT, message_id=WINDOW)
    await payout_pick(cb, db, ui)                    # экран: к выплате 50 ₽
    await _award(db, ui, admin, amount="-500", comment="недостача")

    confirm = fake_cb(f"po:ok:{admin['id']}", chat_id=VR_CHAT, message_id=WINDOW)
    await payout_confirm(confirm, db, ui)
    assert await db.payouts_for_user(admin["id"]) == []
    text = await window_text(db, VR_CHAT)
    assert "Итог периода отрицательный" in text and "−450 ₽" in text


async def test_offsetting_deduction_still_closes_the_period(db, ui):
    """Ноль из начисленного и ровно такого же удержания — выплата
    настоящая: строки обязаны закрыться, иначе тянутся вечно (SPEC §4)."""
    owner, admin = await _admin(db)
    await make_order(db, admin, owner, price=300)
    await _award(db, ui, admin, amount="-50", comment="ровно в ноль")
    total = await db.admin_unpaid_total(admin["id"])
    assert total["due_sum"] == 0.0 and db.is_payable(total)
    async with db.write() as tx:
        payout = await db.create_payout(tx, admin)
    assert payout[1] == 0.0 and payout[2] == 1
    assert await db.admin_unpaid_bonuses(admin["id"]) == []
