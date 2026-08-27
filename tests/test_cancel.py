"""Отмена заказа: окно 15 минут, блокировки, уведомления, идемпотентность."""

from datetime import timedelta

from conftest import VR_ADMIN_IDS
from helpers import (
    bind,
    create_admin,
    create_owner,
    drain,
    fake_cb,
    make_order,
    make_state,
    set_window,
    window_text,
)

from handlers.staff import cancel_confirm, cancel_list, cancel_pick
from handlers.vrheaven import order_cancel_confirm, order_cancel_pick
from utils import utcnow

ADMIN_CHAT = 1
OWNER_CHAT = 201
VR_CHAT = 999
WINDOW = 50


async def _club(db, *, contact="+7 900 000-00-00"):
    owner = await create_owner(db)
    admin = await create_admin(db, owner, contact=contact)
    await bind(db, admin, ADMIN_CHAT)
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, ADMIN_CHAT, WINDOW)
    await set_window(db, VR_CHAT, WINDOW)
    return owner, admin


async def _age_order(db, order_id: int, minutes: int) -> None:
    moment = (utcnow() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    async with db.write() as tx:
        await tx.execute("UPDATE orders SET created_at = ? WHERE id = ?",
                         (moment, order_id))


# ------------------------------------------------------ Самоотмена админом

async def test_self_cancel_within_window_notifies_owner_and_vrheaven(
        db, ui, config, bot, worker):
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    state = make_state(db, ADMIN_CHAT)
    await cancel_confirm(fake_cb(f"sc:ok:{order_id}", message_id=WINDOW),
                         state, db, ui, config)
    await drain(worker)

    assert (await db.get_order(order_id))["cancelled_at"] is not None
    owner_texts = bot.records(OWNER_CHAT)
    assert owner_texts and "Заказ отменил администратор adm1" in owner_texts[0]
    assert "+7 900 000-00-00" in owner_texts[0]
    for admin_id in VR_ADMIN_IDS:
        texts = bot.records(admin_id)
        assert texts and "отменён" in texts[0]


async def test_self_cancel_shows_dash_without_contact(db, ui, config, bot, worker):
    owner, admin = await _club(db, contact="")
    order_id = await make_order(db, admin, owner, price=300)
    await cancel_confirm(fake_cb(f"sc:ok:{order_id}", message_id=WINDOW),
                         make_state(db, ADMIN_CHAT), db, ui, config)
    await drain(worker)
    owner_texts = bot.records(OWNER_CHAT)
    assert "Контакт администратора: —" in owner_texts[0]


async def test_self_cancel_after_15_minutes_is_directed_to_support(db, ui, config):
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    await _age_order(db, order_id, 15)
    await cancel_pick(fake_cb(f"sc:o:{order_id}", message_id=WINDOW),
                      make_state(db, ADMIN_CHAT), db, ui, config)
    text = await window_text(db, ADMIN_CHAT)
    assert "Срок отмены истёк" in text and "@VrHeaven" in text
    assert (await db.get_order(order_id))["cancelled_at"] is None


async def test_cancel_window_boundary_is_exact(db, ui, config):
    owner, admin = await _club(db)
    fresh = await make_order(db, admin, owner, price=300)
    await _age_order(db, fresh, 14)
    expired = await make_order(db, admin, owner, price=300, series_pos=2)
    await _age_order(db, expired, 15)
    await cancel_list(fake_cb("sc:list", message_id=WINDOW),
                      make_state(db, ADMIN_CHAT), db, ui, config)
    from helpers import button_texts
    buttons = " ".join(await button_texts(db, ADMIN_CHAT))
    assert f"№{fresh}" in buttons and f"№{expired}" not in buttons


async def test_self_cancel_blocked_when_share_already_paid(db, ui, config):
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    await cancel_confirm(fake_cb(f"sc:ok:{order_id}", message_id=WINDOW),
                         make_state(db, ADMIN_CHAT), db, ui, config)
    assert (await db.get_order(order_id))["cancelled_at"] is None
    assert "уже включена в выплату" in await window_text(db, ADMIN_CHAT)


async def test_self_cancel_double_tap_has_single_effect(db, ui, config, bot, worker):
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    state = make_state(db, ADMIN_CHAT)
    await cancel_confirm(fake_cb(f"sc:ok:{order_id}", message_id=WINDOW),
                         state, db, ui, config)
    await cancel_confirm(fake_cb(f"sc:ok:{order_id}", message_id=WINDOW),
                         state, db, ui, config)
    await drain(worker)
    owner_texts = bot.records(OWNER_CHAT)
    assert len(owner_texts) == 1


async def test_admin_cannot_cancel_a_foreign_order(db, ui, config):
    owner, admin = await _club(db)
    other = await create_admin(db, owner, "adm2")
    order_id = await make_order(db, other, owner, price=300)
    cb = fake_cb(f"sc:ok:{order_id}", message_id=WINDOW)
    await cancel_confirm(cb, make_state(db, ADMIN_CHAT), db, ui, config)
    assert (await db.get_order(order_id))["cancelled_at"] is None
    assert cb.answer.await_args.args[0] == "Заказ не найден"


# --------------------------------------------------------- Отмена VR Heaven

async def test_vrheaven_cancel_notifies_everyone_but_the_actor(
        db, ui, config, bot, worker):
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    await _age_order(db, order_id, 120)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    await drain(worker)

    assert (await db.get_order(order_id))["cancelled_at"] is not None
    admin_texts = bot.records(ADMIN_CHAT)
    assert admin_texts and "Заказ отменил VR Heaven" in admin_texts[0]
    assert "Контакт администратора" not in admin_texts[0]
    assert bot.records(OWNER_CHAT)
    # инициатор видит результат на своём экране, второго сообщения не получает
    assert bot.records(VR_CHAT) == []
    other = next(iter(VR_ADMIN_IDS - {VR_CHAT}))
    assert bot.records(other)


async def test_vrheaven_cancel_of_a_free_session_promises_nothing_back(
        db, ui, config, bot, worker):
    """У бесплатного сеанса вознаграждения не было — «снято 0 ₽» было бы
    обещанием денег, которых никто не начислял."""
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, kind="free15", price=0,
                                headsets=None, minutes=None, admin_share=0,
                                series_pos=None)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    await drain(worker)

    assert (await db.get_order(order_id))["cancelled_at"] is not None
    assert await db.admin_unpaid_bonuses(admin["id"]) == []
    admin_texts = bot.records(ADMIN_CHAT)
    assert admin_texts and "снято" not in admin_texts[0]
    assert "15 минут бесплатно" in "".join(
        bot.records(next(iter(VR_ADMIN_IDS - {VR_CHAT}))))


async def test_vrheaven_cancel_is_idempotent(db, ui, config, bot, worker):
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    for _ in range(2):
        await order_cancel_confirm(
            fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
            db, ui, config)
    await drain(worker)
    assert len(bot.records(ADMIN_CHAT)) == 1


async def test_vrheaven_cancel_of_paid_order_never_claws_back(db, ui, config):
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        payout = await db.create_payout(tx, admin)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    order = await db.get_order(order_id)
    assert order["cancelled_at"] is not None
    assert order["admin_payout_id"] == payout[0]
    assert (await db.payouts_for_user(admin["id"]))[0]["amount"] == payout[1]


async def test_paid_reward_of_a_cancelled_order_becomes_a_debt(db, ui, config,
                                                                bot, worker):
    """Отменённый заказ уходит из расчётов, а выплаченное за него
    вознаграждение раньше исчезало вместе с ним. Теперь оно становится
    удержанием: деньги остаются в учёте и вычтутся из ближайшей выплаты."""
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)     # доля 50 ₽
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)

    debts = await db.admin_unpaid_bonuses(admin["id"])
    assert len(debts) == 1 and debts[0]["amount"] == -50.0
    assert f"№{order_id}" in debts[0]["comment"]
    total = await db.admin_unpaid_total(admin["id"])
    assert total["due_sum"] == -50.0, "долг переносится в следующий период"

    await drain(worker)
    assert any("удержанием" in t for t in bot.records(ADMIN_CHAT))
    assert "удержанием" in await window_text(db, VR_CHAT)


async def test_the_debt_is_deducted_from_the_next_payout(db, ui, config):
    """Долг возвращается не изъятием проведённой выплаты, а вычетом из
    следующей — обычным механизмом удержаний (SPEC §3а, §5)."""
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        first = await db.create_payout(tx, admin)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    await make_order(db, admin, owner, price=1000, series_pos=5)   # доля 250
    async with db.write() as tx:
        second = await db.create_payout(tx, admin)
    assert second[1] == 200.0                                      # 250 − 50
    assert (await db.payouts_for_user(admin["id"]))[-1]["amount"] == first[1]


async def test_an_unpaid_cancelled_order_creates_no_debt(db, ui, config):
    """Ничего не выплачено — возвращать нечего: заказ просто уходит."""
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    assert await db.admin_unpaid_bonuses(admin["id"]) == []
    assert "удержанием" not in await window_text(db, VR_CHAT)


async def test_double_tap_creates_one_debt(db, ui, config):
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    for _ in range(2):
        await order_cancel_confirm(
            fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
            db, ui, config)
    assert len(await db.admin_unpaid_bonuses(admin["id"])) == 1


async def test_a_payout_racing_the_cancellation_still_becomes_a_debt(db, ui, config):
    """Экран отмены прочитан до выплаты, нажат после. Решение о долге
    принимается по строке заказа внутри той же транзакции, а не по
    состоянию, которое видел экран."""
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    stale = await db.get_order(order_id)
    assert stale["admin_payout_id"] is None          # экран: доля не выплачена
    async with db.write() as tx:
        await db.create_payout(tx, admin)            # выплата в тот же миг
    async with db.write() as tx:
        cancelled = await db.cancel_order(tx, order_id, for_self=False)
    assert cancelled and cancelled.debt == 50.0
    assert (await db.admin_unpaid_bonuses(admin["id"]))[0]["amount"] == -50.0


async def test_a_stale_cancel_screen_still_records_the_debt(db, ui, config):
    """Экран подтверждения собран, когда заказ ещё не был выплачен, и
    нажат после выплаты. Долг считается по строке заказа внутри
    транзакции, а не по тому, что видел экран."""
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    await order_cancel_pick(
        fake_cb(f"ac:o:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    assert "уже включена в проведённую выплату" not in await window_text(db, VR_CHAT)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    debts = await db.admin_unpaid_bonuses(admin["id"])
    assert len(debts) == 1 and debts[0]["amount"] == -50.0


async def test_the_debt_can_be_cancelled_like_any_deduction(db, ui, config):
    """Долг возвращаемый: пока он не выплачен, VR Heaven снимает его
    обычной отменой бонуса."""
    from handlers.vrheaven import bonus_cancel_confirm

    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    debt = (await db.admin_unpaid_bonuses(admin["id"]))[0]
    await bonus_cancel_confirm(
        fake_cb(f"ad:bdelok:{debt['id']}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui)
    assert await db.admin_unpaid_bonuses(admin["id"]) == []


async def test_the_ledger_still_reconciles_after_the_debt(db, ui, config):
    """Проведённая выплата не переписывается, и инварианты сходятся:
    долг живёт отдельной строкой, а не правкой истории."""
    from invariants import check_database

    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
    await order_cancel_confirm(
        fake_cb(f"ac:ok:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    assert check_database(db.path) == []


async def test_vrheaven_cancel_screen_warns_about_paid_share(db, ui, config):
    """Экран отмены называет обе половины: вознаграждение вернётся
    удержанием, выплаченная доля владельца — нет (SPEC §5)."""
    owner, admin = await _club(db)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, admin)
        await db.create_payout(tx, owner)
    await order_cancel_pick(
        fake_cb(f"ac:o:{order_id}", chat_id=VR_CHAT, message_id=WINDOW),
        db, ui, config)
    text = await window_text(db, VR_CHAT)
    assert "уже входила в проведённую выплату" in text
    assert "Вознаграждение вернётся удержанием" in text
    assert "доля владельца не пересчитывается" in text


async def test_cancelled_order_frees_its_place_in_the_series(db, ui, config):
    """Отмена освобождает ступень для следующих заказов, но уже
    записанные вознаграждения не переписывает."""
    from handlers.staff import order_duration, order_payment
    owner, admin = await _club(db)
    async with db.write() as tx:
        await db.set_setting(tx, "discount_enabled", 0)
    state = make_state(db, ADMIN_CHAT)
    for _ in range(2):
        await order_duration(fake_cb("no:d:1:30", message_id=WINDOW), state, db,
                             ui, config)
        await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)
    async with db.write() as tx:
        await db.cancel_order(tx, 1, for_self=False)
    await order_duration(fake_cb("no:d:1:30", message_id=WINDOW), state, db, ui, config)
    await order_payment(fake_cb("no:ok", message_id=WINDOW), state, db, ui, config)

    orders = await db.export_orders()
    assert [o["series_pos"] for o in orders] == [1, 2, 2]
    assert [o["admin_share"] for o in orders] == [50.0, 100.0, 100.0]


async def test_self_cancel_reaches_the_other_devices_of_the_admin(db, ui, config,
                                                                  bot, worker):
    """Чек заказа ушёл на все устройства кабинета — отмена обязана дойти
    туда же, иначе на втором телефоне навсегда остаётся чек заказа,
    которого больше нет. Инициатор видит результат на своём Окне."""
    SECOND = 777
    owner, admin = await _club(db)
    await bind(db, admin, SECOND)
    await set_window(db, SECOND, WINDOW)
    order_id = await make_order(db, admin, owner, price=300)
    await cancel_confirm(fake_cb(f"sc:ok:{order_id}", chat_id=ADMIN_CHAT,
                                 message_id=WINDOW),
                         make_state(db, ADMIN_CHAT), db, ui, config)
    await drain(worker)
    second = bot.records(SECOND)
    assert second and "Заказ отменён" in second[0]
    assert "Заказ отменил администратор" in second[0]
    assert bot.records(ADMIN_CHAT) == [], "инициатор видит результат на Окне"
