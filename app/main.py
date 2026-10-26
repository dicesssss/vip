"""Entrypoint: ``python -m app.main``.

Start-up order (fail fast, never destructive):
 config -> logging -> volume check -> instance lock -> database (integrity, migrations)
 -> accounts -> monitor restore -> bot polling.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from . import __version__
from .config import ConfigError, load_config
from .logging_setup import add_file_logging, setup_logging

log = logging.getLogger("app")


async def run() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR\n{exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_level, cfg.secrets())
    log.info("Starting Username Monitor v%s", __version__)

    from . import storage
    from .db import Database, DatabaseError

    try:
        info = storage.verify_and_prepare(cfg)
    except storage.StorageError as exc:
        log.critical("PERSISTENT STORAGE PROBLEM\n%s", exc)
        return 3
    add_file_logging(cfg.logs_dir)

    lock = storage.InstanceLock(cfg.lock_path)
    try:
        await lock.acquire()
    except storage.StorageError as exc:
        log.critical("%s", exc)
        return 3

    db = Database(cfg.db_path)
    bot = None
    manager = None
    monitor = None
    exit_code = 0
    try:
        try:
            db.open()
            ok, messages = await db.integrity_check()
            if not ok:
                log.critical("DATABASE INTEGRITY CHECK FAILED: %s. The file was left untouched. "
                             "Restore a backup (docs/BACKUP_RESTORE.md).", "; ".join(messages[:5]))
                return 4
            version = await db.migrate(cfg.backups_dir)
            log.info("Database ready (schema v%d, journal=%s)", version, db.journal_mode)
        except DatabaseError as exc:
            log.critical("DATABASE ERROR: %s", exc)
            return 4

        # Heavy imports after the cheap validation succeeded.
        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from aiogram.fsm.storage.memory import MemoryStorage

        from .accounts import AccountManager, default_client_factory
        from .bot.context import AppContext
        from .bot.keyboards import availability_alert
        from .bot.router import build_router
        from .checker import UsernameChecker
        from .monitor import Monitor
        from .notifier import Notifier
        from .repo import Repo

        repo = Repo(db)
        bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

        async def send_admin(text: str, markup) -> None:
            await bot.send_message(cfg.admin_id, text, reply_markup=markup, disable_web_page_preview=True)

        notifier = Notifier(send_admin, repo, tz=cfg.tz())
        manager = AccountManager(sessions_dir=cfg.sessions_dir, pending_dir=cfg.pending_dir,
                                 client_factory=default_client_factory(cfg.api_id, cfg.api_hash),
                                 repo=repo, notifier=notifier)
        monitor = Monitor(repo=repo, manager=manager, checker=UsernameChecker(), notifier=notifier, tz=cfg.tz(),
                          min_interval=cfg.min_interval, max_interval=cfg.max_interval,
                          availability_markup=availability_alert)
        ctx = AppContext(cfg=cfg, db=db, repo=repo, manager=manager, monitor=monitor, notifier=notifier,
                         bot=bot, volume_id=info.volume_id)

        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(build_router(ctx))

        # Fail clearly on a bad bot token before starting anything else.
        try:
            me = await bot.get_me()
        except Exception as exc:  # noqa: BLE001
            log.critical("BOT_TOKEN rejected or Telegram unreachable (%s). Check the BOT_TOKEN variable.",
                         type(exc).__name__)
            return 5
        log.info("Control bot connected as @%s", me.username)

        await manager.load_and_start()
        resumed = await monitor.resume_from_db()
        await notifier.send(
            f"🟢 <b>Username Monitor started</b> (v{__version__})\n"
            f"Sessions restored: {len(manager.runners)} · monitoring "
            f"{'resumed' if resumed else 'not active'} · volume <code>{info.volume_id}</code>\n"
            "Use /menu for the dashboard.")

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)

        polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False,
                                                       allowed_updates=["message", "callback_query"]),
                                      name="polling")
        stopper = asyncio.create_task(stop_event.wait(), name="stop-wait")
        done, _ = await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if polling in done and not stop_event.is_set():
            exc = polling.exception()
            log.critical("polling ended unexpectedly: %s", type(exc).__name__ if exc else "no error")
            exit_code = 1
        log.info("Shutting down…")
        with contextlib.suppress(Exception):
            await dp.stop_polling()
        for task in (polling, stopper):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return exit_code
    finally:
        # Graceful shutdown: tasks first, then connections, then the database, then the lock.
        if monitor is not None:
            with contextlib.suppress(Exception):
                await monitor.shutdown()
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.stop_all()
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.session.close()
        with contextlib.suppress(Exception):
            await db.close()
        lock.release()
        log.info("Stopped cleanly")


def main() -> None:
    try:
        code = asyncio.run(run())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
