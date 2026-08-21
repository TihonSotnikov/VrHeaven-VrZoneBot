"""Резервные копии: имя, атомарность, проверка, ротация, восстановимость."""

import gzip
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from helpers import create_admin, create_owner, make_order

import backup as bk


def _names(config) -> list[str]:
    return sorted(os.listdir(config.backup_dir))


async def _seed(db):
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=790, admin_share=50)


async def test_backup_is_timestamped_verified_and_compressed(db, config):
    await _seed(db)
    info = await bk.create_backup(config, bk.REASON_DAILY)
    assert info.name.startswith("adminbot-") and info.name.endswith("-daily.db.gz")
    assert bk.parse_name(info.name)[1] == "daily"
    with gzip.open(info.path, "rb") as handle:
        assert handle.read(16).startswith(b"SQLite format 3")


async def test_second_backup_of_the_day_does_not_overwrite_the_first(db, config):
    """Именно на этом раньше терялась единственная копия дня."""
    await _seed(db)
    first = await bk.create_backup(config, bk.REASON_BOOT)
    second = await bk.create_backup(config, bk.REASON_DAILY)
    assert first.name != second.name
    assert os.path.exists(first.path) and os.path.exists(second.path)


async def test_backup_contains_the_data(db, config):
    await _seed(db)
    info = await bk.create_backup(config, bk.REASON_MANUAL)
    problems, stats = await bk.verify_backup(info.path, str(config.tz))
    assert problems == []
    assert stats["users"] == 2 and stats["orders"] == 1
    assert stats["version"] >= 2


async def test_broken_snapshot_is_not_published(db, config, monkeypatch):
    """Копия, не прошедшая проверку, не появляется в каталоге вовсе."""
    await _seed(db)
    monkeypatch.setattr(bk, "_verify_file", lambda path, tz: ["сломано"])
    with pytest.raises(RuntimeError, match="не прошла проверку"):
        await bk.create_backup(config, bk.REASON_DAILY)
    assert bk.list_backups(config.backup_dir) == []
    assert [n for n in _names(config) if n.startswith(".tmp")] == []


async def test_failed_backup_never_destroys_an_existing_one(db, config, monkeypatch):
    await _seed(db)
    good = await bk.create_backup(config, bk.REASON_DAILY)
    monkeypatch.setattr(bk, "_snapshot", lambda src, dst: (_ for _ in ()).throw(
        sqlite3.OperationalError("диск переполнен")))
    with pytest.raises(sqlite3.OperationalError):
        await bk.create_backup(config, bk.REASON_DAILY)
    assert os.path.exists(good.path)


def _touch(config, stamp: datetime, reason: str) -> str:
    os.makedirs(config.backup_dir, exist_ok=True)
    name = f"adminbot-{stamp.strftime('%Y%m%dT%H%M%SZ')}-{reason}.db.gz"
    path = os.path.join(config.backup_dir, name)
    open(path, "wb").close()
    return name


async def test_rotation_keeps_daily_weekly_monthly(db, config):
    now = datetime.now(UTC)
    tuned = replace(config, backup_keep_days=7, backup_keep_weeks=4,
                    backup_keep_months=3)
    recent = [_touch(tuned, now - timedelta(days=day), "daily") for day in range(1, 7)]
    weekly = [_touch(tuned, now - timedelta(weeks=week), "daily")
              for week in range(2, 5)]
    ancient = _touch(tuned, now - timedelta(days=400), "daily")
    await _seed(db)
    fresh = await bk.create_backup(tuned, bk.REASON_DAILY)

    left = set(_names(tuned))
    assert os.path.basename(fresh.path) in left
    assert set(recent) <= left
    assert ancient not in left
    assert any(name in left for name in weekly)


async def test_pre_migration_snapshot_is_exempt_from_rotation(db, config):
    now = datetime.now(UTC)
    tuned = replace(config, backup_keep_days=1, backup_keep_weeks=1,
                    backup_keep_months=1)
    protected = _touch(tuned, now - timedelta(days=40), bk.REASON_PRE_MIGRATION)
    old_daily = _touch(tuned, now - timedelta(days=40), "daily")
    await _seed(db)
    await bk.create_backup(tuned, bk.REASON_DAILY)
    left = set(_names(tuned))
    assert protected in left
    assert old_daily not in left


async def test_offhost_command_runs_and_reports_failure(db, config):
    await _seed(db)
    target = os.path.join(config.data_dir, "offhost")
    os.makedirs(target, exist_ok=True)
    good = replace(config, backup_offhost_cmd=f"cp {{path}} {target}/{{name}}")
    info = await bk.create_backup(good, bk.REASON_DAILY)
    assert await bk.push_offhost(good, info) is None
    assert os.path.exists(os.path.join(target, info.name))

    bad = replace(config, backup_offhost_cmd="false")
    assert await bk.push_offhost(bad, info) is not None
    assert await bk.push_offhost(config, info) is None      # не настроено — пропуск


async def test_invariant_failures_are_visible_in_verification(db, config):
    await _seed(db)
    info = await bk.create_backup(config, bk.REASON_MANUAL)
    # ломаем распакованную копию так, как ломается настоящая порча данных
    raw = os.path.join(config.data_dir, "broken.db")
    with gzip.open(info.path, "rb") as src, open(raw, "wb") as dst:
        dst.write(src.read())
    conn = sqlite3.connect(raw)
    conn.execute("UPDATE orders SET owner_share = owner_share + 1")
    conn.commit()
    conn.close()
    problems = bk._verify_file(raw, str(config.tz))
    assert problems and "доля владельца" in problems[0]
