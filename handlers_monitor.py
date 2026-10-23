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
        await show(target, text, kb.target_card(ctx.monitor.running) if tgt else kb.home_only())

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
        started = await ctx.monitor.restart() if ctx.monitor.running else await ctx.monitor.start()
        ctx.monitor.wake()
        prefix = f"✅ Registered <b>@{username}</b> and {'started' if started else 'could not start'} monitoring.{warn}\n\n"
        tgt = await ctx.repo.get_target(row["id"])
        text = prefix + views.target_card(
            tgt, monitor_status=ctx.monitor.status.value,
            interval=await ctx.repo.get_int("interval_seconds", 30),
            account_label=await monitor_account_label(ctx), tz=tz)
        await show(target, text, kb.target_card(ctx.monitor.running))

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

    # ------------------------------------------------------------------ controls
    @router.callback_query(Nav.filter(F.a == "start"))
    async def cb_start(cb: CallbackQuery) -> None:
        if not await ctx.monitor.start():
            await cb.answer("Already running, or no target set.", show_alert=True)
        await _target_view(cb)

    @router.callback_query(Nav.filter(F.a == "stop"))
    async def cb_stop(cb: CallbackQuery) -> None:
        await ctx.monitor.stop()
        await cb.answer("Monitoring stopped.")
        await _target_view(cb) if await ctx.repo.get_current_target() else await show(
            cb, "⏹ Monitoring is stopped. No target is set.", kb.home_only())

    @router.callback_query(Nav.filter(F.a == "restart"))
    async def cb_restart(cb: CallbackQuery) -> None:
        if not await ctx.monitor.restart():
            await cb.answer("No target set.", show_alert=True)
        await _target_view(cb)

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

    # ------------------------------------------------------------------ username change (manual, confirmed)
    @router.callback_query(Nav.filter(F.a == "claim_pick"))
    async def cb_claim_pick(cb: CallbackQuery, callback_data: Nav) -> None:
        tgt = await ctx.repo.get_current_target()
        if tgt is None:
            await show(cb, "Set a target first with /snipe @username.", kb.home_only())
            return
        if callback_data.v:
            await _claim_confirm(cb, int(callback_data.v), tgt["username"])
            return
        views_ = [r.view() for r in ctx.manager.all()]
        if not views_:
            await show(cb, "No accounts yet.", kb.home_only())
            return
        await show(cb, f"✏️ Which account should get <b>@{tgt['username']}</b>?\n"
                       "Only accounts you own. Nothing happens until you confirm.",
                   kb.account_picker(views_, "claim_pick"))

    async def _claim_confirm(cb: CallbackQuery, account_id: int, username: str) -> None:
        runner = ctx.manager.get(account_id)
        if runner is None:
            await show(cb, "That account no longer exists.", kb.home_only())
            return
        try:
            me = await describe_account(runner)
        except (AccountUnavailable, RateLimited) as exc:
            await show(cb, f"Cannot confirm the account right now: {exc}", kb.home_only())
            return
        except Exception as exc:  # noqa: BLE001
            info = await runner.report_error("get_me", exc)
            await show(cb, f"Cannot confirm the account identity ({info.category.value}).", kb.home_only())
            return
        current = f"@{me['username']}" if me["username"] else "(no username)"
        await show(
            cb,
            "⚠️ <b>Confirm username change</b>\n"
            f"Account: <b>{runner.safe_id}</b> (verified live: {current})\n"
            f"New public username: <b>@{username}</b>\n\n"
            "This changes the account's public username. It does <b>not</b> create a channel. "
            "Telegram decides the outcome; the exact response will be shown, and nothing is retried automatically.",
            kb.claim_confirm(account_id))

    @router.callback_query(Nav.filter(F.a == "claim_do"))
    async def cb_claim_do(cb: CallbackQuery, callback_data: Nav) -> None:
        tgt = await ctx.repo.get_current_target()
        runner = ctx.manager.get(int(callback_data.v))
        if tgt is None or runner is None:
            await show(cb, "Target or account is gone. Nothing was changed.", kb.home_only())
            return
        result = await change_username(runner, runner.account_id, tgt["username"])
        icon = "✅" if result.ok else "❌"
        lines = [f"{icon} <b>{result.outcome.value}</b>", result.message]
        if result.wait_until:
            lines.append(f"Retry not before: {fmt_time(result.wait_until, tz)}")
        if result.ok:
            await ctx.repo.update_account_identity(runner.account_id, label=f"@{tgt['username']}",
                                                   username=tgt["username"], first_name=runner.label)
            await ctx.monitor.stop()
            lines.append("Monitoring was stopped because the target is now yours.")
        await show(cb, "\n".join(lines), kb.home_only())
