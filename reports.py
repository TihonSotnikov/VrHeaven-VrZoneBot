"""Отчёты — независимые от способа показа.

Каждый отчёт собирается как markup.Report: заголовок, абзацы и таблицы.
Показать его можно нативной таблицей Telegram (Rich Messages) или
моноширинным блоком — решает messaging.Messenger. Отчёт об этом не знает,
поэтому переход на запасной вариант не требует ни одной правки здесь.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from db import Database
from markup import Report, Table, h, lines
from pricing import ladder_amount, normalize_reset, order_row_label, series_start
from utils import fmt_dt, fmt_money, fmt_num, fmt_signed_money

MAX_TABLE_ROWS = 25

PERIOD_NOTE = ("Период закрывается выплатой, календарных границ нет. "
               "Плановые выплаты — 10-го и 25-го числа")


def month_start_utc_iso(tz: ZoneInfo) -> tuple[str, datetime]:
    """Начало текущего месяца: (граница UTC для БД, локальный момент для показа).

    Отчёт о состоянии считается от 1-го числа, а не за сутки: сбой
    доставки замечают не в тот же день, и суточное окно прячет ровно то,
    ради чего отчёт открывают.
    """
    start = datetime.now(tz).replace(day=1, hour=0, minute=0, second=0,
                                     microsecond=0)
    return start.astimezone(UTC).isoformat(timespec="seconds"), start


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
    живёт в подвале, за последней таблицей (markup.Report.to_html).
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


async def vrheaven_summary(db: Database, *, tables: bool = True) -> Report:
    """Сводка текущего периода: владельцы, администраторы и остаток VR Heaven.

    tables=False оставляет один подвал — те же числа, посчитанные тем же
    кодом, без расшифровок. Так сводку зовёт рассылка дня выплат:
    напоминание читают с телефона, а расшифровка и без того лежит
    за кнопкой меню.

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

    Таблица «Заказы» — единственное место в боте, где рядом стоят цена
    заказа и остаток VR Heaven по нему. Обе цифры принадлежат только
    VR Heaven: администратору цена не нужна (вознаграждение считается
    лесенкой), владельцу не показывается ничего, кроме его доли
    (SPEC §6), а полное распределение владелец не видит никогда. Сводка
    собирается единственным вызовом из панели VR Heaven и рассылки дня
    выплат — обе точки закрыты фильтром супер-админа.
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
        if tables:
            report.add(h("<b>Владельцы</b>")).add(table)
    if admins:
        table, admins_total = _summary_table(admins, with_bonus=True)
        if tables:
            report.add(h("<b>Администраторы</b>")).add(table)
        bonus_total = round(sum(r["bonus_sum"] for r in admins), 2)
    if tables:
        orders = await db.vrheaven_unpaid_orders()
        if orders:
            shown = orders[-MAX_TABLE_ROWS:]
            report.add(h("<b>Заказы</b>")).add(Table(
                ["Заказ", "Админ", "Цена, ₽", "Остаток VR Heaven, ₽"],
                [[f"№{o['id']}", o["admin_handle"], fmt_num(o["price"]),
                  fmt_num(o["vr_share"])] for o in shown],
            ))
            if len(orders) > MAX_TABLE_ROWS:
                report.add(h("Показаны последние {} из {} заказов",
                             MAX_TABLE_ROWS, len(orders)))
    vr_total = await db.vrheaven_unpaid_total()
    vr_remainder = round(vr_total["share_sum"] - bonus_total, 2)
    # По факту на строку: пять величин в одну строку не читаются, а внутри
    # нативного сообщения «\n» ещё и схлопывается в пробел — перевод строки
    # превращается в разрыв на сборке отчёта (markup.Report.to_html)
    footer = h("Заказов за период: {}\nОборот: {}",
               vr_total["orders_count"], fmt_money(vr_total["turnover"]))
    footer += h("\n<b>К выплате владельцам: {}</b>"
                "\n<b>К выплате администраторам: {}</b>",
                fmt_money(owners_total), fmt_money(admins_total))
    if bonus_total:
        # Столбец «Остаток VR Heaven» суммируется в остаток по заказам, а
        # не в итоговый: бонус не принадлежит ни одному заказу и вычитается
        # из остатка целиком. Без этой строки сумма столбца не сходится с
        # подвалом, и сходиться ей не с чем
        footer += h("\nв том числе бонусы: {}", fmt_money(bonus_total))
        footer += h("\nОстаток по заказам: {}",
                    fmt_money(vr_total["share_sum"]))
    footer += h("\n<b>Остаток VR Heaven: {}</b>", fmt_money(vr_remainder))
    return report.add(footer)


async def _series_line(db: Database, user, tz: ZoneInfo) -> str:
    """Место в текущей 12-часовой серии и цена следующего заказа.

    Две строки, а не одна: сделанное и следующая ступень — разные факты,
    и вторую администратор ищет глазами чаще первой.
    """
    reset_min = normalize_reset(user["series_reset_min"])
    since_iso, start_local = series_start_utc_iso(reset_min, tz)
    in_series = await db.count_series_orders(user["id"], since_iso)
    return lines(
        h("Серия с {}: заказов {}", start_local.strftime("%H:%M"), in_series),
        h("Следующий заказ: {}", fmt_money(ladder_amount(in_series + 1))),
    )


async def admin_period(db: Database, user, tz: ZoneInfo, *,
                       title: str = "Моя статистика",
                       with_series: bool = True,
                       note: str = PERIOD_NOTE) -> Report:
    """Статистика администратора: расшифровка периода, итог, серия.

    Порядок не косметический. Экран читается сверху вниз одним
    движением: каждая таблица названа своей подписью, а под ними стоят
    три величины — что начислено по заказам, что добавили бонусы и
    удержания и сколько выходит к выплате. Слагаемые стоят рядом с
    итогом, а не через полэкрана от него, и каждое — под своей таблицей.

    Итог внизу не теряется на обрезке длинного отчёта: режутся строки
    таблиц, а всё, что стоит после последней таблицы, остаётся целым
    (markup.Report.to_html).

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

    if orders:
        # Подпись над таблицей называет, что в ней лежит, — так же, как
        # у второй таблицы. Безымянная таблица под шапкой отчёта читалась
        # как продолжение шапки
        shown = orders[-MAX_TABLE_ROWS:]
        report.add(h("<b>Заказы</b>")).add(Table(
            ["Дата", "Заказ", "Вознаграждение, ₽"],
            [[fmt_dt(o["created_at"], tz, "%d.%m %H:%M"), order_row_label(o),
              fmt_num(o["admin_share"])] for o in shown],
        ))
        if len(orders) > MAX_TABLE_ROWS:
            report.add(h("Показаны последние {} из {} заказов",
                         MAX_TABLE_ROWS, len(orders)))
    if bonuses:
        # Столбцы второй таблицы себя не называют: «Сумма» бывает и
        # бонусом, и удержанием. Подпись над таблицей стоит дешевле
        # широкого заголовка столбца
        report.add(h("<b>Бонусы и удержания</b>")).add(Table(
            ["Дата", "Сумма, ₽", "За что"],
            [[fmt_dt(b["created_at"], tz, "%d.%m"), fmt_num(b["amount"]),
              b["comment"]] for b in bonuses[-MAX_TABLE_ROWS:]],
        ))

    # Итоговый блок — по факту на строку и без выделений: подписи здесь
    # не соперничают с подписями таблиц, иначе одни и те же слова
    # («Бонусы и удержания») читаются то заголовком, то величиной.
    # Без бонусов «Вознаграждение за заказы» и «К выплате» — одно и то же
    # число, написанное дважды: слагаемые показываем там, где их больше
    # одного, а расшифровка по заказам и так стоит таблицей выше
    money = h("Вознаграждение за заказы: {}\nБонусы и удержания: {}\n",
              fmt_money(share), fmt_signed_money(bonus_sum)) if bonuses else ""
    money += h("К выплате: {}", fmt_money(due))
    if due < 0:
        # Голое «−150 ₽» не объясняет ни выплаты, которой не будет, ни
        # того, куда денется остаток (SPEC §4)
        money += ("\nИтог отрицательный — выплаты не будет: "
                  "удержание перейдёт в следующий период")
    report.add(money)

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

    Порядок тот же, что в статистике администратора: названная таблица —
    расшифровка, итог — под ней. Слагаемых у владельца нет, поэтому
    величина внизу ровно одна: доля по заказам и есть его «К выплате».
    """
    report = Report(f"{title} · {user['handle']}")
    orders = await db.owner_unpaid_orders(user["id"])
    if not orders:
        return report.add("Заказов в текущем периоде пока нет").add(note)

    shown = orders[-MAX_TABLE_ROWS:]
    report.add(h("<b>Заказы</b>")).add(Table(
        ["Дата", "Админ", "Ваша доля, ₽"],
        [[fmt_dt(o["created_at"], tz, "%d.%m %H:%M"), o["admin_handle"],
          fmt_num(o["owner_share"])] for o in shown],
    ))
    if len(orders) > MAX_TABLE_ROWS:
        report.add(h("Показаны последние {} из {} заказов",
                     MAX_TABLE_ROWS, len(orders)))
    report.add(h("К выплате: {}",
                 fmt_money(round(sum(o["owner_share"] for o in orders), 2))))
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


async def system_report(db: Database, tz: ZoneInfo, *, errors: int,
                        backups) -> Report:
    """Состояние самого бота по команде /system: доставка, сбои, копии.

    Здесь только здоровье бота. Заказы, оборот и отмены отсюда убраны
    намеренно: деньги живут в сводке, и один экран, где к обороту
    приклеено число недоставленных сообщений, не отвечает толком ни на
    один из двух вопросов.

    Подписи называют то, что меряют. «Доставка в очереди» и «Отброшено»
    не говорили человеку ничего: речь о сообщениях, которые бот шлёт
    людям — чеках, уведомлениях, отчётах, — и ровно так они и названы.

    Счётчик сбоев живёт в памяти процесса и обнуляется его перезапуском,
    поэтому подпись говорит, с какого момента он считает: раньше его
    чистила утренняя сводка, и «за сутки» было правдой; теперь отчёт
    зовут руками, и обнулять его нажатием одного из трёх супер-админов
    значило бы прятать числа от двух остальных.
    """
    since_iso, since = month_start_utc_iso(tz)
    report = Report(f"Состояние бота · {datetime.now(tz).strftime('%d.%m.%Y %H:%M')}")
    outbox = await db.outbox_stats(since_iso)
    report.add(lines(
        h("<b>Сообщения бота людям</b> — чеки, уведомления, отчёты"),
        h("Считаем с {}", since.strftime("%d.%m.%Y")),
        h("Ждут отправки: {}", outbox["pending"]),
        h("Не доставлены, попытки закончились: {}", outbox["failed"]),
        h("Не доставлены, чат недоступен или файл потерян: {}", outbox["dropped"]),
    ))
    report.add(h("Сбоев при обработке нажатий и команд, с запуска бота: {}",
                 errors))
    if backups:
        newest = backups[-1]
        report.add(h("Копий базы: {}\nСвежая: {} ({} КБ)", len(backups),
                     newest.created.strftime("%d.%m %H:%M UTC"),
                     newest.size // 1024))
    else:
        report.add("Копий базы нет — проверьте задачу резервного копирования")
    return report
