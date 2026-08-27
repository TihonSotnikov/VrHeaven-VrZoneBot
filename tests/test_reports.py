"""Что экраны периодов показывают — и чего не показывают.

Администратору и владельцу видны их собственные деньги; оборот клуба и
цены заказов к их выплате отношения не имеют и на экран не выносятся
(SPEC §6: владельцу — только его доля). Вознаграждение администратора
считается лесенкой серии и от цены не зависит вовсе.
"""

from pathlib import Path
from zoneinfo import ZoneInfo

from helpers import create_admin, create_owner, make_order

import reports
from utils import fmt_money

TZ = ZoneInfo("Europe/Moscow")


async def _html(report) -> str:
    return report.to_html(rich=False)


# ------------------------------------------------------- Моя статистика

async def test_admin_statistics_lead_with_the_payable(db):
    """Порядок: вознаграждение → бонусы → итог. Итог наверху ещё и
    переживает обрезку длинного отчёта (markup.Report.to_html)."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], 500.0, "за инициативу")
        await db.create_bonus(tx, admin["id"], -300.0, "недостача")
    html = await _html(await reports.admin_period(db, admin, TZ))

    assert "Вознаграждение за заказы: 50 ₽" in html
    assert "Бонусы и удержания: +200 ₽" in html
    assert "<b>К выплате: 250 ₽</b>" in html
    assert html.index("К выплате") < html.index("<pre>"), "итог выше таблицы"


async def test_admin_statistics_hide_turnover_and_order_prices(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    html = await _html(await reports.admin_period(db, admin, TZ))

    assert "Оборот" not in html and "оборот" not in html
    assert "700" not in html, "цена заказа администратору не нужна"


async def test_admin_statistics_do_not_contradict_themselves_when_empty(db):
    """«Заказов нет» рядом со счётом заказов серии читалось как
    противоречие: пустой период — про начисления, серия — про смену."""
    admin = await create_admin(db)
    html = await _html(await reports.admin_period(db, admin, TZ))
    assert "В текущем периоде начислений пока нет" in html
    assert "Заказов в текущем периоде пока нет" not in html


async def test_admin_statistics_keep_the_next_step_of_the_series(db):
    admin = await create_admin(db)
    await make_order(db, admin, price=700)
    html = await _html(await reports.admin_period(db, admin, TZ))
    assert "следующий — 100 ₽" in html


async def test_payout_day_report_drops_the_series_line(db):
    """Отчёт дня выплат — про итог периода, а не про текущую смену."""
    admin = await create_admin(db)
    await make_order(db, admin, price=700)
    html = await _html(await reports.admin_period(
        db, admin, TZ, title="К выплате на 01.09.2026", with_series=False,
        note="Выплата проводится вручную"))
    assert "Серия" not in html
    assert reports.PERIOD_NOTE not in html, "приписка ровно одна"
    assert "Выплата проводится вручную" in html


async def test_admin_statistics_explain_a_negative_balance(db):
    """Итог меньше нуля не выплачивается, а переносится (SPEC §4) —
    и на экране это должно быть сказано, а не оставлено голым минусом."""
    admin = await create_admin(db)
    await make_order(db, admin, price=700)
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], -300.0, "недостача")
    html = await _html(await reports.admin_period(db, admin, TZ))
    assert "<b>К выплате: −250 ₽</b>" in html
    assert "удержание перейдёт в следующий период" in html


async def test_admin_statistics_stay_silent_on_a_positive_balance(db):
    admin = await create_admin(db)
    await make_order(db, admin, price=700)
    html = await _html(await reports.admin_period(db, admin, TZ))
    assert "Итог отрицательный" not in html


# ------------------------------------------------------- Текущий период

async def test_owner_period_shows_only_the_owner_share(db):
    owner = await create_owner(db)                       # 30% по умолчанию
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    html = await _html(await reports.owner_period(db, owner, TZ))

    assert "<b>К выплате: 210 ₽</b>" in html
    assert "Ваша доля" in html
    assert "Оборот" not in html and "оборот" not in html
    assert "700" not in html, "цена заказа — не доля владельца"


# --------------------------------------------------- Мои администраторы

async def test_owner_admins_name_the_share_as_the_owners_own(db):
    """«Доля» рядом со столбцом «Логин» читается двояко — чья именно.
    Столбец назван «Ваша доля», и внизу сказано, с чего она считается."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    html = await _html(await reports.owner_admins(db, owner))

    assert "Ваша доля" in html
    assert "Ваша доля с заказов каждого администратора за текущий период" in html
    assert "210" in html
    assert "Оборот" not in html and "оборот" not in html
    assert "700" not in html


# ------------------------------------------------------------------ Сводка

async def test_summary_shows_one_club_turnover(db):
    """Оборот в сводке один и клубный: сумма цен заказов периода.

    Прежде «Оборот» стоял столбцом в обеих таблицах. У владельца и у
    администратора открыты разные заказы (стороны закрываются
    независимо), поэтому два «Итого · Оборот» на одном экране давали два
    разных числа под одним словом, и ни одно из них не было оборотом
    клуба: заказы прямых администраторов в таблицу владельцев не попадают
    вовсе.
    """
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    direct = await create_admin(db, None, handle="direct")
    await make_order(db, admin, owner, price=700)
    await make_order(db, direct, None, price=300)
    html = await _html(await reports.vrheaven_summary(db))

    assert html.count("борот") == 1, "оборот на экране ровно один"
    assert f"Заказов за период: 2 · оборот {fmt_money(1000)}" in html


async def test_summary_turnover_counts_an_order_once_while_a_side_is_open(db):
    """Заказ остаётся в обороте периода, пока открыта хотя бы одна доля,
    и входит в него ровно один раз — как и в остаток VR Heaven."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)

    async with db.write() as tx:
        await db.create_payout(tx, admin)          # закрыта сторона админа
    html = await _html(await reports.vrheaven_summary(db))
    assert "Заказов за период: 1 · оборот 700 ₽" in html

    async with db.write() as tx:
        await db.create_payout(tx, owner)          # закрыты обе стороны
    html = await _html(await reports.vrheaven_summary(db))
    assert "Заказов за период: 0 · оборот 0 ₽" in html


async def test_summary_keeps_the_totals_in_one_place(db):
    """Строки «Итого» повторяли суммы подвала числом в число. Итог живёт
    внизу: он один и переживает обрезку длинного отчёта."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    html = await _html(await reports.vrheaven_summary(db))

    assert "Итого" not in html
    assert html.count("К выплате владельцам") == 1
    assert html.count("К выплате администраторам") == 1


async def test_summary_turnover_includes_orders_of_a_deleted_recipient(db):
    """Доля удалённого получателя достаётся VR Heaven и остаётся видимой
    (SPEC §4); его заказ — часть оборота клуба, хотя строки в таблице
    у него больше нет."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    async with db.write() as tx:
        await db.delete_user(tx, admin["id"])
    html = await _html(await reports.vrheaven_summary(db))

    assert "Заказов за период: 1 · оборот 700 ₽" in html


def test_the_period_total_is_called_the_same_everywhere():
    """«Накоплено к выплате» и «К выплате» — одна величина, и слово о ней
    одно: иначе одна и та же сумма выглядит двумя разными понятиями."""
    root = Path(__file__).resolve().parent.parent
    for name in ("reports.py", "scheduler.py", "keyboards.py",
                 "handlers/staff.py", "handlers/vrheaven.py"):
        assert "Накоплено" not in (root / name).read_text(encoding="utf-8"), name
