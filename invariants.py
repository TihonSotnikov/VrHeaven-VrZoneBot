"""Проверка деловых инвариантов — над любым файлом базы.

Проверяются утверждения о деньгах, а не пути в коде, поэтому этот модуль
одинаково применим к рабочей базе, к резервной копии и к тестовым данным.
Запускается в проверке резервных копий, в тестах и вручную во время
разбора происшествий:

    python invariants.py /путь/к/adminbot.db
"""

import sqlite3
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pricing import SALARY_LADDER, ladder_amount, normalize_reset, series_start

DEFAULT_TZ = "Asia/Novosibirsk"


def _cents(value: float) -> int:
    return round(float(value) * 100)


def check_database(path: str, tz_name: str = DEFAULT_TZ) -> list[str]:
    """Возвращает список нарушений; пустой список — всё сходится."""
    tz = ZoneInfo(tz_name)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        problems: list[str] = []
        problems += _check_sqlite(conn)
        problems += _check_order_shares(conn)
        problems += _check_payouts(conn)
        problems += _check_uniqueness(conn)
        problems += _check_series(conn, tz)
        return problems
    finally:
        conn.close()


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
            value = row[field]
            if _cents(value) != round(float(value) * 100):
                problems.append(f"заказ №{row['id']}: {field} не кратно копейке")
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
    for payout in conn.execute("SELECT p.*, u.role FROM payouts p"
                               " JOIN users u ON u.id = p.user_id"):
        share_col = "admin_share" if payout["role"] == "admin" else "owner_share"
        payout_col = "admin_payout_id" if payout["role"] == "admin" else "owner_payout_id"
        row = conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM({share_col}), 0) AS total"
            f" FROM orders WHERE {payout_col} = ?",
            (payout["id"],),
        ).fetchone()
        bonuses = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM bonuses WHERE payout_id = ?",
            (payout["id"],),
        ).fetchone()["total"]
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
        if payout["amount"] == 0 and row["n"] == 0 and bonuses == 0:
            problems.append(f"выплата №{payout['id']}: пустая")
    return problems


def _check_uniqueness(conn) -> list[str]:
    problems = []
    for row in conn.execute(
        "SELECT handle, COUNT(*) AS n FROM users"
        " WHERE is_active = 1 AND deleted_at IS NULL GROUP BY handle HAVING n > 1"
    ):
        problems.append(f"логин «{row['handle']}» у {row['n']} действующих записей")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(promos)")}
    name_col = "name_folded" if "name_folded" in columns else "name"
    for row in conn.execute(
        f"SELECT {name_col} AS name, COUNT(*) AS n FROM promos"
        f" WHERE archived_at IS NULL GROUP BY {name_col} HAVING n > 1"
    ):
        problems.append(f"акция «{row['name']}» существует в {row['n']} экземплярах")
    order_columns = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    if "client_token" in order_columns:
        for row in conn.execute(
            "SELECT client_token, COUNT(*) AS n FROM orders"
            " WHERE client_token IS NOT NULL GROUP BY client_token HAVING n > 1"
        ):
            problems.append(f"один ключ оформления у {row['n']} заказов")
    return problems


def _check_series(conn, tz) -> list[str]:
    """В одной 12-часовой серии администратора не бывает двух заказов
    с одинаковым местом — иначе кто-то недополучил вознаграждение."""
    problems = []
    resets = {row["id"]: row["series_reset_min"]
              for row in conn.execute("SELECT id, series_reset_min FROM users")}
    seen: dict[tuple, int] = {}
    for row in conn.execute(
        "SELECT id, admin_id, series_pos, created_at FROM orders"
        " WHERE cancelled_at IS NULL AND series_pos IS NOT NULL ORDER BY id"
    ):
        created = datetime.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        start = series_start(normalize_reset(resets.get(row["admin_id"])),
                             created.astimezone(tz))
        key = (row["admin_id"], start.isoformat(), row["series_pos"])
        if key in seen:
            problems.append(
                f"заказы №{seen[key]} и №{row['id']}: одно место"
                f" {row['series_pos']} в одной серии"
            )
        else:
            seen[key] = row["id"]
    if SALARY_LADDER != (50.0, 100.0, 150.0, 200.0, 250.0):
        problems.append("лесенка вознаграждения отличается от описанной в SPEC")
    return problems


def main() -> int:
    if len(sys.argv) < 2:
        print("Использование: python invariants.py <файл базы> [часовой пояс]")
        return 2
    tz_name = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TZ
    problems = check_database(sys.argv[1], tz_name)
    if not problems:
        print("Инварианты выполнены: расхождений нет")
        return 0
    print(f"Найдено расхождений: {len(problems)}")
    for problem in problems:
        print(" -", problem)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
