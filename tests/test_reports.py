"""Что экраны периодов показывают — и чего не показывают.

Администратору и владельцу видны их собственные деньги; оборот клуба и
цены заказов к их выплате отношения не имеют и на экран не выносятся
(SPEC §6: владельцу — только его доля). Вознаграждение администратора
считается лесенкой серии и от цены не зависит вовсе.

Цена заказа и остаток VR Heaven по нему стоят рядом ровно в одном месте
всего бота — в сводке VR Heaven, за фильтром супер-админа.
"""

import re
from pathlib import Path
from zoneinfo import ZoneInfo

from helpers import create_admin, create_owner, make_order, validate_html

import reports
from markup import TEXT_LIMIT
from utils import fmt_money, fmt_num

TZ = ZoneInfo("Europe/Moscow")


async def _html(report) -> str:
    return report.to_html(rich=False)


# Всё, что Telegram показывает с новой строки: разрыв, конец абзаца,
# конец строки таблицы и конец таблицы в любом из двух её видов
_BREAK_RE = re.compile(r"<br>|</p>|</tr>|</table>|</pre>")


def _visible_lines(html: str) -> list[str]:
    """Текст так, как Telegram разложит его на строки, — для обоих
    способов показа сразу: <br> и <p> нативного сообщения значат ровно
    то же, что «\n» моноширинного."""
    text = _BREAK_RE.sub("\n", html)
    text = re.sub(r"<[^>]+>", "", text)
    return [line.strip() for line in text.split("\n") if line.strip()]


def _table_rows(html: str) -> list[list[str]]:
    """Ячейки нативных таблиц отчёта (rich=True)."""
    return [re.findall(r"<t[dh]>(.*?)</t[dh]>", tr)
            for tr in re.findall(r"<tr>(.*?)</tr>", html)]


async def _scene(db):
    """Реалистичный период: два клуба с разными долями владельца, прямой
    администратор без клуба и разные ступени лесенки.

      №1  adma / clubA 30%   700 ₽ →  50 / 210 / 440
      №2  adma / clubA 30%   500 ₽ → 100 / 150 / 250
      №3  admb / clubB 10%   800 ₽ →  50 /  80 / 670
      №4  direct — без клуба 300 ₽ →  50 /   0 / 250

    Оборот 2 300 ₽ = 440 владельцам + 250 администраторам + 1 610 остатка.
    """
    club_a = await create_owner(db, "cluba", percent=30)
    club_b = await create_owner(db, "clubb", percent=10)
    adm_a = await create_admin(db, club_a, handle="adma")
    adm_b = await create_admin(db, club_b, handle="admb")
    direct = await create_admin(db, None, handle="direct")
    await make_order(db, adm_a, club_a, price=700, series_pos=1)
    await make_order(db, adm_a, club_a, price=500, series_pos=2)
    await make_order(db, adm_b, club_b, price=800, series_pos=1)
    await make_order(db, direct, None, price=300, series_pos=1)
    return club_a, club_b, adm_a, adm_b, direct


# ------------------------------------------------------- Моя статистика

async def test_admin_statistics_close_with_the_payable(db):
    """Сверху — названные таблицы, снизу — три величины: вознаграждение →
    бонусы → итог. Слагаемые стоят рядом с итогом, а не через полэкрана."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], 500.0, "за инициативу")
        await db.create_bonus(tx, admin["id"], -300.0, "недостача")
    html = await _html(await reports.admin_period(db, admin, TZ))

    assert "Вознаграждение за заказы: 50 ₽" in html
    assert "Бонусы и удержания: +200 ₽" in html
    assert "К выплате: 250 ₽" in html
    assert html.rindex("</pre>") < html.index("К выплате"), "итог ниже таблиц"
    assert html.index("<b>Заказы</b>") < html.index("<pre>"), "таблица названа"
    assert (html.index("<b>Бонусы и удержания</b>")
            < html.rindex("<pre>")), "вторая таблица названа"


async def test_admin_statistics_keep_the_totals_unbolded(db):
    """Выделение осталось у подписей таблиц: величины внизу его не носят,
    иначе одни и те же слова читаются то заголовком, то суммой."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], 500.0, "за инициативу")
    html = await _html(await reports.admin_period(db, admin, TZ))

    bold = re.findall(r"<b>(.*?)</b>", html)
    for label in ("Вознаграждение за заказы", "Бонусы и удержания: ",
                  "К выплате"):
        assert not [b for b in bold if label in b], f"{label} выделено"
    assert "Бонусы и удержания" in bold, "подпись таблицы осталась выделенной"


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
    assert "Следующий заказ: 100 ₽" in html


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
    assert "К выплате: −250 ₽" in html
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

    assert "К выплате: 210 ₽" in html
    assert "Ваша доля" in html
    assert "Оборот" not in html and "оборот" not in html
    assert "700" not in html, "цена заказа — не доля владельца"


async def test_owner_period_closes_with_the_payable(db):
    """Тот же порядок, что в статистике администратора: названная
    таблица — расшифровка, итог — под ней и без выделения."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    html = await _html(await reports.owner_period(db, owner, TZ))

    assert html.index("<b>Заказы</b>") < html.index("<pre>"), "таблица названа"
    assert html.rindex("</pre>") < html.index("К выплате"), "итог ниже таблицы"
    assert "<b>К выплате" not in html


async def test_busy_admin_statistics_still_keeps_its_total(db):
    """Итог внизу не должен стоить администратору обрезки: режутся строки
    таблиц, всё после последней таблицы остаётся целым."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    for _ in range(reports.MAX_TABLE_ROWS + 5):
        await make_order(db, admin, owner, price=700, series_pos=5)
    async with db.write() as tx:
        for i in range(reports.MAX_TABLE_ROWS):
            await db.create_bonus(tx, admin["id"], 100.0, f"смена {i} " + "х" * 180)
    report = await reports.admin_period(db, admin, TZ)

    for rich in (True, False):
        html = report.to_html(rich=rich)
        assert len(html) <= TEXT_LIMIT
        assert len(html) > TEXT_LIMIT - 400, "отчёт и правда не помещался целиком"
        assert f"К выплате: {fmt_money(30 * 250 + 25 * 100)}" in html
        validate_html(html)


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
    assert "Заказов за период: 2" in _visible_lines(html)
    assert f"Оборот: {fmt_money(1000)}" in _visible_lines(html)


async def test_summary_turnover_counts_an_order_once_while_a_side_is_open(db):
    """Заказ остаётся в обороте периода, пока открыта хотя бы одна доля,
    и входит в него ровно один раз — как и в остаток VR Heaven."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)

    async with db.write() as tx:
        await db.create_payout(tx, admin)          # закрыта сторона админа
    shown = _visible_lines(await _html(await reports.vrheaven_summary(db)))
    assert "Заказов за период: 1" in shown and f"Оборот: {fmt_money(700)}" in shown

    async with db.write() as tx:
        await db.create_payout(tx, owner)          # закрыты обе стороны
    shown = _visible_lines(await _html(await reports.vrheaven_summary(db)))
    assert "Заказов за период: 0" in shown and f"Оборот: {fmt_money(0)}" in shown


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
    получателей у него больше нет."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    async with db.write() as tx:
        await db.delete_user(tx, admin["id"])
    shown = _visible_lines(await _html(await reports.vrheaven_summary(db)))

    assert "Заказов за период: 1" in shown and f"Оборот: {fmt_money(700)}" in shown


# --------------------------------------------- Заказы сводки: цена и остаток

async def test_summary_shows_the_price_and_the_remainder_of_every_order(db):
    """Две цифры по каждому заказу: сколько взяли с клиента и сколько с
    этого заказа осталось VR Heaven после администратора и владельца."""
    await _scene(db)
    rows = _table_rows((await reports.vrheaven_summary(db)).to_html(rich=True))

    assert ["Заказ", "Админ", "Цена, ₽", "Остаток VR Heaven, ₽"] in rows
    assert ["№1", "adma", fmt_num(700), fmt_num(440)] in rows
    assert ["№2", "adma", fmt_num(500), fmt_num(250)] in rows
    assert ["№3", "admb", fmt_num(800), fmt_num(670)] in rows
    assert ["№4", "direct", fmt_num(300), fmt_num(250)] in rows


async def test_summary_order_rows_reconcile_with_the_three_way_split(db):
    """Остаток по заказу — не новая формула, а та же самая: цена минус
    вознаграждение администратора минус доля владельца (SPEC §3).

    Проверяется на разных ступенях лесенки, разных долях владельца и на
    заказе прямого администратора, у которого доли владельца нет вовсе.
    """
    await _scene(db)
    orders = {o["id"]: o for o in await db.fetchall("SELECT * FROM orders")}
    for row in await db.vrheaven_unpaid_orders():
        order = orders[row["id"]]
        assert row["price"] == order["price"]
        assert round(row["vr_share"], 2) == round(
            order["price"] - order["admin_share"] - order["owner_share"], 2)


async def test_summary_order_column_adds_up_to_the_remainder_below(db):
    """Сумма столбца «Остаток VR Heaven» равна остатку в подвале: иначе
    столбец и итог под ним спорят друг с другом на одном экране."""
    await _scene(db)
    rows = await db.vrheaven_unpaid_orders()
    total = await db.vrheaven_unpaid_total()

    assert round(sum(r["vr_share"] for r in rows), 2) == round(total["share_sum"], 2)
    shown = _visible_lines(await _html(await reports.vrheaven_summary(db)))
    assert f"Остаток VR Heaven: {fmt_money(1610)}" in shown
    assert f"К выплате владельцам: {fmt_money(440)}" in shown
    assert f"К выплате администраторам: {fmt_money(250)}" in shown
    # 440 + 250 + 1 610 = 2 300 — оборот периода сходится до копейки
    assert f"Оборот: {fmt_money(2300)}" in shown


async def test_summary_names_the_remainder_before_bonuses_when_they_exist(db):
    """Бонус не принадлежит ни одному заказу и вычитается из остатка
    целиком, поэтому сумма столбца и итог расходятся ровно на бонусы.
    Промежуточная строка называет обе величины, и спорить им не о чем."""
    _, _, adm_a, _, _ = await _scene(db)
    async with db.write() as tx:
        await db.create_bonus(tx, adm_a["id"], 200.0, "за инициативу")
    shown = _visible_lines(await _html(await reports.vrheaven_summary(db)))

    assert f"в том числе бонусы: {fmt_money(200)}" in shown
    assert f"Остаток по заказам: {fmt_money(1610)}" in shown
    assert f"Остаток VR Heaven: {fmt_money(1410)}" in shown


async def test_summary_omits_the_intermediate_line_without_bonuses(db):
    """Без бонусов «Остаток по заказам» и «Остаток VR Heaven» — одно
    число, написанное дважды."""
    await _scene(db)
    html = await _html(await reports.vrheaven_summary(db))
    assert "Остаток по заказам" not in html


async def test_summary_order_row_keeps_the_share_of_a_deleted_recipient(db):
    """Доля, которая не будет выплачена никогда, достаётся VR Heaven —
    и по строке заказа это видно, а не только в итоге."""
    club_a, _, adm_a, _, _ = await _scene(db)
    async with db.write() as tx:
        await db.delete_user(tx, adm_a["id"])
    rows = {r["id"]: r for r in await db.vrheaven_unpaid_orders()}

    # №1: 700 − 0 (админа больше нет) − 210 владельцу = 490
    assert round(rows[1]["vr_share"], 2) == 490
    assert round(rows[2]["vr_share"], 2) == 350          # 500 − 0 − 150
    assert round(rows[3]["vr_share"], 2) == 670, "чужой заказ не задет"


async def test_summary_order_row_is_unchanged_by_a_paid_side(db):
    """Выплаченная доля из остатка ушла и раньше: заказ остаётся в
    таблице, пока открыта вторая сторона, с тем же остатком."""
    club_a, _, adm_a, _, _ = await _scene(db)
    async with db.write() as tx:
        await db.create_payout(tx, adm_a)
    rows = {r["id"]: r for r in await db.vrheaven_unpaid_orders()}

    assert round(rows[1]["vr_share"], 2) == 440
    assert round(rows[2]["vr_share"], 2) == 250


async def test_summary_drops_an_order_once_both_sides_are_paid(db):
    club_a, _, adm_a, _, _ = await _scene(db)
    async with db.write() as tx:
        await db.create_payout(tx, adm_a)
        await db.create_payout(tx, club_a)
    assert [r["id"] for r in await db.vrheaven_unpaid_orders()] == [3, 4]


async def test_summary_shows_a_negative_remainder_of_a_single_order(db):
    """Лесенка не зависит от цены заказа, поэтому остаток по заказу может
    быть отрицательным (SPEC §3) — и на экране он таким и стоит."""
    owner = await create_owner(db)                       # 30%
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300, series_pos=5)  # 250 / 90 / −40
    rows = _table_rows((await reports.vrheaven_summary(db)).to_html(rich=True))

    assert ["№1", "adm1", fmt_num(300), fmt_num(-40)] in rows


async def test_summary_order_table_shows_the_last_rows_and_says_so(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    for _ in range(reports.MAX_TABLE_ROWS + 3):
        await make_order(db, admin, owner, price=300)
    html = (await reports.vrheaven_summary(db)).to_html(rich=True)
    rows = [r for r in _table_rows(html) if r and r[0].startswith("№")]

    assert len(rows) == reports.MAX_TABLE_ROWS
    assert rows[-1][0] == f"№{reports.MAX_TABLE_ROWS + 3}", "показаны последние"
    assert f"Показаны последние {reports.MAX_TABLE_ROWS} из" in html


async def test_busy_summary_still_fits_and_keeps_its_footer(db):
    """Третья таблица не должна стоить сводке итога: длинный отчёт режется
    по середине, последний блок остаётся целым (markup.Report.to_html)."""
    owners = [await create_owner(db, f"club{i:02d}") for i in range(20)]
    for i, owner in enumerate(owners):
        admin = await create_admin(db, owner, handle=f"adm{i:02d}")
        for _ in range(5):
            await make_order(db, admin, owner, price=700, series_pos=1)
    report = await reports.vrheaven_summary(db)

    for rich in (True, False):
        html = report.to_html(rich=rich)
        assert len(html) <= TEXT_LIMIT
        assert f"Остаток VR Heaven: {fmt_money(44000)}" in html
        validate_html(html)


async def test_summary_without_orders_has_no_order_table(db):
    await create_owner(db)
    await create_admin(db)
    html = await _html(await reports.vrheaven_summary(db))
    assert "<b>Заказы</b>" not in html


# ------------------------------- Цена и остаток VR Heaven — только VR Heaven

async def test_admin_and_owner_never_see_the_vrheaven_order_columns(db):
    """Восстановленные столбцы принадлежат супер-админу. У владельца
    полного распределения нет никогда (SPEC §6), администратору цена
    заказа не нужна: его вознаграждение считается лесенкой."""
    club_a, _, adm_a, _, _ = await _scene(db)
    for report in (await reports.admin_period(db, adm_a, TZ),
                   await reports.owner_period(db, club_a, TZ),
                   await reports.owner_admins(db, club_a)):
        html = await _html(report)
        assert "Остаток VR Heaven" not in html
        assert "Цена" not in html
        for price in (fmt_num(700), fmt_num(500)):
            assert price not in html, "цена заказа — цифра VR Heaven"


# --------------------------------------------- Подвал: по факту на строку

SUMMARY_METRICS = (
    "Заказов за период: 2",
    "Оборот: ",
    "К выплате владельцам: ",
    "К выплате администраторам: ",
    "Остаток VR Heaven: ",
)


async def test_summary_footer_puts_every_metric_on_its_own_line(db):
    """Пять величин в одной строке не читаются вовсе. Проверяются оба
    способа показа: внутри <p> нативного сообщения «\n» схлопывается в
    пробел, поэтому моноширинный отчёт выглядел правильно, а настоящий —
    сплошной строкой (markup.breaks)."""
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    direct = await create_admin(db, None, handle="direct")
    await make_order(db, admin, owner, price=700)
    await make_order(db, direct, None, price=300)
    report = await reports.vrheaven_summary(db)

    for rich in (True, False):
        shown = _visible_lines(report.to_html(rich=rich))
        for metric in SUMMARY_METRICS:
            owning = [line for line in shown if metric in line]
            assert len(owning) == 1, f"{metric} при rich={rich}"
            other = [m for m in SUMMARY_METRICS
                     if m != metric and m in owning[0]]
            assert not other, f"{metric} склеен с {other} при rich={rich}"


async def test_daily_digest_puts_every_metric_on_its_own_line(db, config):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    report = await reports.daily_digest(db, TZ, errors=3, backup_note="Копия: ок")

    for rich in (True, False):
        shown = _visible_lines(report.to_html(rich=rich))
        for metric in ("Заказов за сутки: 1", f"Оборот: {fmt_money(700)}",
                       "Отмен: 0", "Доставка в очереди: 0", "Не доставлено: 0",
                       "Отброшено: 0", "Ошибок в работе: 3"):
            assert metric in shown, f"{metric} при rich={rich}"


async def test_admin_statistics_put_every_metric_on_its_own_line(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=700)
    async with db.write() as tx:
        await db.create_bonus(tx, admin["id"], 200.0, "за инициативу")
    report = await reports.admin_period(db, admin, TZ)

    for rich in (True, False):
        shown = _visible_lines(report.to_html(rich=rich))
        for metric in (f"Вознаграждение за заказы: {fmt_money(50)}",
                       "Бонусы и удержания: +200 ₽",
                       f"К выплате: {fmt_money(250)}",
                       f"Следующий заказ: {fmt_money(100)}"):
            assert metric in shown, f"{metric} при rich={rich}"
        assert any(re.fullmatch(r"Серия с \d\d:\d\d: заказов 1", line)
                   for line in shown), f"строка серии при rich={rich}"


def test_the_period_total_is_called_the_same_everywhere():
    """«Накоплено к выплате» и «К выплате» — одна величина, и слово о ней
    одно: иначе одна и та же сумма выглядит двумя разными понятиями."""
    root = Path(__file__).resolve().parent.parent
    for name in ("reports.py", "scheduler.py", "keyboards.py",
                 "handlers/staff.py", "handlers/vrheaven.py"):
        assert "Накоплено" not in (root / name).read_text(encoding="utf-8"), name
