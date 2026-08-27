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


async def _export_files(db, ui, config, bot, worker) -> dict:
    await owner_export(fake_cb("op:export", chat_id=OWNER_CHAT, message_id=WINDOW),
                       make_state(db, OWNER_CHAT), db, ui, config)
    await drain(worker)
    return {d[3].filename: d[3] for d in bot.documents}


async def test_owner_export_is_scoped_to_own_data(db, ui, config, bot, worker):
    owner = await create_owner(db, "own1")
    foreign_owner = await create_owner(db, "own2")
    mine = await create_admin(db, owner, "mine")
    foreign = await create_admin(db, foreign_owner, "foreign")
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)

    my_order = await make_order(db, mine, owner, price=790, admin_share=50)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "3=4")
    # исторический заказ прежнего типа «акция» со своей записанной ценой:
    # выгрузка обязана показывать его ровно так, как он записан
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


async def test_owner_export_hides_admin_compensation_and_ladder(
        db, ui, config, bot, worker):
    """Журнал заказа несёт вознаграждение администратора и его номер
    в лесенке серии; ни в один файл владельца они не попадают.

    Запись журнала здесь ровно та, что пишет handlers.staff при
    оформлении заказа, — иначе проверка сторожила бы выдуманный случай.
    """
    owner = await create_owner(db, "own1")
    admin = await create_admin(db, owner, "mine")
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)
    order = await make_order(db, admin, owner, price=790, admin_share=137.11,
                             series_pos=7)
    from db import Actor
    async with db.write() as tx:
        await db.audit(tx, Actor.staff(admin, 4242), "order.create", "order", order,
                       after={"price": 790.0, "admin_share": 137.11,
                              "owner_share": 237.0, "series_pos": 7})

    docs = await _export_files(db, ui, config, bot, worker)
    everything = "\n".join(d.data.decode("utf-8-sig") for d in docs.values())
    for forbidden in ("admin_share", "series_pos", "137.11",
                      "вознаграждение_админа", "серия_№", "остаток_vr_heaven",
                      "id_выплаты_админа"):
        assert forbidden not in everything, forbidden

    # Своё владелец по-прежнему видит — и в заказах, и в журнале
    audit = _parse(docs["own1-audit.csv"])
    created = next(r for r in audit if r["действие"] == "заказ оформлен")
    assert created["изменения"] == "стало: price=790.0, owner_share=237.0"


async def test_owner_audit_shows_only_the_fields_on_the_list(
        db, ui, config, bot, worker):
    """Список полей закрытый: неизвестное действие приходит строкой без
    подробностей, а Telegram-идентификаторы и время сбросов серии —
    внутренняя кухня — не показываются и в известных."""
    owner = await create_owner(db, "own1")
    admin = await create_admin(db, owner, "mine")
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)
    from db import Actor
    async with db.write() as tx:
        await db.audit(tx, Actor.staff(admin, 4242), "user.login", "user",
                       admin["id"], after={"chat_id": 4242, "new_device": True})
        await db.audit(tx, Actor.superadmin(999), "user.series_reset", "user",
                       admin["id"], before={"series_reset_min": None},
                       after={"series_reset_min": 360})
        # Действие из будущего, которого в списке полей ещё нет
        await db.audit(tx, Actor.superadmin(999), "user.limit", "user",
                       admin["id"], after={"admin_debt": 999.99})

    docs = await _export_files(db, ui, config, bot, worker)
    audit = _parse(docs["own1-audit.csv"])
    by_action = {r["действие"]: r for r in audit}

    assert by_action["вход в кабинет"]["изменения"] == "стало: new_device=True"
    assert by_action["изменено время сбросов серии"]["изменения"] == ""
    assert by_action["user.limit"]["изменения"] == ""
    everything = "\n".join(d.data.decode("utf-8-sig") for d in docs.values())
    assert "4242" not in everything and "999.99" not in everything


async def test_owner_export_files_are_utf8_with_a_signature(
        db, ui, config, bot, worker):
    """Bot API отдаёт документ без указания кодировки, и просмотрщик
    определяет её сам: без подписи кириллица становится кракозябрами.
    Разделитель «;» и подпись держат файл читаемым и в Excel, и в телефоне.
    """
    owner = await create_owner(db, "own1")
    admin = await create_admin(db, owner, "mine")
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "Акция «Три по цене двух»")
    await make_order(db, admin, owner, price=600, kind="promo", headsets=None,
                     minutes=None, promo_id=promo_id,
                     promo_name="Акция «Три по цене двух»", series_pos=2)

    docs = await _export_files(db, ui, config, bot, worker)
    for name, document in docs.items():
        data = document.data
        assert data.startswith(b"\xef\xbb\xbf"), name
        # Тело — настоящий UTF-8, а не подпись поверх однобайтовой кодировки
        data[3:].decode("utf-8")
        # Строки заканчиваются CRLF: Excel не склеивает их в одну
        assert data.count(b"\n") == data.count(b"\r\n"), name

    orders = _parse(docs["own1-orders.csv"])
    assert orders[0]["акция"] == "Акция «Три по цене двух»"
    assert orders[0]["заказ"] == "Акция · Акция «Три по цене двух»"
    users = _parse(docs["own1-users.csv"])
    assert users[0]["имя"] == "Админ mine"


async def test_exported_values_do_not_become_spreadsheet_formulas(
        db, ui, config, bot, worker):
    """Телефон «+7 …» Excel считает формулой и молча превращает в число,
    а значение с «=» — выполняет. Пометка текста спасает и то, и другое;
    обычных значений она не касается."""
    owner = await create_owner(db, "own1")
    admin = await create_admin(db, owner, "mine", contact="+7 999 123-45-67")
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)
    async with db.write() as tx:
        promo_id = await db.create_promo(tx, "=1+1")
    await make_order(db, admin, owner, price=600, kind="promo", headsets=None,
                     minutes=None, promo_id=promo_id, promo_name="=1+1",
                     series_pos=2)

    docs = await _export_files(db, ui, config, bot, worker)
    users = _parse(docs["own1-users.csv"])
    assert users[0]["контакт"] == "'+7 999 123-45-67"
    assert users[0]["имя"] == "Админ mine"          # обычное значение как было
    assert users[0]["статус"] == "действующий"
    orders = _parse(docs["own1-orders.csv"])
    assert orders[0]["акция"] == "'=1+1"
    assert orders[0]["цена"] == "600.0"             # число не помечается


async def test_owner_export_hides_the_debt_left_by_a_cancelled_order(
        db, ui, config, bot, worker):
    """Отмена оплаченного заказа записывает выплаченное вознаграждение
    удержанием и называет его в журнале. Владельцу видна сама отмена и
    цена заказа; сумма вознаграждения и номер удержания — нет, это
    расчёт VR Heaven с администратором.

    Запись журнала здесь ровно та, что пишет handlers.vrheaven при
    отмене заказа супер-админом.
    """
    owner = await create_owner(db, "own1")
    admin = await create_admin(db, owner, "mine")
    await bind(db, owner, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)
    order = await make_order(db, admin, owner, price=790, admin_share=137.11)
    from db import Actor
    async with db.write() as tx:
        await db.audit(tx, Actor.superadmin(999), "order.cancel", "order", order,
                       before={"cancelled": False},
                       after={"cancelled": True, "by": "vrheaven", "price": 790.0,
                              "debt": 137.11, "bonus": 42})

    docs = await _export_files(db, ui, config, bot, worker)
    everything = "\n".join(d.data.decode("utf-8-sig") for d in docs.values())
    for forbidden in ("debt", "bonus", "137.11"):
        assert forbidden not in everything, forbidden

    audit = _parse(docs["own1-audit.csv"])
    cancelled = next(r for r in audit if r["действие"] == "заказ отменён")
    assert cancelled["изменения"] == (
        "было: cancelled=False · стало: cancelled=True, by=vrheaven, price=790.0")


async def test_admin_cannot_trigger_owner_export(db, ui, config, bot):
    admin = await create_admin(db)
    await bind(db, admin, OWNER_CHAT)
    await set_window(db, OWNER_CHAT, WINDOW)
    await owner_export(fake_cb("op:export", chat_id=OWNER_CHAT, message_id=WINDOW),
                       make_state(db, OWNER_CHAT), db, ui, config)
    assert bot.documents == []
    assert "Кабинет администратора" in await window_text(db, OWNER_CHAT)
