"""Экспорт владельца: те же возможности, что у VR Heaven, но только его
данные и только его доля."""

import csv
import io

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

from handlers.staff import owner_export

OWNER_CHAT = 201
WINDOW = 50


def _parse(document) -> list[dict]:
    return list(csv.DictReader(io.StringIO(document.data.decode("utf-8-sig")),
                               delimiter=";"))


async def test_owner_export_is_scoped_to_own_data(db, ui, config, bot, worker):
    owner = await create_owner(db, "own1")
    foreign_owner = await create_owner(db, "own2")
    mine = await create_admin(db, owner, "mine")
    foreign = await create_admin(db, foreign_owner, "foreign")
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)

    my_order = await make_order(db, mine, owner, price=790, admin_share=50)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "3=4", 600)
    my_promo = await make_order(db, mine, owner, price=600, kind="promo",
                                headsets=None, minutes=None, promo_id=promo_id,
                                promo_name="3=4", series_pos=2)
    await make_order(db, foreign, foreign_owner, price=500)
    from db import Actor
    async with db.write() as tx:
        await db.create_payout(tx, owner)
        await db.create_payout(tx, mine)
        await db.cancel_order(tx, my_order, for_self=False)
        await db.audit(tx, Actor.superadmin(999), "order.cancel", "order",
                       my_order, after={"price": 790})

    await owner_export(fake_cb("op:export", chat_id=OWNER_CHAT, message_id=WINDOW),
                       make_state(db, OWNER_CHAT), db, ui, config)
    await drain(worker)

    docs = {d[3].filename: d[3] for d in bot.documents}
    assert set(docs) == {"own1-users.csv", "own1-orders.csv", "own1-payouts.csv",
                         "own1-audit.csv"}
    assert all(d.data.startswith(b"\xef\xbb\xbf") for d in docs.values())

    users = _parse(docs["own1-users.csv"])
    assert [u["логин"] for u in users] == ["mine"]
    assert "доля_владельца_%" not in users[0] and "сброс_серии" not in users[0]

    orders = _parse(docs["own1-orders.csv"])
    assert sorted(int(o["id"]) for o in orders) == [my_order, my_promo]
    by_id = {int(o["id"]): o for o in orders}
    assert by_id[my_promo]["заказ"] == "Акция · 3=4"
    assert float(by_id[my_order]["доля_владельца"]) == 237.0
    assert by_id[my_order]["отменён"]
    for hidden in ("вознаграждение_админа", "серия_№", "id_выплаты_админа",
                   "базовая_цена", "остаток_vr_heaven"):
        assert hidden not in by_id[my_order]

    payouts = _parse(docs["own1-payouts.csv"])
    assert len(payouts) == 1 and float(payouts[0]["сумма"]) == 417.0
    assert "получатель" not in payouts[0]

    audit = _parse(docs["own1-audit.csv"])
    assert any(row["действие"] == "заказ отменён" for row in audit)
    assert all("telegram" not in key for key in audit[0])

    assert "Файлы придут в этот чат" in await window_text(db, OWNER_CHAT)


async def test_owner_audit_never_leaks_other_clubs(db, ui, config, bot, worker):
    owner = await create_owner(db, "own1")
    other = await create_owner(db, "own2")
    theirs = await create_admin(db, other, "theirs")
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)
    their_order = await make_order(db, theirs, other, price=500)
    from db import Actor
    async with db.write() as tx:
        await db.audit(tx, Actor.superadmin(999), "order.cancel", "order",
                       their_order, after={"price": 500})

    await owner_export(fake_cb("op:export", chat_id=OWNER_CHAT, message_id=WINDOW),
                       make_state(db, OWNER_CHAT), db, ui, config)
    await drain(worker)
    audit = next(d[3] for d in bot.documents if d[3].filename == "own1-audit.csv")
    assert str(their_order) not in audit.data.decode("utf-8-sig")


async def test_admin_cannot_trigger_owner_export(db, ui, config, bot):
    admin = await create_admin(db)
    await bind(db, admin, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)
    await owner_export(fake_cb("op:export", chat_id=OWNER_CHAT, message_id=WINDOW),
                       make_state(db, OWNER_CHAT), db, ui, config)
    assert bot.documents == []
    assert "Кабинет администратора" in await window_text(db, OWNER_CHAT)
