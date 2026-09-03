"""Плановые задачи: напоминание дня выплат, копии базы, проверка копий, уборка.

Все сообщения задач уходят Записями через очередь: отчёт, не дошедший
до получателя, останется видимой строкой со статусом, а не пропадёт
в журнале сервера. Ни одна задача не трогает Окно чата напрямую —
его переставит работник очереди.
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import backup as bk
import notify
import reports
from config import Config
from db import Actor, Database
from fsm_storage import STATE_TTL_DAYS
from markup import h, join
from messaging import Messenger, file_payload
from roster import super_admin_ids
from utils import fmt_money, utcnow

log = logging.getLogger(__name__)

# Копия крупнее этого размера в Telegram не отправляется: лимит бота —
# 50 МБ, и упереться в него посреди ночи хуже, чем предупредить заранее
TELEGRAM_DOCUMENT_LIMIT = 45 * 1024 * 1024


def setup_scheduler(db: Database, ui: Messenger, config: Config,
                    storage=None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=str(config.tz))
    tz = str(config.tz)
    scheduler.add_job(payout_day_job, CronTrigger(day="10,25", hour=10, minute=0,
                                                  timezone=tz),
                      args=(db, ui, config), misfire_grace_time=3600)
    scheduler.add_job(backup_job, CronTrigger(hour=3, minute=0, timezone=tz),
                      args=(db, ui, config), misfire_grace_time=3600)
    scheduler.add_job(restore_check_job,
                      CronTrigger(day_of_week="mon", hour=4, minute=0, timezone=tz),
                      args=(db, ui, config), misfire_grace_time=3600)
    scheduler.add_job(housekeeping_job, CronTrigger(hour=4, minute=30, timezone=tz),
                      args=(db, ui, config, storage), misfire_grace_time=3600)
    return scheduler


# ------------------------------------------------------------- День выплат

PAYOUT_DAY_NOTE = ("Выплата проводится вручную; итоговая сумма может "
                   "измениться, пока период не закрыт выплатой")


async def _send_payout_reminders(db: Database, tx, rows, title: str,
                                 stamp: str) -> None:
    """Напоминание каждому получателю с ненулевым итогом периода.

    Нулевой итог напоминания не получает — обещание «к выплате» было бы
    ложным. У администратора итог due_sum включает бонусы.

    Раскладки по заказам здесь нет намеренно: она лежит за кнопкой меню
    и открывается в любую минуту, а в напоминании занимала экран, ради
    которого его никто не открывает. Остаётся то, за чем его читают:
    сколько и на какое число.
    """
    for row in rows:
        if row["due_sum"] <= 0:
            continue
        if not await db.chats_for_user(row["id"]):
            continue
        user = await db.get_user(row["id"])
        await notify.to_user(
            db, tx, user,
            join(h("<b>{}</b>", title),
                 h("К выплате: {}", fmt_money(row["due_sum"])),
                 PAYOUT_DAY_NOTE),
            kind="period_report", dedup=f"payout_day:{stamp}:{row['id']}")


async def payout_day_job(db: Database, ui: Messenger, config: Config) -> None:
    """10-го и 25-го в 10:00: напоминание супер-админам и получателям.

    Период закрывается выплатой, а не календарём (SPEC §4), поэтому
    заголовок говорит «к выплате на <дату>», а не «период закрыт»:
    обещать закрытие, которого не происходит, — прямой путь к спору
    о числах, которые не сойдутся. Приписка снизу добавляет, что выплату
    проводят руками и итог ещё может измениться.

    Ни одно из двух сообщений не несёт таблиц: день выплат — это повод
    заплатить, а не разобрать период построчно. Разбор открывается
    кнопкой меню и в этот день, и в любой другой.
    """
    today = datetime.now(config.tz)
    stamp = today.strftime("%Y-%m-%d")
    title = f"К выплате на {today.strftime('%d.%m.%Y')}"
    summary = await reports.vrheaven_summary(db, tables=False)
    summary.title = f"День выплат · {today.strftime('%d.%m.%Y')}"
    summary.add(PAYOUT_DAY_NOTE)
    async with db.write() as tx:
        await notify.to_super_admins(db, tx, config, summary.to_html(rich=False),
                                     kind="period_report",
                                     dedup=f"payout_day:{stamp}:vr")
        await _send_payout_reminders(
            db, tx, await db.owners_unpaid_summary(), title, stamp)
        await _send_payout_reminders(
            db, tx, await db.admins_unpaid_summary(), title, stamp)
    ui.wake()


# --------------------------------------------------------- Резервные копии

async def make_backup(db: Database, config: Config,
                      reason: str = bk.REASON_MANUAL) -> bk.BackupInfo:
    """Проверенная копия базы; ротация не трогает свежую копию."""
    return await bk.create_backup(config, reason)


async def backup_job(db: Database, ui: Messenger, config: Config) -> None:
    """Ежедневно в 03:00: копия базы, выгрузка за пределы сервера и
    документ каждому супер-админу.

    Документ — Запись: он остаётся в чате навсегда и никогда не удаляется
    ботом. Это единственная копия, переживающая потерю сервера, если
    внешняя выгрузка не настроена.
    """
    try:
        info = await bk.create_backup(config, bk.REASON_DAILY)
    except Exception as e:
        log.exception("Резервная копия не создана")
        await _alert(db, ui, config,
                     join("<b>Резервная копия не создана</b>", h("Причина: {}", e),
                          "Проверьте место на диске и права доступа"),
                     dedup=f"backup_fail:{utcnow().date()}")
        return
    offhost_error = await bk.push_offhost(config, info)
    async with db.write() as tx:
        if info.problems:
            # Копия снята и годна к восстановлению, но деньги в ней не
            # сходятся. Раньше такое расхождение отменяло саму копию и
            # тем самым прятало себя же; теперь оно приходит человеком
            # читаемой тревогой, а копия остаётся на месте
            await notify.to_super_admins(
                db, tx, config,
                join("<b>Деловые инварианты не сходятся</b>",
                     h("Копия: {}", info.name),
                     h("{}", "\n".join(info.problems[:5])),
                     "Копия снята и сохранена — расхождение разбирается по ней"),
                kind="alert", dedup=f"invariants:{info.name}")
        await db.audit(tx, Actor.system(), "backup.create", "setting", None,
                       after={"name": info.name, "size": info.size,
                              "offhost": "ok" if config.backup_offhost_cmd
                              and not offhost_error else "—"})
        if info.size <= TELEGRAM_DOCUMENT_LIMIT:
            await notify.to_super_admins(
                db, tx, config,
                h("<b>Резервная копия базы</b>\n{} · {} КБ",
                  info.created.strftime("%d.%m.%Y %H:%M UTC"), info.size // 1024),
                kind="backup", dedup=f"backup:{info.name}",
                document=file_payload(info.path, info.name))
        else:
            await notify.to_super_admins(
                db, tx, config,
                join("<b>Резервная копия базы</b>",
                     h("Файл {} КБ больше лимита Telegram — копия сохранена "
                       "только на сервере", info.size // 1024)),
                kind="alert", dedup=f"backup_big:{info.name}")
        if offhost_error:
            await notify.to_super_admins(
                db, tx, config,
                join("<b>Копия не выгружена за пределы сервера</b>",
                     h("Причина: {}", offhost_error)),
                kind="alert", dedup=f"offhost_fail:{info.name}")
    ui.wake()


async def restore_check_job(db: Database, ui: Messenger, config: Config) -> None:
    """Еженедельно: свежая копия распаковывается, открывается и
    проверяется инвариантами. «Копии есть» превращается в «копии рабочие»."""
    backups = bk.list_backups(config.backup_dir)
    if not backups:
        await _alert(db, ui, config,
                     join("<b>Проверка резервных копий</b>", "Копий нет вовсе"),
                     dedup=f"restore_none:{utcnow().date()}")
        return
    newest = backups[-1]
    problems, stats = await bk.verify_backup(newest.path)
    if problems:
        await _alert(db, ui, config,
                     join("<b>Резервная копия не прошла проверку</b>",
                          h("Файл: {}", newest.name),
                          h("{}", "\n".join(problems[:5]))),
                     dedup=f"restore_bad:{newest.name}")
        return
    async with db.write() as tx:
        await notify.to_super_admins(
            db, tx, config,
            join("<b>Проверка резервной копии: ОК</b>",
                 h("Файл: {}\nЗаписей: {}\nЗаказов: {}\nВыплат: {}\n"
                   "Версия схемы: {}", newest.name, stats["users"], stats["orders"],
                   stats["payouts"], stats["version"])),
            kind="backup", dedup=f"restore_ok:{newest.name}")
    ui.wake()


# ------------------------------------------------------------------ Уборка

async def housekeeping_job(db: Database, ui: Messenger, config: Config,
                           storage=None) -> None:
    """Ежедневная уборка: брошенные сценарии, доставленные Записи,
    окна чатов, за которыми никого нет."""
    if storage is not None:
        cutoff = (utcnow() - timedelta(days=STATE_TTL_DAYS)).isoformat(timespec="seconds")
        removed = await storage.cleanup(cutoff)
        if removed:
            log.info("Убрано брошенных сценариев: %s", removed)
    keep = set(await super_admin_ids(db, config))
    if config.superadmin_chat_id is not None:
        # Окно панели живёт в теме группы, а не в личном чате: без этой
        # строки уборка сочла бы его окном, за которым никого нет
        keep.add(config.superadmin_chat_id)
    async with db.write() as tx:
        purged = await db.purge_sent_records(
            tx, (utcnow() - timedelta(days=30)).isoformat(timespec="seconds"))
        windows = await db.prune_orphan_windows(
            tx, keep_chat_ids=keep,
            before_iso=(utcnow() - timedelta(days=30)).isoformat(timespec="seconds"))
    if purged or windows:
        log.info("Уборка: записей очереди %s, окон %s", purged, windows)


async def _alert(db: Database, ui: Messenger, config: Config, text: str, *,
                 dedup: str) -> None:
    async with db.write() as tx:
        await notify.to_super_admins(db, tx, config, text, kind="alert",
                                     dedup=dedup)
    ui.wake()
