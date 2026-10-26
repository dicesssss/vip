"""Dashboard, status, logs and notification preferences."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import __version__, storage
from ..logging_setup import tail_log
from ..timeutil import utcnow
from . import keyboards as kb
from . import views
from .common import monitor_account_label, show
from .context import AppContext
from .keyboards import Nav

log = logging.getLogger(__name__)


def register(router: Router, ctx: AppContext) -> None:
    tz = ctx.cfg.tz()

    async def home(target, state: FSMContext) -> None:
        await state.clear()
        await ctx.cancel_login()
        name = target.from_user.first_name if target.from_user else None
        await show(target, views.dashboard(name), kb.main_menu())

    @router.message(CommandStart())
    @router.message(Command("menu", "help"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await home(message, state)

    @router.callback_query(Nav.filter(F.a.in_({"home", "cancel"})))
    async def cb_home(cb: CallbackQuery, state: FSMContext) -> None:
        await home(cb, state)

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        await show(message, await _status_text(), kb.refresh("status"))

    @router.callback_query(Nav.filter(F.a == "status"))
    async def cb_status(cb: CallbackQuery) -> None:
        await show(cb, await _status_text(), kb.refresh("status"))

    async def _status_text() -> str:
        ok, _ = await ctx.db.integrity_check(quick=True)
        target = await ctx.repo.get_current_target()
        runner_label = await monitor_account_label(ctx)
        return views.status(
            version=__version__,
            uptime_seconds=(utcnow() - ctx.started_at).total_seconds(),
            counts=ctx.manager.counts(),
            target=target,
            monitor_status=ctx.monitor.status.value,
            interval=await ctx.repo.get_int("interval_seconds", 30),
            monitor_account=runner_label,
            last_check=ctx.monitor.last_check_wall or (target or {}).get("last_check_at"),
            floods=await ctx.repo.recent_events("flood_wait", 3),
            errors=await ctx.repo.recent_events("error", 3),
            storage=storage.health(ctx.cfg),
            db_ok=ok,
            volume_id=ctx.volume_id,
            tz=tz,
        )

    @router.callback_query(Nav.filter(F.a == "logs"))
    async def cb_logs(cb: CallbackQuery) -> None:
        await show(cb, views.logs(tail_log(ctx.cfg.log_file)), kb.refresh("logs"))

    @router.message(Command("logs"))
    async def cmd_logs(message: Message) -> None:
        await show(message, views.logs(tail_log(ctx.cfg.log_file)), kb.refresh("logs"))

    async def _flood_view(target) -> None:
        text = views.flood_settings(
            await ctx.repo.get_bool("notify_flood"), await ctx.repo.get_bool("notify_errors"),
            await ctx.repo.recent_events("flood_wait", 8), tz)
        await show(target, text, kb.flood_settings(
            await ctx.repo.get_bool("notify_flood"), await ctx.repo.get_bool("notify_errors")))

    @router.callback_query(Nav.filter(F.a == "flood"))
    async def cb_flood(cb: CallbackQuery) -> None:
        await _flood_view(cb)

    @router.callback_query(Nav.filter(F.a == "tog_flood"))
    async def cb_tog_flood(cb: CallbackQuery) -> None:
        await ctx.repo.set_bool("notify_flood", not await ctx.repo.get_bool("notify_flood"))
        await _flood_view(cb)

    @router.callback_query(Nav.filter(F.a == "tog_err"))
    async def cb_tog_err(cb: CallbackQuery) -> None:
        await ctx.repo.set_bool("notify_errors", not await ctx.repo.get_bool("notify_errors"))
        await _flood_view(cb)
