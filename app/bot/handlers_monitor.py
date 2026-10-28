"""/snipe, target card, intervals, monitoring controls and the username-change flow."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..claim import ClaimOutcome, change_username, describe_account
from ..accounts import AccountUnavailable, RateLimited
from ..timeutil import fmt_time
from ..validators import ValidationError, normalize_username, parse_interval
from . import keyboards as kb
from . import views
from .common import monitor_account_label, show
from .context import AppContext
from .keyboards import Nav

log = logging.getLogger(__name__)


class MonitorInput(StatesGroup):
    username = State()
    interval = State()


def register(router: Router, ctx: AppContext) -> None:
    tz = ctx.cfg.tz()

    async def _target_view(target) -> None:
        tgt = await ctx.repo.get_current_target()
        text = views.target_card(
            tgt, monitor_status=ctx.monitor.status.value,
            interval=await ctx.repo.get_int("interval_seconds", 30),
            account_label=await monitor_account_label(ctx), tz=tz)
        # Removed manual start button logic, keeping clean views
        await show(target, text, kb.home_only() if not tgt else kb.refresh("target"))

    async def _register_target(target, raw: str) -> None:
        try:
            username = normalize_username(raw)
        except ValidationError as exc:
            await show(target, f"❌ {exc}", kb.home_only())
            return
        row = await ctx.repo.get_or_create_target(username)
        await ctx.repo.set_current_target(row["id"])
        warn = ""
        if not ctx.manager.all():
            warn = "\n\n⚠️ No account is connected yet. Add one first; monitoring waits for it."
        
        # AUTOMATICALLY START MONITORING IMMEDIATELY UPON /SNIPE
        started = await ctx.monitor.restart() if ctx.monitor.running else await ctx.monitor.start()
        ctx.monitor.wake()
        
        prefix = f"✅ Registered and automatically started monitoring <b>@{username}</b>!{warn}\n\n"
        tgt = await ctx.repo.get_target(row["id"])
        text = prefix + views.target_card(
            tgt, monitor_status=ctx.monitor.status.value,
            interval=await ctx.repo.get_int("interval_seconds", 30),
            account_label=await monitor_account_label(ctx), tz=tz)
        await show(target, text, kb.home_only() if not tgt else kb.refresh("target"))

    # ------------------------------------------------------------------ /snipe
    @router.message(Command("snipe"))
    async def cmd_snipe(message: Message, command: CommandObject, state: FSMContext) -> None:
        await state.clear()
        if not command.args:
            await message.answer("Usage: <code>/snipe @username</code>", reply_markup=kb.home_only())
            return
        await _register_target(message, command.args.split()[0])

    @router.callback_query(Nav.filter(F.a == "mon"))
    async def cb_mon(cb: CallbackQuery, state: FSMContext) -> None:
        if await ctx.repo.get_current_target() is not None:
            await _target_view(cb)
            return
        await state.set_state(MonitorInput.username)
        await show(cb, "🎯 Send the username to monitor, e.g. <code>@example_name</code>", kb.cancel())

    @router.message(MonitorInput.username, F.text)
    async def msg_username(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _register_target(message, message.text or "")

    @router.callback_query(Nav.filter(F.a == "target"))
    async def cb_target(cb: CallbackQuery) -> None:
        await _target_view(cb)

    @router.message(Command("target"))
    async def cmd_target(message: Message) -> None:
        await _target_view(message)

    # ------------------------------------------------------------------ controls (Manual start/restart removed)
    @router.callback_query(Nav.filter(F.a == "stop"))
    async def cb_stop(cb: CallbackQuery) -> None:
        await ctx.monitor.stop()
        await cb.answer("Monitoring stopped.")
        await _target_view(cb) if await ctx.repo.get_current_target() else await show(
            cb, "⏹ Monitoring is stopped. No target is set.", kb.home_only())

    @router.callback_query(Nav.filter(F.a == "hist"))
    async def cb_hist(cb: CallbackQuery) -> None:
        tgt = await ctx.repo.get_current_target()
        if tgt is None:
            await show(cb, "No target set.", kb.home_only())
            return
        rows = await ctx.repo.list_history(tgt["id"], 15)
        await show(cb, views.history(rows, tgt["username"], tz), kb.refresh("hist"))

    # ------------------------------------------------------------------ settings & intervals
    async def _settings_view(target) -> None:
        interval = await ctx.repo.get_int("interval_seconds", 30)
        raw = await ctx.repo.get_setting("monitor_account_id")
        selected = int(raw) if raw.isdigit() else None
        await show(target,
                   views.settings(interval, ctx.cfg.min_interval, ctx.cfg.max_interval, await monitor_account_label(ctx)),
                   kb.settings(interval, [r.view() for r in ctx.manager.all()], selected))

    @router.callback_query(Nav.filter(F.a == "set"))
    async def cb_settings(cb: CallbackQuery) -> None:
        await _settings_view(cb)

    @router.callback_query(Nav.filter(F.a == "iv"))
    async def cb_interval(cb: CallbackQuery, callback_data: Nav) -> None:
        try:
            value = parse_interval(callback_data.v, ctx.cfg.min_interval, ctx.cfg.max_interval)
        except ValidationError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        await ctx.repo.set_setting("interval_seconds", str(value))
        ctx.monitor.wake()
        await _settings_view(cb)

    @router.callback_query(Nav.filter(F.a == "iv_custom"))
    async def cb_interval_custom(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(MonitorInput.interval)
        await show(cb, f"✍️ Send the interval in seconds ({ctx.cfg.min_interval}–{ctx.cfg.max_interval}).", kb.cancel())

    @router.message(MonitorInput.interval, F.text)
    async def msg_interval(message: Message, state: FSMContext) -> None:
        try:
            value = parse_interval(message.text or "", ctx.cfg.min_interval, ctx.cfg.max_interval)
        except ValidationError as exc:
            await message.answer(f"❌ {exc}", reply_markup=kb.cancel())
            return
        await state.clear()
        await ctx.repo.set_setting("interval_seconds", str(value))
        ctx.monitor.wake()
        await _settings_view(message)

    @router.callback_query(Nav.filter(F.a == "usemon"))
    async def cb_use_for_monitoring(cb: CallbackQuery, callback_data: Nav) -> None:
        runner = ctx.manager.get(int(callback_data.v))
        if runner is None or runner.permanent:
            await cb.answer("That account cannot be used.", show_alert=True)
            return
        await ctx.repo.set_setting("monitor_account_id", str(runner.account_id))
        ctx.monitor.wake()
        await _settings_view(cb)
