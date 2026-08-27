"""Точка входа: запуск бота в режиме long polling.

Порядок запуска подобран так, чтобы каждый шаг был обратим:

1. Единственность процесса — блокировка каталога данных. Второй экземпляр
   на одной базе означал бы гонку за апдейтами и за файлом; он выходит
   сразу и с внятным сообщением.
2. Копия базы **до** открытия базы. Именно этот снимок нужен, если
   обновление принесло миграцию, которая всё испортила.
3. Открытие базы и миграции; перед первой миграцией снимается отдельная
   неизменяемая копия.
4. Всё остальное.

Останов симметричен: сначала перестаём принимать апдейты, потом дожидаемся
работы в полёте и только затем закрываем базу.
"""

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand

import backup as bk
import errors
from config import Config, load_config
from db import Database
from delivery import InboundThrottle, ThrottleMiddleware
from fsm_storage import SQLiteStorage
from handlers import common, staff, vrheaven
from logs import ContextMiddleware, setup_logging
from messaging import Messenger
from outbox import OutboxWorker
from scheduler import setup_scheduler

log = logging.getLogger(__name__)


class SingleInstance:
    """Блокировка каталога данных: два процесса на одной базе — это
    драка за getUpdates и за файл одновременно."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, ".bot.lock")
        self._handle = None

    def acquire(self) -> None:
        import fcntl
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._handle = open(self.path, "w")
        try:
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._handle.close()
            raise RuntimeError(
                f"Бот уже запущен на этой базе данных ({self.path}). "
                "Второй экземпляр не запускается"
            ) from e
        self._handle.write(str(os.getpid()))
        self._handle.flush()

    def release(self) -> None:
        if self._handle:
            self._handle.close()
            self._handle = None


def _log_discrepancies(info: bk.BackupInfo) -> None:
    """Деловые расхождения копию не отменяют, но и незамеченными не
    остаются: в журнале они есть с первой секунды, тревогой супер-админам
    уходят с ближайшей суточной копией."""
    if info.problems:
        log.error("Копия %s снята, но деловые инварианты не сходятся:\n%s",
                  info.name, "\n".join(f" - {p}" for p in info.problems[:5]))


async def boot_backup(config: Config) -> None:
    """Копия перед открытием базы: снимок того состояния, к которому
    можно вернуться, если обновление окажется неудачным."""
    if not os.path.exists(config.db_path):
        return
    try:
        info = await bk.create_backup(config, bk.REASON_BOOT)
        log.info("Копия перед запуском: %s", info.name)
        _log_discrepancies(info)
    except Exception:
        # Бэкап не должен мешать боту стартовать, но молчать о нём нельзя
        log.exception("Не удалось снять копию базы перед запуском")


async def run() -> None:
    config = load_config()
    setup_logging(json_output=config.log_json, level=config.log_level)

    lock = SingleInstance(config.data_dir)
    lock.acquire()

    await boot_backup(config)

    db = Database(config.db_path)

    async def before_migration(pending) -> None:
        log.warning("Ожидают применения миграции: %s",
                    ", ".join(f"v{m.version} {m.name}" for m in pending))
        if os.path.exists(config.db_path):
            # Снимок — единственная точка отката схемы, поэтому неудача
            # здесь миграцию отменяет. Отменяет её непригодный файл, а не
            # спор о деньгах: тот уезжает в журнал и миграции не мешает
            info = await bk.create_backup(config, bk.REASON_PRE_MIGRATION)
            log.warning("Копия перед миграцией: %s", info.name)
            _log_discrepancies(info)

    await db.init(before_migration=before_migration)
    log.info("Схема базы: версия %s", await db.schema_version())

    bot = Bot(token=config.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(ThrottleMiddleware())

    ui = Messenger(bot, db)
    worker = OutboxWorker(bot, db, ui, config)
    ui.on_enqueue = worker.wake

    storage = SQLiteStorage(db)
    # Долговечное состояние без изоляции событий превратило бы
    # update_data в гонку: два апдейта одного чата затирали бы друг друга
    dp = Dispatcher(storage=storage, events_isolation=SimpleEventIsolation())
    dp["config"] = config
    dp["db"] = db
    dp["ui"] = ui
    dp.update.outer_middleware(ContextMiddleware())
    dp.update.outer_middleware(InboundThrottle())
    errors.setup_error_handler(dp)

    # Порядок важен: сначала чужие чаты, затем панель VR Heaven
    # (фильтр по списку супер-админов), затем кабинеты команды
    dp.include_router(common.guard_router)
    dp.include_router(vrheaven.router)
    dp.include_router(staff.router)

    scheduler = setup_scheduler(db, ui, config, storage)
    scheduler.start()
    worker.start()

    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    log.info("Бот запущен: база %s, копии %s", config.db_path, config.backup_dir)
    try:
        await dp.start_polling(bot, handle_signals=True)
    except TelegramConflictError:
        # Тот же токен уже опрашивает Telegram из другого места:
        # тихо крутиться в цикле здесь опаснее, чем упасть заметно
        log.critical("Тот же токен используется другим процессом — выходим")
        raise
    finally:
        log.info("Останов: доставляем незавершённое и закрываем базу")
        scheduler.shutdown(wait=False)
        await worker.stop()
        await bot.session.close()
        await db.close()
        lock.release()


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except RuntimeError as e:
        print(f"Запуск невозможен: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
