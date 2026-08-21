"""Отчёты — независимые от способа показа.

Каждый отчёт собирается как markup.Report: заголовок, абзацы и таблицы.
Показать его можно нативной таблицей Telegram (Rich Messages) или
моноширинным блоком — решает messaging.Messenger. Отчёт об этом не знает,
поэтому переход на запасной вариант не требует ни одной правки здесь.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from db import Database
from markup import Report, Table, h
from pricing import ladder_amount, normalize_reset, order_row_label, series_start
from utils import fmt_dt, fmt_money, fmt_num

MAX_TABLE_ROWS = 25

PERIOD_NOTE = ("Период закрывается выплатой, календарных границ нет. "
               "Плановые выплаты — 1-го и 15-го числа")


def day_start_utc_iso(tz: ZoneInfo) -> str:
    """Начало текущего локального дня в UTC — граница строки «Сегодня»."""
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).isoformat(timespec="seconds")


def series_start_utc_iso(reset_min, tz: ZoneInfo) -> tuple[str, datetime]:
    """Начало текущей серии администратора: (граница UTC для БД, локальный
    момент для показа)."""
    start_local = series_start(reset_min, datetime.now(tz))
    return start_local.astimezone(UTC).isoformat(timespec="seconds"), start_local


def _summary_table(rows, with_bonus: bool = False) -> tuple[Table, float]:
    """Таблица сводки по одной роли; возвращает (таблица, сумма к выплате)."""
    headers = ["Логин", "Заказы", "Оборот", "К выплате"]
    if with_bonus:
        headers.insert(3, "Бонусы")
    table_rows = []
    total_n, total_turnover, total_bonus, total_due = 0, 0.0, 0.0, 0.0
    for r in rows:
        row = [r["handle"], r["orders_count"], fmt_num(r["turnover"]),
               fmt_num(r["due_sum"])]
        if with_bonus:
            row.insert(3, fmt_num(r["bonus_sum"]))
        table_rows.append(row)
        total_n += r["orders_count"]
        total_turnover += r["turnover"]
        total_bonus += r["bonus_sum"]
        total_due += r["due_sum"]
    total_row = ["Итого", total_n, fmt_num(total_turnover), fmt_num(total_due)]
    if with_bonus:
        total_row.insert(3, fmt_num(total_bonus))
    table_rows.append(total_row)
    return Table(headers, table_rows), round(total_due, 2)


async def vrheaven_summary(db: Database) -> Report:
    """Сводка текущего периода: владельцы, администраторы и остаток VR Heaven.

    Показываются действующие (даже без заказов) и приостановленные
    с ненулевым остатком; обороты обеих таблиц считаются по одним и тем же
    заказам. «К выплате» администратора включает его невыплаченные бонусы.
    Остаток VR Heaven суммируется по заказам, у которых открыта хотя бы
    одна доля, за вычетом невыплаченных бонусов — бонусы платит VR Heaven
    из своего остатка.
    """
    report = Report("Сводка за текущий период")
    owners = [r for r in await db.owners_unpaid_summary()
              if r["is_active"] or r["orders_count"] > 0]
    admins = [r for r in await db.admins_unpaid_summary()
              if r["is_active"] or r["orders_count"] > 0 or r["bonus_sum"] > 0]
    if not owners and not admins:
        return report.add("Владельцы и администраторы пока не подключены")

    owners_total, admins_total, bonus_total = 0.0, 0.0, 0.0
    if owners:
        table, owners_total = _summary_table(owners)
        report.add(h("<b>Владельцы</b>")).add(table)
    if admins:
        table, admins_total = _summary_table(admins, with_bonus=True)
        report.add(h("<b>Администраторы</b>")).add(table)
        bonus_total = round(sum(r["bonus_sum"] for r in admins), 2)
    vr_total = await db.vrheaven_unpaid_total()
    vr_remainder = round(vr_total["share_sum"] - bonus_total, 2)
    footer = h("<b>К выплате владельцам: {}</b>\n<b>К выплате администраторам: {}</b>",
               fmt_money(owners_total), fmt_money(admins_total))
    if bonus_total:
        footer += h("\nв том числе бонусы: {}", fmt_money(bonus_total))
    footer += h("\n<b>Остаток VR Heaven: {}</b>", fmt_money(vr_remainder))
    return report.add(footer)


async def admin_period(db: Database, user, tz: ZoneInfo, *,
                       title: str = "Моя статистика",
                       with_today: bool = True) -> Report:
    """Статистика администратора: «Сегодня», текущая серия, заказы периода
    и бонусы; итог к выплате — доля вместе с бонусами."""
    report = Report(f"{title} · {user['handle']}")
    if with_today:
        today = await db.admin_today_total(user["id"], day_start_utc_iso(tz))
        report.add(h("Сегодня: заказов {} · оборот {}",
                     today["orders_count"], fmt_money(today["turnover"])))
        reset_min = normalize_reset(user["series_reset_min"])
        since_iso, start_local = series_start_utc_iso(reset_min, tz)
        in_series = await db.count_series_orders(user["id"], since_iso)
        report.add(h("Серия с {}: заказов {} · следующий заказ — {}",
                     start_local.strftime("%H:%M"), in_series,
                     fmt_money(ladder_amount(in_series + 1))))
    orders = await db.admin_unpaid_orders(user["id"])
    bonuses = await db.admin_unpaid_bonuses(user["id"])
    bonus_sum = round(sum(b["amount"] for b in bonuses), 2)
    if not orders and not bonuses:
        return report.add("Заказов в текущем периоде пока нет")

    turnover = round(sum(o["price"] for o in orders), 2)
    share = round(sum(o["admin_share"] for o in orders), 2)
    if orders:
        shown = orders[-MAX_TABLE_ROWS:]
        report.add(Table(
            ["Дата", "Заказ", "Сумма", "Доля"],
            [[fmt_dt(o["created_at"], tz, "%d.%m %H:%M"), order_row_label(o),
              fmt_num(o["price"]), fmt_num(o["admin_share"])] for o in shown],
        ))
        if len(orders) > MAX_TABLE_ROWS:
            report.add(h("Показаны последние {} из {} заказов",
                         MAX_TABLE_ROWS, len(orders)))
    else:
        report.add("Заказов в текущем периоде пока нет")
    if bonuses:
        report.add(Table(
            ["Дата", "Бонус", "За что"],
            [[fmt_dt(b["created_at"], tz, "%d.%m"), fmt_num(b["amount"]),
              b["comment"]] for b in bonuses[-MAX_TABLE_ROWS:]],
        ))
    footer = h("Заказов: {}\nОборот: {}", len(orders), fmt_money(turnover))
    if bonuses:
        footer += h("\nБонусы: {}", fmt_money(bonus_sum))
    footer += h("\n<b>К выплате: {}</b>", fmt_money(round(share + bonus_sum, 2)))
    report.add(footer)
    return report.add(PERIOD_NOTE)


async def owner_period(db: Database, user, tz: ZoneInfo, *,
                       title: str = "Текущий период") -> Report:
    """Отчёт владельца по заказам его администраторов с невыплаченной долей."""
    report = Report(f"{title} · {user['handle']}")
    orders = await db.owner_unpaid_orders(user["id"])
    if not orders:
        return report.add("Заказов в текущем периоде пока нет").add(PERIOD_NOTE)

    shown = orders[-MAX_TABLE_ROWS:]
    report.add(Table(
        ["Дата", "Админ", "Сумма", "Доля"],
        [[fmt_dt(o["created_at"], tz, "%d.%m %H:%M"), o["admin_handle"],
          fmt_num(o["price"]), fmt_num(o["owner_share"])] for o in shown],
    ))
    if len(orders) > MAX_TABLE_ROWS:
        report.add(h("Показаны последние {} из {} заказов",
                     MAX_TABLE_ROWS, len(orders)))
    report.add(h("Заказов: {}\nОборот: {}\n<b>К выплате: {}</b>",
                 len(orders), fmt_money(round(sum(o["price"] for o in orders), 2)),
                 fmt_money(round(sum(o["owner_share"] for o in orders), 2))))
    return report.add(PERIOD_NOTE)


async def owner_admins(db: Database, user) -> Report:
    """Администраторы владельца с их заказами за текущий период."""
    report = Report(f"Мои администраторы · {user['handle']}")
    rows = await db.owner_admins_summary(user["id"])
    if not rows:
        return report.add("Администраторы пока не подключены")
    report.add(Table(
        ["Логин", "Заказы", "Оборот", "Доля"],
        [[r["handle"], r["orders_count"], fmt_num(r["turnover"]),
          fmt_num(r["share_sum"])] for r in rows],
    ))
    return report.add("Заказы, оборот и доля — за текущий период")


def payout_history(payouts, tz: ZoneInfo, handle: str) -> Report:
    report = Report(f"История выплат · {handle}")
    if not payouts:
        return report.add("Выплат пока не было")
    return report.add(Table(
        ["Дата", "Сумма", "Заказы"],
        [[fmt_dt(p["created_at"], tz, "%d.%m.%Y"), fmt_num(p["amount"]),
          p["orders_count"]] for p in payouts],
    ))


async def daily_digest(db: Database, tz: ZoneInfo, *, errors: int,
                       backup_note: str) -> Report:
    """Утренняя сводка супер-админам: обороты, доставка, копии, ошибки.

    Смысл ровно один: то, что раньше молча оседало в журнале сервера,
    теперь каждый день попадает человеку на глаза.
    """
    report = Report(f"Сводка дня · {datetime.now(tz).strftime('%d.%m.%Y')}")
    since = day_start_utc_iso(tz)
    orders = await db.fetchone(
        "SELECT COUNT(*) AS n, COALESCE(SUM(price), 0) AS turnover FROM orders"
        " WHERE created_at >= ? AND cancelled_at IS NULL", (since,)
    )
    cancelled = await db.fetchone(
        "SELECT COUNT(*) AS n FROM orders WHERE cancelled_at >= ?", (since,)
    )
    outbox = await db.outbox_stats()
    report.add(h("Заказов за сутки: {} · оборот {}",
                 orders["n"], fmt_money(orders["turnover"])))
    report.add(h("Отмен за сутки: {}", cancelled["n"]))
    report.add(h("Доставка: в очереди {} · не доставлено {} · отброшено {}",
                 outbox["pending"], outbox["failed"], outbox["dropped"]))
    report.add(h("Ошибок в работе: {}", errors))
    report.add(backup_note)
    actions = await db.audit_since(since)
    if actions:
        counts: dict[str, int] = {}
        for row in actions:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        report.add(Table(
            ["Действие", "Раз"],
            sorted(([action, n] for action, n in counts.items()), key=lambda r: -r[1]),
        ))
    return report
