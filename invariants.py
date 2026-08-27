"""Проверка деловых инвариантов — над любым файлом базы.

Проверяются утверждения о деньгах, а не пути в коде, поэтому этот модуль
одинаково применим к рабочей базе, к резервной копии и к тестовым данным.
Запускается в проверке резервных копий, в тестах и вручную во время
разбора происшествий:

    python invariants.py /путь/к/adminbot.db

Проверки не зависят ни от одной настройки: всё, что нужно для суждения о
заказе, лежит в самом заказе. Часовой пояс здесь больше не нужен —
граница серии хранится в заказе, а не выводится из текущего расписания
сбросов (миграция v3).
"""

import sqlite3
import sys
from bisect import bisect_left, insort
from dataclasses import dataclass

from pricing import SALARY_LADDER, ladder_amount

# Копейка в двоичной дроби точно не представима: 0.1 + 0.2 != 0.3.
# Допуск на порядки меньше копейки — расхождение сверх него означает,
# что сумма записана не по правилам округления, а не что float дрогнул.
CENT_EPSILON = 1e-6


def _cents(value: float) -> int:
    return round(float(value) * 100)


def _not_whole_cents(value) -> bool:
    """Сумма не кратна копейке — деньги записаны мимо дисциплины округления."""
    scaled = float(value) * 100
    return abs(scaled - round(scaled)) > CENT_EPSILON


@dataclass(frozen=True)
class Report:
    """Итог проверки, разделённый по последствиям.

    Разделение не косметическое. Непригодный файл нельзя опубликовать
    копией, нельзя развернуть и нельзя мигрировать: дальше с ним будет
    только хуже. Расхождение в деньгах — повод разобраться человеку, но
    не повод остановить копии, выкладку и запуск бота: данные от этого
    целее не станут, а единственный канал, которым о расхождении можно
    узнать, закроется вместе с копиями.
    """

    integrity: tuple[str, ...] = ()
    business: tuple[str, ...] = ()

    @property
    def problems(self) -> list[str]:
        return [*self.integrity, *self.business]


def inspect_database(path: str) -> Report:
    """Проверяет файл базы и раскладывает найденное по последствиям."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        business: list[str] = []
        business += _check_order_shares(conn)
        business += _check_payouts(conn)
        business += _check_uniqueness(conn)
        business += _check_series(conn)
        return Report(integrity=tuple(_check_sqlite(conn)), business=tuple(business))
    finally:
        conn.close()


def check_database(path: str) -> list[str]:
    """Возвращает список нарушений; пустой список — всё сходится."""
    return inspect_database(path).problems


def _tables(conn) -> set[str]:
    """Какие таблицы в файле есть.

    База прежней версии не знает ни бонусов, ни акций: они появляются
    миграцией v1. Судить о них по такому файлу нечем, а снимок с него
    снять обязательно — это единственная точка отката той самой миграции.
    """
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _check_sqlite(conn) -> list[str]:
    problems = []
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        problems.append(f"целостность файла: {result}")
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        problems.append(f"нарушены связи таблиц: {len(broken)} строк")
    return problems


def _check_order_shares(conn) -> list[str]:
    """Три части заказа обязаны в сумме давать цену — до копейки.

    Остаток VR Heaven не хранится: он и есть цена минус две доли, поэтому
    проверяются его составляющие — доля владельца считается его процентом,
    вознаграждение администратора берётся из лесенки.
    """
    problems = []
    for row in conn.execute(
        "SELECT id, price, admin_share, owner_share, owner_percent, series_pos,"
        " owner_id FROM orders"
    ):
        for field in ("price", "admin_share", "owner_share"):
            if _not_whole_cents(row[field]):
                problems.append(
                    f"заказ №{row['id']}: {field} = {row[field]} не кратно копейке"
                )
        expected_owner = round(float(row["price"]) * float(row["owner_percent"]) / 100, 2)
        if _cents(row["owner_share"]) != _cents(expected_owner):
            problems.append(
                f"заказ №{row['id']}: доля владельца {row['owner_share']}"
                f" не равна {expected_owner}"
            )
        if row["owner_id"] is None and _cents(row["owner_share"]) != 0:
            problems.append(f"заказ №{row['id']}: доля владельца без владельца")
        if row["series_pos"] is not None:
            expected_admin = ladder_amount(row["series_pos"])
            if _cents(row["admin_share"]) != _cents(expected_admin):
                problems.append(
                    f"заказ №{row['id']}: вознаграждение {row['admin_share']}"
                    f" не соответствует ступени {row['series_pos']}"
                    f" ({expected_admin})"
                )
        if row["series_pos"] is not None and row["series_pos"] < 1:
            problems.append(f"заказ №{row['id']}: место в серии {row['series_pos']}")
    return problems


def _check_payouts(conn) -> list[str]:
    """Сумма выплаты обязана сходиться с тем, что она закрыла."""
    problems = []
    has_bonuses = "bonuses" in _tables(conn)
    if has_bonuses:
        for row in conn.execute("SELECT id, amount FROM bonuses"):
            if _not_whole_cents(row["amount"]):
                problems.append(
                    f"бонус №{row['id']}: сумма {row['amount']} не кратна копейке")
    for row in conn.execute("SELECT id, amount FROM payouts"):
        if _not_whole_cents(row["amount"]):
            problems.append(
                f"выплата №{row['id']}: сумма {row['amount']} не кратна копейке")
    for payout in conn.execute("SELECT p.*, u.role FROM payouts p"
                               " JOIN users u ON u.id = p.user_id"):
        share_col = "admin_share" if payout["role"] == "admin" else "owner_share"
        payout_col = "admin_payout_id" if payout["role"] == "admin" else "owner_payout_id"
        row = conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM({share_col}), 0) AS total"
            f" FROM orders WHERE {payout_col} = ?",
            (payout["id"],),
        ).fetchone()
        closed_bonuses = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n"
            " FROM bonuses WHERE payout_id = ?",
            (payout["id"],),
        ).fetchone() if has_bonuses else None
        bonuses = closed_bonuses["total"] if closed_bonuses else 0
        bonus_count = closed_bonuses["n"] if closed_bonuses else 0
        expected = round(row["total"] + bonuses, 2)
        if _cents(expected) != _cents(payout["amount"]):
            problems.append(
                f"выплата №{payout['id']}: записано {payout['amount']},"
                f" по строкам {expected}"
            )
        if row["n"] != payout["orders_count"]:
            problems.append(
                f"выплата №{payout['id']}: заказов записано {payout['orders_count']},"
                f" фактически {row['n']}"
            )
        # Пустая — та, что не закрыла ни одной строки. Судить по сумме
        # бонусов нельзя: бонус и равное ему удержание дают ноль, а строки
        # настоящие, и период ими закрывается законно (SPEC §4)
        if payout["amount"] == 0 and row["n"] == 0 and bonus_count == 0:
            problems.append(f"выплата №{payout['id']}: пустая")
    return problems


def _check_uniqueness(conn) -> list[str]:
    problems = []
    for row in conn.execute(
        "SELECT handle, COUNT(*) AS n FROM users"
        " WHERE is_active = 1 AND deleted_at IS NULL GROUP BY handle HAVING n > 1"
    ):
        problems.append(f"логин «{row['handle']}» у {row['n']} действующих записей")
    if "promos" in _tables(conn):
        columns = {r[1] for r in conn.execute("PRAGMA table_info(promos)")}
        name_col = "name_folded" if "name_folded" in columns else "name"
        for row in conn.execute(
            f"SELECT {name_col} AS name, COUNT(*) AS n FROM promos"
            f" WHERE archived_at IS NULL GROUP BY {name_col} HAVING n > 1"
        ):
            problems.append(
                f"акция «{row['name']}» существует в {row['n']} экземплярах")
    order_columns = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    if "client_token" in order_columns:
        for row in conn.execute(
            "SELECT client_token, COUNT(*) AS n FROM orders"
            " WHERE client_token IS NOT NULL GROUP BY client_token HAVING n > 1"
        ):
            problems.append(f"один ключ оформления у {row['n']} заказов")
    return problems


def _check_series(conn) -> list[str]:
    """Место в серии обязано сходиться с числом заказов до него.

    Место считается при оформлении: неотменённые заказы серии плюс один.
    Отмена освобождает ступень (SPEC §3), поэтому после отмены заказа из
    середины серии следующий заказ законно получает место, которое уже
    занято живым заказом. Прежнее правило «двух заказов на одном месте не
    бывает» объявляло это нарушением — и одна законная отмена навсегда
    останавливала снятие копий базы.

    Серия заказа — окно от его собственной границы (`series_since`), а не
    выведенное из текущего расписания сбросов: настройка меняется,
    прошлое — нет. «Заказы до него» считаются по этому окну ровно так,
    как считал их `create_order`, а не складыванием заказов с одинаковой
    границей: VR Heaven может сдвинуть время сбросов посреди серии
    (SPEC §3), и тогда у соседних заказов границы разные, хотя считались
    они друг за другом. Счёт по совпадению границ объявлял бы такую пару
    нарушением при каждой проверке — навсегда.

    Проверяется то, что от отмен не зависит. Пусть в окне заказа перед
    ним стоит k−1 заказов, из которых l не отменены сейчас. Отмена
    необратима, значит на момент оформления неотменённых было не меньше l
    и не больше k−1, а место обязано лежать в [l+1, k]. Выход за границы
    означает, что ступень выдана мимо правила: гонка двух устройств,
    ручная правка, порча файла.

    Заказы прежних версий границы не несут, восстановить её нечем, и
    сравнивать их не с чем — они проверяются только по лесенке.
    """
    problems = []
    columns = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    if "series_since" in columns:
        # моменты оформления заказов администратора, отсортированные:
        # все и, отдельно, неотменённые сейчас
        placed: dict[int, list[str]] = {}
        live: dict[int, list[str]] = {}
        holder: dict[tuple, int] = {}          # чей живой заказ занял место
        for row in conn.execute(
            "SELECT id, admin_id, series_pos, series_since, created_at, cancelled_at"
            " FROM orders WHERE series_pos IS NOT NULL AND series_since IS NOT NULL"
            " ORDER BY id"
        ):
            admin, since = row["admin_id"], row["series_since"]
            before = placed.setdefault(admin, [])
            alive = live.setdefault(admin, [])
            pos = row["series_pos"]
            highest = len(before) - bisect_left(before, since) + 1
            lowest = len(alive) - bisect_left(alive, since) + 1
            if not lowest <= pos <= highest:
                twin = holder.get((admin, since, pos))
                problems.append(
                    f"заказы №{twin} и №{row['id']}: одно место {pos} в одной серии"
                    if twin is not None else
                    f"заказ №{row['id']}: место {pos} в серии не сходится"
                    f" с числом заказов до него ({lowest}…{highest})"
                )
            # моменты — UTC ISO одного вида, поэтому сравниваются строками
            created = row["created_at"] or ""
            insort(before, created)
            if row["cancelled_at"] is None:
                insort(alive, created)
                holder.setdefault((admin, since, pos), row["id"])
    if SALARY_LADDER != (50.0, 100.0, 150.0, 200.0, 250.0):
        problems.append("лесенка вознаграждения отличается от описанной в SPEC")
    return problems


# Коды возврата: развёртывание и откат читают именно их, поэтому разница
# между «файл непригоден» и «деньги не сходятся» выражена кодом, а не
# только текстом. Непригодный файл разворачивать нельзя; расхождение в
# деньгах — тревога человеку, а не причина остановить выкладку.
EXIT_OK = 0
EXIT_BROKEN_FILE = 1
EXIT_USAGE = 2
EXIT_BUSINESS = 3


def main() -> int:
    if len(sys.argv) < 2:
        print("Использование: python invariants.py <файл базы>")
        return EXIT_USAGE
    if len(sys.argv) > 2:
        # Прежние версии принимали часовой пояс вторым аргументом; он
        # больше ни на что не влияет, но и ронять проверку из-за него
        # посреди разбора происшествия незачем
        print("Часовой пояс больше не нужен: проверки от него не зависят",
              file=sys.stderr)
    report = inspect_database(sys.argv[1])
    if not report.problems:
        print("Инварианты выполнены: расхождений нет")
        return EXIT_OK
    if report.integrity:
        print(f"Файл базы непригоден: {len(report.integrity)}")
        for problem in report.integrity:
            print(" -", problem)
    if report.business:
        print(f"Деловые расхождения: {len(report.business)}")
        for problem in report.business:
            print(" -", problem)
    return EXIT_BROKEN_FILE if report.integrity else EXIT_BUSINESS


if __name__ == "__main__":
    raise SystemExit(main())
