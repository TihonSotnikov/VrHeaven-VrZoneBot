"""Резервные копии: снять, проверить, сохранить, проверить восстановимость.

Пять требований, каждое закрывает конкретный способ потерять данные:

1. Имя копии содержит момент до секунды — вторая копия за сутки больше не
   затирает первую.
2. Копия пишется во временный файл и только затем переименовывается на
   место: неудачная копия не может уничтожить удачную.
3. Копия проверяется сразу после создания (целостность файла, связи
   таблиц, деловые инварианты). Непрошедшая проверку копия не публикуется.
4. Перед каждой миграцией снимается отдельная копия, которая не участвует
   в ротации 90 дней.
5. Восстановимость проверяется расписанием: копия открывается, по ней
   гоняются инварианты, результат приходит супер-админам Записью.

Копия за пределы сервера уходит двумя путями: документом супер-админам
(остаётся в чате навсегда) и, если задан BACKUP_OFFHOST_CMD, внешней
командой — например rclone или scp на другую машину.
"""

import asyncio
import gzip
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from config import Config
from invariants import check_database

log = logging.getLogger(__name__)

PREFIX = "adminbot-"
SUFFIX = ".db.gz"
REASON_BOOT = "boot"
REASON_DAILY = "daily"
REASON_PRE_MIGRATION = "pre-migration"
REASON_MANUAL = "manual"

# Копия перед миграцией — единственная точка отката схемы, поэтому она
# не участвует в обычной ротации
PRE_MIGRATION_KEEP_DAYS = 90

_NAME_RE = re.compile(
    rf"^{re.escape(PREFIX)}(\d{{8}}T\d{{6}}Z)-([a-z-]+){re.escape(SUFFIX)}$"
)


@dataclass(frozen=True)
class BackupInfo:
    name: str
    path: str
    created: datetime
    reason: str
    size: int


def parse_name(name: str) -> tuple[datetime, str] | None:
    match = _NAME_RE.match(name)
    if not match:
        return None
    created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
        tzinfo=UTC)
    return created, match.group(2)


def list_backups(backup_dir: str) -> list[BackupInfo]:
    if not os.path.isdir(backup_dir):
        return []
    found = []
    for name in os.listdir(backup_dir):
        parsed = parse_name(name)
        if not parsed:
            continue
        path = os.path.join(backup_dir, name)
        found.append(BackupInfo(name=name, path=path, created=parsed[0],
                                reason=parsed[1], size=os.path.getsize(path)))
    return sorted(found, key=lambda b: b.created)


def _snapshot(db_path: str, target: str) -> None:
    """VACUUM INTO — согласованный снимок без остановки бота."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (target,))
    finally:
        conn.close()


def _verify_file(path: str) -> list[str]:
    problems = check_database(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for required in ("users", "orders", "payouts"):
            if required not in tables:
                problems.append(f"в копии нет таблицы {required}")
    finally:
        conn.close()
    return problems


def _create_sync(db_path: str, backup_dir: str, reason: str) -> BackupInfo:
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = os.path.join(backup_dir, f"{PREFIX}{stamp}-{reason}{SUFFIX}")
    work = os.path.join(backup_dir, f".tmp-{uuid.uuid4().hex}")
    raw, packed = work + ".db", work + SUFFIX
    try:
        _snapshot(db_path, raw)
        problems = _verify_file(raw)
        if problems:
            raise RuntimeError("копия не прошла проверку: " + "; ".join(problems[:5]))
        with open(raw, "rb") as src, gzip.open(packed, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        # Переименование атомарно: файл на месте появляется целиком
        os.replace(packed, final)
    finally:
        for path in (raw, packed):
            if os.path.exists(path):
                os.remove(path)
    return BackupInfo(name=os.path.basename(final), path=final,
                      created=datetime.now(UTC), reason=reason,
                      size=os.path.getsize(final))


async def create_backup(config: Config, reason: str, *,
                        db_path: str | None = None) -> BackupInfo:
    """Снимает проверенную копию базы. Ничего не удаляет до успеха."""
    info = await asyncio.to_thread(
        _create_sync, db_path or config.db_path, config.backup_dir, reason,
    )
    log.info("Резервная копия: %s (%.1f КБ)", info.name, info.size / 1024)
    await asyncio.to_thread(rotate, config)
    return info


def rotate(config: Config) -> list[str]:
    """Ротация «дед — отец — сын»: суточные, недельные, месячные.

    Плоское окно в N последних копий теряет данные, если порча замечена
    позже: за месяц копии успевают вытеснить друг друга. Здесь остаются
    все копии за последние дни, по одной на неделю и по одной на месяц.
    """
    backups = list_backups(config.backup_dir)
    if not backups:
        return []
    now = datetime.now(UTC)
    keep: set[str] = set()
    weeks: dict[tuple[int, int], str] = {}
    months: dict[tuple[int, int], str] = {}
    for info in backups:
        age = now - info.created
        if info.reason == REASON_PRE_MIGRATION:
            if age <= timedelta(days=PRE_MIGRATION_KEEP_DAYS):
                keep.add(info.name)
            continue
        if age <= timedelta(days=config.backup_keep_days):
            keep.add(info.name)
        if age <= timedelta(weeks=config.backup_keep_weeks):
            iso = info.created.isocalendar()
            weeks[(iso.year, iso.week)] = info.name    # последняя за неделю
        if age <= timedelta(days=31 * config.backup_keep_months):
            months[(info.created.year, info.created.month)] = info.name
    for names in (weeks, months):
        keep.update(names.values())
    keep.add(backups[-1].name)                          # свежую не трогаем никогда
    removed = []
    for info in backups:
        if info.name in keep:
            continue
        try:
            os.remove(info.path)
            removed.append(info.name)
        except OSError as e:
            log.warning("Не удалось удалить копию %s: %s", info.name, e)
    if removed:
        log.info("Ротация копий удалила %s файлов", len(removed))
    return removed


def _verify_backup_sync(path: str) -> tuple[list[str], dict]:
    with tempfile.TemporaryDirectory() as tmp:
        restored = os.path.join(tmp, "restored.db")
        with gzip.open(path, "rb") as src, open(restored, "wb") as dst:
            shutil.copyfileobj(src, dst)
        problems = _verify_file(restored)
        conn = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
        try:
            stats = {
                "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                "payouts": conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0],
                "version": conn.execute("PRAGMA user_version").fetchone()[0],
            }
        finally:
            conn.close()
    return problems, stats


async def verify_backup(path: str) -> tuple[list[str], dict]:
    """Проверка восстановимости: копия распаковывается, открывается и
    проверяется инвариантами — «копии есть» превращается в «копии рабочие»."""
    return await asyncio.to_thread(_verify_backup_sync, path)


async def push_offhost(config: Config, info: BackupInfo) -> str | None:
    """Отправляет копию за пределы сервера внешней командой.

    Команда задаётся BACKUP_OFFHOST_CMD и получает {path} и {name}.
    Не задана — шаг пропускается; копия всё равно уходит документом
    супер-админам. Возвращает текст ошибки или None.
    """
    if not config.backup_offhost_cmd:
        return None
    command = config.backup_offhost_cmd.format(path=info.path, name=info.name)
    process = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        message = stderr.decode(errors="replace").strip()[:300]
        log.error("Выгрузка копии за пределы сервера не удалась: %s", message)
        return message or f"код возврата {process.returncode}"
    log.info("Копия выгружена за пределы сервера: %s", info.name)
    return None
