"""Миграции схемы: перенос данных, атомарность, защита от отката.

Главное свойство: прерванная миграция не оставляет базу в состоянии
«версия новая, данных нет». Проверяется впрыском сбоя в середину.
"""

import sqlite3

import pytest

import migrations
from db import Database
from migrations import LATEST_VERSION, pending_migrations

# Схема бота до перевода акций в настройки и лесенки вознаграждения
LEGACY_SCHEMA = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    role          TEXT    NOT NULL CHECK (role IN ('owner', 'admin')),
    handle        TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    contact       TEXT    NOT NULL DEFAULT '',
    password_hash TEXT    NOT NULL,
    percent       REAL    NOT NULL,
    owner_id      INTEGER REFERENCES users(id),
    is_active     INTEGER NOT NULL DEFAULT 1,
    deleted_at    TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX idx_users_handle ON users(handle);
CREATE TABLE user_chats (
    chat_id    INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT    NOT NULL
);
CREATE TABLE payouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    amount       REAL    NOT NULL,
    orders_count INTEGER NOT NULL,
    created_at   TEXT    NOT NULL
);
CREATE TABLE orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id         INTEGER NOT NULL REFERENCES users(id),
    owner_id         INTEGER REFERENCES users(id),
    kind             TEXT    NOT NULL CHECK (kind IN ('standard', 'group')),
    headsets         INTEGER,
    minutes          INTEGER,
    base_price       REAL    NOT NULL,
    discount_percent REAL    NOT NULL DEFAULT 0,
    price            REAL    NOT NULL,
    admin_percent    REAL    NOT NULL,
    admin_share      REAL    NOT NULL,
    owner_percent    REAL    NOT NULL DEFAULT 0,
    owner_share      REAL    NOT NULL DEFAULT 0,
    admin_payout_id  INTEGER REFERENCES payouts(id),
    owner_payout_id  INTEGER REFERENCES payouts(id),
    cancelled_at     TEXT,
    created_at       TEXT    NOT NULL
);
CREATE INDEX idx_orders_admin_unpaid ON orders(admin_id)
    WHERE admin_payout_id IS NULL AND cancelled_at IS NULL;
CREATE TABLE settings (key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE windows (chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
"""


def _legacy_db(path: str, *, settings: dict | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO users (id, role, handle, name, password_hash, percent,"
        " created_at) VALUES (1, 'owner', 'club', 'Клуб', 'x', 30, '2026-07-01')")
    conn.execute(
        "INSERT INTO users (id, role, handle, name, password_hash, percent,"
        " owner_id, created_at)"
        " VALUES (2, 'admin', 'adm', 'Админ', 'x', 10, 1, '2026-07-01')")
    conn.execute("INSERT INTO user_chats VALUES (777, 2, '2026-07-01')")
    conn.execute(
        "INSERT INTO payouts (id, user_id, amount, orders_count, created_at)"
        " VALUES (1, 2, 50.0, 1, '2026-07-10')")
    conn.execute(
        "INSERT INTO orders (id, admin_id, owner_id, kind, headsets, minutes,"
        " base_price, discount_percent, price, admin_percent, admin_share,"
        " owner_percent, owner_share, admin_payout_id, created_at)"
        " VALUES (1, 2, 1, 'standard', 1, 60, 500, 0, 500, 10, 50, 30, 150,"
        " 1, '2026-07-05T10:00:00+00:00')")
    conn.execute(
        "INSERT INTO orders (id, admin_id, owner_id, kind, base_price,"
        " discount_percent, price, admin_percent, admin_share, owner_percent,"
        " owner_share, cancelled_at, created_at)"
        " VALUES (2, 2, 1, 'group', 600, 0, 600, 10, 60, 30, 180,"
        " '2026-07-06T11:00:00+00:00', '2026-07-06T10:00:00+00:00')")
    for key, value in (settings or {}).items():
        conn.execute("INSERT INTO settings VALUES (?, ?)", (key, value))
    conn.execute("INSERT INTO windows VALUES (777, 42)")
    conn.commit()
    conn.close()


async def test_legacy_database_is_carried_over_without_losses(tmp_path):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path, settings={"price_1_30": 350, "price_group": 650})

    db = Database(path)
    await db.init()
    try:
        assert await db.schema_version() == LATEST_VERSION
        paid = await db.get_order(1)
        assert paid["price"] == 500 and paid["admin_payout_id"] == 1
        assert paid["series_pos"] is None and paid["client_token"] is None
        legacy_group = await db.get_order(2)
        assert legacy_group["kind"] == "group"
        assert legacy_group["cancelled_at"] is not None
        assert (await db.get_user_by_chat(777))["handle"] == "adm"
        assert (await db.get_user(2))["series_reset_min"] is None
        assert (await db.get_settings())["price_1_30"] == 350
        # акция «3=4» перенесена из настроек; денежная величина, которая
        # была у неё в прежней модели, снята миграцией v5
        promos = await db.list_promos()
        assert [p["name"] for p in promos] == ["3=4"]
        assert promos[0]["name_folded"] == "3=4"
        assert "price" not in promos[0].keys()
        assert (await db.payouts_for_user(2))[0]["amount"] == 50.0
        # прежние окна перенесены указателями и убираются при первом обращении
        window = await db.get_window(777)
        assert window["message_id"] == 42 and window["text"] == ""
    finally:
        await db.close()


async def test_disabled_group_promo_is_not_carried_over(tmp_path):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path, settings={"group_enabled": 0, "price_group": 650})
    db = Database(path)
    await db.init()
    try:
        assert await db.list_promos() == []
        assert "group_enabled" not in await db.get_settings()
    finally:
        await db.close()


async def test_migration_is_idempotent_across_restarts(tmp_path):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    for _ in range(3):
        db = Database(path)
        await db.init()
        await db.close()
    db = Database(path)
    await db.init()
    try:
        assert len(await db.list_promos()) == 1
        assert len(await db.export_orders()) == 2
        assert await db.schema_version() == LATEST_VERSION
    finally:
        await db.close()


async def test_fresh_database_starts_at_the_latest_version(db):
    assert await db.schema_version() == LATEST_VERSION
    assert await db.list_promos() == []
    tables = {r[0] for r in await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"audit_log", "outbox", "chat_windows", "fsm_state",
            "super_admins"} <= tables
    assert "windows" not in tables


async def test_crash_inside_a_migration_changes_nothing(tmp_path, monkeypatch):
    """Прерванная миграция откатывается целиком и повторяется при старте."""
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)

    original = migrations._apply_v2

    async def exploding(conn):
        await original(conn)
        raise RuntimeError("процесс убит посреди миграции")

    monkeypatch.setattr(migrations, "_apply_v2", exploding)
    monkeypatch.setattr(migrations, "MIGRATIONS", [
        migrations.MIGRATIONS[0],
        migrations.Migration(2, "надёжность", exploding, migrations._verify_v2),
    ])
    db = Database(path)
    with pytest.raises(RuntimeError, match="убит"):
        await db.init()
    version = (await db.fetchone("PRAGMA user_version"))[0]
    assert version == 1                                   # версия не поднялась
    tables = {r[0] for r in await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "audit_log" not in tables                      # и данных не появилось
    assert len(await db.export_orders()) == 2
    await db.close()

    monkeypatch.undo()
    db = Database(path)
    await db.init()                                       # повтор проходит начисто
    try:
        assert await db.schema_version() == LATEST_VERSION
        assert len(await db.export_orders()) == 2
    finally:
        await db.close()


async def test_post_condition_failure_aborts_the_migration(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)

    async def failing_verify(conn):
        raise RuntimeError("пост-условие не выполнено")

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        migrations.Migration(1, "базовая схема", migrations._apply_v1,
                             failing_verify, rebuilds_tables=True),
    ])
    db = Database(path)
    with pytest.raises(RuntimeError, match="пост-условие"):
        await db.init()
    assert (await db.fetchone("PRAGMA user_version"))[0] == 0
    rows = await db.fetchall("SELECT COUNT(*) FROM orders")
    assert rows[0][0] == 2
    await db.close()


async def test_database_newer_than_the_program_refuses_to_start(tmp_path):
    """Откатили релиз, не откатив схему — падаем громко, а не портим данные."""
    path = str(tmp_path / "future.db")
    db = Database(path)
    await db.init()
    async with db.write() as tx:
        await tx.execute(f"PRAGMA user_version = {LATEST_VERSION + 5}")
    await db.close()

    db = Database(path)
    with pytest.raises(RuntimeError, match="новее программы"):
        await db.init()
    await db.close()


async def test_duplicate_active_promo_names_stop_the_migration(tmp_path):
    """Дубликаты — повод позвать человека, а не молча что-то переписать."""
    path = str(tmp_path / "dupes.db")
    _legacy_db(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE promos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            price REAL NOT NULL, archived_at TEXT, created_at TEXT NOT NULL);
        INSERT INTO promos (name, price, created_at)
            VALUES ('День рождения', 500, '2026-01-01'),
                   ('день рождения', 600, '2026-01-02');
        ALTER TABLE users ADD COLUMN series_reset_min INTEGER;
        ALTER TABLE orders ADD COLUMN promo_id INTEGER;
        ALTER TABLE orders ADD COLUMN promo_name TEXT;
        ALTER TABLE orders ADD COLUMN series_pos INTEGER;
        CREATE TABLE bonuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
            amount REAL NOT NULL, comment TEXT NOT NULL DEFAULT '',
            payout_id INTEGER, cancelled_at TEXT, created_at TEXT NOT NULL);
        PRAGMA user_version = 1;
    """)
    conn.commit()
    conn.close()

    db = Database(path)
    with pytest.raises(RuntimeError, match="Одинаковые названия"):
        await db.init()
    assert (await db.fetchone("PRAGMA user_version"))[0] == 1
    await db.close()


async def test_pre_migration_snapshot_is_taken_before_any_change(tmp_path):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    seen = {}

    async def snapshot(pending):
        seen["versions"] = [m.version for m in pending]
        conn = sqlite3.connect(path)
        seen["tables_before"] = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()

    db = Database(path)
    await db.init(before_migration=snapshot)
    try:
        assert seen["versions"] == list(range(1, LATEST_VERSION + 1))
        assert "audit_log" not in seen["tables_before"]
    finally:
        await db.close()


async def test_no_pending_migrations_skips_the_snapshot(tmp_path):
    path = str(tmp_path / "fresh.db")
    db = Database(path)
    await db.init()
    await db.close()

    called = []
    db = Database(path)
    await db.init(before_migration=lambda pending: called.append(pending))
    try:
        assert called == []
        assert await pending_migrations(db.conn) == []
    finally:
        await db.close()


async def test_v3_adds_the_series_boundary_and_keeps_every_order(tmp_path):
    """Миграция v3 добавляет колонку, ничего не переписывая: у заказов
    прежней схемы граница остаётся пустой, и это правильный ответ —
    восстановить её нечем."""
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    conn = sqlite3.connect(path)
    before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    conn.close()

    db = Database(path)
    await db.init()
    try:
        assert await db.schema_version() == LATEST_VERSION
        rows = await db.fetchall("SELECT id, series_since FROM orders")
        assert len(rows) == before
        assert all(r["series_since"] is None for r in rows)
        columns = {r[1] for r in await db.fetchall("PRAGMA table_info(orders)")}
        assert "series_since" in columns
    finally:
        await db.close()


async def test_v3_is_idempotent_across_restarts(tmp_path):
    path = str(tmp_path / "again.db")
    for _ in range(3):
        db = Database(path)
        await db.init()
        assert await db.schema_version() == LATEST_VERSION
        await db.close()
    conn = sqlite3.connect(path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(orders)")]
    conn.close()
    assert columns.count("series_since") == 1


async def test_v4_rebuilds_orders_without_losing_anything(tmp_path):
    """Пересборка ради нового типа не имеет права потерять ни строки,
    ни ссылки на выплату, ни ключ идемпотентности, ни снимок акции."""
    path = str(tmp_path / "legacy.db")
    _legacy_db(path, settings={"price_group": 650})

    db = Database(path)
    await db.init()
    try:
        orders = await db.export_orders()
        assert [o["id"] for o in orders] == [1, 2]
        paid = await db.get_order(1)
        assert paid["price"] == 500 and paid["admin_payout_id"] == 1
        assert (await db.get_order(2))["kind"] == "group"   # прежний тип уцелел
        # уникальность ключа оформления пережила пересборку
        indexes = {r[0] for r in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='orders'")}
        assert {"idx_orders_client_token", "idx_orders_series",
                "idx_orders_admin_unpaid", "idx_orders_owner_unpaid",
                "idx_orders_admin_created", "idx_orders_created"} <= indexes
        assert not await db.fetchall(
            "SELECT name FROM sqlite_master WHERE name = 'orders_legacy'")
    finally:
        await db.close()


async def test_v4_accepts_the_free_session_and_still_refuses_nonsense(tmp_path):
    path = str(tmp_path / "kinds.db")
    db = Database(path)
    await db.init()
    try:
        async with db.write() as tx:
            await tx.execute(
                "INSERT INTO users (id, role, handle, name, password_hash,"
                " percent, created_at) VALUES (1, 'admin', 'a', 'A', 'x', 0, 'now')")
            await tx.execute(
                "INSERT INTO orders (admin_id, kind, base_price, discount_percent,"
                " price, admin_percent, admin_share, created_at)"
                " VALUES (1, 'free15', 0, 0, 0, 0, 0, 'now')")
        assert (await db.get_order(1))["kind"] == "free15"
        with pytest.raises(sqlite3.IntegrityError):
            async with db.write() as tx:
                await tx.execute(
                    "INSERT INTO orders (admin_id, kind, base_price,"
                    " discount_percent, price, admin_percent, admin_share,"
                    " created_at) VALUES (1, 'whatever', 0, 0, 0, 0, 0, 'now')")
    finally:
        await db.close()


async def test_v4_refuses_to_drop_a_column_it_does_not_know(tmp_path, monkeypatch):
    """Число строк потерю колонки не ловит — перенос идёт по именам.
    Незнакомая колонка обязана остановить обновление, а не исчезнуть."""
    path = str(tmp_path / "extra.db")
    _legacy_db(path)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:3])
    db = Database(path)
    await db.init()                                   # доводим базу до v3
    await db.close()
    monkeypatch.undo()

    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE orders ADD COLUMN hand_written TEXT")
    conn.commit()
    conn.close()

    db = Database(path)
    with pytest.raises(RuntimeError, match="потеряла бы колонки"):
        await db.init()
    assert (await db.fetchone("PRAGMA user_version"))[0] == 3
    columns = {r[1] for r in await db.fetchall("PRAGMA table_info(orders)")}
    assert "hand_written" in columns
    assert len(await db.export_orders()) == 2
    await db.close()


async def test_v4_removes_the_obsolete_pc_bonus_settings(tmp_path):
    """Понятие удалено целиком — строка, которую никто не читает, уходит."""
    path = str(tmp_path / "legacy.db")
    _legacy_db(path, settings={"pc_bonus_enabled": 0, "pc_bonus_points": 250,
                               "price_1_30": 350})
    db = Database(path)
    await db.init()
    try:
        stored = {r["key"] for r in await db.fetchall("SELECT key FROM settings")}
        assert "pc_bonus_enabled" not in stored and "pc_bonus_points" not in stored
        settings = await db.get_settings()
        assert "pc_bonus_enabled" not in settings
        assert settings["price_1_30"] == 350        # чужие настройки не тронуты
        assert settings["free15_enabled"] == 1
    finally:
        await db.close()


async def test_v5_drops_the_promo_money_and_keeps_the_promos(tmp_path):
    """Величина уходит из схемы; акции, их имена и связь с заказами — нет."""
    path = str(tmp_path / "legacy.db")
    _legacy_db(path, settings={"price_group": 650})
    db = Database(path)
    await db.init()
    try:
        columns = {r[1] for r in await db.fetchall("PRAGMA table_info(promos)")}
        assert columns == {"id", "name", "name_folded", "archived_at",
                           "created_at"}
        promos = await db.list_promos()
        assert [(p["id"], p["name"]) for p in promos] == [(1, "3=4")]
        assert not await db.fetchall(
            "SELECT name FROM sqlite_master WHERE name = 'promos_v5'")
        # связь заказов с акциями уцелела: переименование не увело её
        # на временную таблицу
        orders_sql = await db.fetchone(
            "SELECT sql FROM sqlite_master WHERE name = 'orders'")
        assert "REFERENCES promos(id)" in orders_sql[0]
        assert not await db.fetchall("PRAGMA foreign_key_check")
    finally:
        await db.close()


async def test_v5_does_not_reprice_a_single_order(tmp_path):
    """Исторические деньги неприкосновенны — включая заказы прежнего типа
    «акция», у которых цена когда-то бралась из самой акции."""
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    conn = sqlite3.connect(path)
    before = conn.execute(
        "SELECT id, kind, price, admin_share, owner_share FROM orders"
        " ORDER BY id").fetchall()
    conn.close()
    assert before, "в фикстуре должны быть заказы"

    db = Database(path)
    await db.init()
    try:
        after = [tuple(r) for r in await db.fetchall(
            "SELECT id, kind, price, admin_share, owner_share FROM orders"
            " ORDER BY id")]
        assert after == before
        # исторический заказ типа «акция» сохранил свою прежнюю цену
        assert (2, "group", 600.0) == after[1][:3]
    finally:
        await db.close()


async def test_v5_is_idempotent_across_restarts(tmp_path):
    path = str(tmp_path / "again.db")
    _legacy_db(path)
    for _ in range(3):
        db = Database(path)
        await db.init()
        assert await db.schema_version() == LATEST_VERSION
        await db.close()
    conn = sqlite3.connect(path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(promos)")]
    promos = conn.execute("SELECT COUNT(*) FROM promos").fetchone()[0]
    conn.close()
    assert "price" not in columns and columns.count("name") == 1
    assert promos == 1


async def test_v4_is_idempotent_across_restarts(tmp_path):
    path = str(tmp_path / "again.db")
    _legacy_db(path)
    for _ in range(3):
        db = Database(path)
        await db.init()
        assert await db.schema_version() == LATEST_VERSION
        await db.close()
    conn = sqlite3.connect(path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(orders)")]
    count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    conn.close()
    assert columns.count("kind") == 1 and count == 2
