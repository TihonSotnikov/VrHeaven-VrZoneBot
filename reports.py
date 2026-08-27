"""Отчёты — независимые от способа показа.

Каждый отчёт собирается как markup.Report: заголовок, абзацы и таблицы.
Показать его можно нативной таблицей Telegram (Rich Messages) или
моноширинным блоком — решает messaging.Messenger. Отчёт об этом не знает,
поэтому переход на запасной вариант не требует ни одной правки здесь.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from db import Database
from export import ACTION_LABELS
from markup import Report, Table, h, lines
from pricing import ladder_amount, normalize_reset, order_row_label, series_start
from utils import fmt_dt, fmt_money, fmt_num, fmt_signed_money

MAX_TABLE_ROWS = 25

PERIOD_NOTE = ("Период закрывается выплатой, календарных границ нет. "
               "Плановые выплаты — 1-го и 15-го числа")


def day_start_utc_iso(tz: ZoneInfo) -> str:
    """Начало текущего локального дня в UTC — граница суток сводки дня."""
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).isoformat(timespec="seconds")


def series_start_utc_iso(reset_min, tz: ZoneInfo) -> tuple[str, datetime]:
    """Начало текущей серии администратора: (граница UTC для БД, локальный
    момент для показа)."""
    start_local = series_start(reset_min, datetime.now(tz))
    return start_local.astimezone(UTC).isoformat(timespec="seconds"), start_local


def _summary_table(rows, with_bonus: bool = False) -> tuple[Table, float]:
    """Таблица сводки по одной роли; возвращает (таблица, сумма к выплате).

    Оборота в строках нет: у владельца и у администратора открыты разные
    заказы (доли закрываются независимо), поэтому столбцы «Оборот» двух
    таблиц складывались бы в два разных числа под одним словом. Оборот в
    сводке один — клубный, он стоит в итоге отчёта.

    Строки «Итого» тоже нет: суммы к выплате повторялись бы дважды на
    одном экране, а итог обязан пережить обрезку длинного отчёта — он
    живёт в последнем блоке (markup.Report.to_html).
    """
    headers = ["Логин", "Заказы", "К выплате, ₽"]
    if with_bonus:
        headers.insert(2, "Бонусы, ₽")
    table_rows = []
    total_due = 0.0
    for r in rows:
        row = [r["handle"], r["orders_count"], fmt_num(r["due_sum"])]
        if with_bonus:
            row.insert(2, fmt_num(r["bonus_sum"]))
        table_rows.append(row)
        total_due += r["due_sum"]
    return Table(headers, table_rows), round(total_due, 2)


async def vrheaven_summary(db: Database) -> Report:
    """Сводка текущего периода: владельцы, администраторы и остаток VR Heaven.

    Показываются действующие (даже без заказов) и приостановленные
    с ненулевым остатком. «К выплате» администратора включает его
    невыплаченные бонусы. Остаток VR Heaven суммируется по заказам,
    у которых открыта хотя бы одна доля, за вычетом невыплаченных
    бонусов — бонусы платит VR Heaven из своего остатка.

    Оборот в сводке ровно один и означает одно: сумма цен заказов
    периода — тех самых заказов, по которым внизу считается остаток
    (открыта хотя бы одна доля). Каждый заказ входит в него однажды,
    включая заказы прямых администраторов и удалённых получателей.
    По строкам таблиц оборот не раскладывается: доли администратора и
    владельца закрываются независимо, поэтому «оборот администраторов»
    и «оборот владельцев» — разные наборы заказов и разные числа.

    Приостановленный попадает в таблицу по числу открытых строк, а не по
    знаку суммы. Удержание — открытая строка с отрицательной суммой: она
    увеличивает остаток VR Heaven ровно так же, как бонус его уменьшает.
    Прежнее условие `bonus_sum > 0` прятало такую строку из таблицы, а
    вместе с ней и из остатка внизу — он считается по показанным строкам.
    Счёт строк заодно оставляет на виду того, у кого бонус и удержание
    сошлись в ноль: платить ему нечего, а закрыть период всё равно надо.
    """
    report = Report("Сводка за текущий период")
    owners = [r for r in await db.owners_unpaid_summary()
              if r["is_active"] or r["orders_count"] > 0]
    admins = [r for r in await db.admins_unpaid_summary()
              if r["is_active"] or r["orders_count"] > 0 or r["bonus_count"] > 0]
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
    footer = h("Заказов за период: {} · оборот {}",
               vr_total["orders_count"], fmt_money(vr_total["turnover"]))
    footer += h("\n<b>К выплате владельцам: {}</b>"
                "\n<b>К выплате администраторам: {}</b>",
                fmt_money(owners_total), fmt_money(admins_total))
    if bonus_total:
        footer += h("\nв том числе бонусы: {}", fmt_money(bonus_total))
    footer += h("\n<b>Остаток VR Heaven: {}</b>", fmt_money(vr_remainder))
    return report.add(footer)


async def _series_line(db: Database, user, tz: ZoneInfo) -> str:
    """Место в текущей 12-часовой серии и цена следующего заказа."""
    reset_min = normalize_reset(user["series_reset_min"])
    since_iso, start_local = series_start_utc_iso(reset_min, tz)
    in_series = await db.count_series_orders(user["id"], since_iso)
    return h("Серия с {}: заказов {} · следующий — {}",
             start_local.strftime("%H:%M"), in_series,
             fmt_money(ladder_amount(in_series + 1)))


async def admin_period(db: Database, user, tz: ZoneInfo, *,
                       title: str = "Моя статистика",
                       with_series: bool = True,
                       note: str = PERIOD_NOTE) -> Report:
    """Статистика администратора: деньги периода, их расшифровка, серия.

    Порядок не косметический. Экран открывают ради «к выплате», а
    длинный отчёт режется по середине (markup.Report.to_html), поэтому
    итог стоит наверху: он переживает обрезку, а таблица — расшифровка.
    Оборот администратору не показывается: его вознаграждение считается
    лесенкой серии и от цены заказа не зависит вовсе.
    """
    report = Report(f"{title} · {user['handle']}")
    orders = await db.admin_unpaid_orders(user["id"])
    bonuses = await db.admin_unpaid_bonuses(user["id"])
    if not orders and not bonuses:
        # Не «заказов нет»: строкой ниже стоит счёт заказов серии, и две
        # цифры об одном слове читались бы как противоречие. Пустой период —
        # про начисления, серия — про смену, это разные вещи
        report.add("В текущем периоде начислений пока нет")
        if with_series:
            report.add(await _series_line(db, user, tz))
        return report.add(note)

    share = round(sum(o["admin_share"] for o in orders), 2)
    bonus_sum = round(sum(b["amount"] for b in bonuses), 2)
    due = round(share + bonus_sum, 2)
    # Без бонусов «Вознаграждение за заказы» и «К выплате» — одно и то же
    # число, написанное дважды: слагаемые показываем там, где их больше
    # одного, а расшифровка по заказам и так стоит таблицей ниже
    money = h("Вознаграждение за заказы: {}\nБонусы и удержания: {}\n",
              fmt_money(share), fmt_signed_money(bonus_sum)) if bonuses else ""
    money += h("<b>К выплате: {}</b>", fmt_money(due))
    if due < 0:
        # Голое «−150 ₽» не объясняет ни выплаты, которой не будет, ни
        # того, куда денется остаток (SPEC §4)
        money += ("\nИтог отрицательный — выплаты не будет: "
                  "удержание перейдёт в следующий период")
    report.add(money)

    if orders:
        shown = orders[-MAX_TABLE_ROWS:]
        report.add(Table(
            ["Дата", "Заказ", "Вознаграждение, ₽"],
            [[fmt_dt(o["created_at"], tz, "%d.%m %H:%M"), order_row_label(o),
              fmt_num(o["admin_share"])] for o in shown],
        ))
        if len(orders) > MAX_TABLE_ROWS:
            report.add(h("Показаны последние {} из {} заказов",
                         MAX_TABLE_ROWS, len(orders)))
    if bonuses:
        # Столбцы второй таблицы себя не называют: «Сумма» бывает и
        # бонусом, и удержанием. Подпись над таблицей связывает её со
        # строкой «Бонусы и удержания» выше и стоит дешевле широкого
        # заголовка столбца
        report.add(h("<b>Бонусы и удержания</b>")).add(Table(
            ["Дата", "Сумма, ₽", "За что"],
            [[fmt_dt(b["created_at"], tz, "%d.%m"), fmt_num(b["amount"]),
              b["comment"]] for b in bonuses[-MAX_TABLE_ROWS:]],
        ))
    if with_series:
        report.add(await _series_line(db, user, tz))
    return report.add(note)


async def owner_period(db: Database, user, tz: ZoneInfo, *,
                       title: str = "Текущий период",
                       note: str = PERIOD_NOTE) -> Report:
    """Отчёт владельца по заказам его администраторов с невыплаченной долей.

    Владельцу показывается только его доля (SPEC §6): цена заказа и
    оборот клуба к его выплате отношения не имеют, а долю по каждому
    заказу он и так получает Записью в момент оформления.
    """
    report = Report(f"{title} · {user['handle']}")
    orders = await db.owner_unpaid_orders(user["id"])
    if not orders:
        return report.add("Заказов в текущем периоде пока нет").add(note)

    report.add(h("<b>К выплате: {}</b>",
                 fmt_money(round(sum(o["owner_share"] for o in orders), 2))))
    shown = orders[-MAX_TABLE_ROWS:]
    report.add(Table(
        ["Дата", "Админ", "Ваша доля, ₽"],
        [[fmt_dt(o["created_at"], tz, "%d.%m %H:%M"), o["admin_handle"],
          fmt_num(o["owner_share"])] for o in shown],
    ))
    if len(orders) > MAX_TABLE_ROWS:
        report.add(h("Показаны последние {} из {} заказов",
                     MAX_TABLE_ROWS, len(orders)))
    return report.add(note)


async def owner_admins(db: Database, user) -> Report:
    """Администраторы владельца: сколько каждый принёс ему за период.

    «Доля» без уточнения читается двояко — своя доля администратора или
    доля владельца с него, — поэтому столбец назван «Ваша доля» и внизу
    сказано, с чего она считается.
    """
    report = Report(f"Мои администраторы · {user['handle']}")
    rows = await db.owner_admins_summary(user["id"])
    if not rows:
        return report.add("Администраторы пока не подключены")
    report.add(Table(
        ["Логин", "Заказы", "Ваша доля, ₽"],
        [[r["handle"], r["orders_count"], fmt_num(r["share_sum"])] for r in rows],
    ))
    return report.add("Ваша доля с заказов каждого администратора "
                      "за текущий период")


def payout_history(payouts, tz: ZoneInfo, handle: str) -> Report:
    report = Report(f"История выплат · {handle}")
    if not payouts:
        return report.add("Выплат пока не было")
    return report.add(Table(
        ["Дата", "Сумма, ₽", "Заказы"],
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
    # Два блока, а не пять абзацев: сначала дела клуба, потом состояние
    # самого бота. Пять пустых строк между однострочными фактами
    # растягивали сводку на экран, ничего к ней не добавляя
    report.add(h("Заказов за сутки: {} · оборот {} · отмен {}",
                 orders["n"], fmt_money(orders["turnover"]), cancelled["n"]))
    report.add(lines(
        h("Доставка: в очереди {} · не доставлено {} · отброшено {}",
          outbox["pending"], outbox["failed"], outbox["dropped"]),
        h("Ошибок в работе: {}", errors),
        backup_note,
    ))
    actions = await db.audit_since(since)
    if actions:
        counts: dict[str, int] = {}
        for row in actions:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        report.add(Table(
            # Человеческие названия, а не коды журнала: сводку читает
            # владелец бизнеса, а «order.create» — слово для инженера
            ["Действие", "Раз"],
            sorted(([ACTION_LABELS.get(action, action), n]
                    for action, n in counts.items()), key=lambda r: -r[1]),
        ))
    return report
