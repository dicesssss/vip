"""Add / import / list / remove accounts."""
from __future__ import annotations

import logging
import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..accounts import AccStatus
from ..imports import ImportFailed, import_session_file, import_string_session
from ..login import LoginError, LoginFlow, LoginOutcome
from ..validators import ValidationError, parse_phone
from . import keyboards as kb
from . import views
from .common import safe_delete, show
from .context import AppContext
from .keyboards import Nav

log = logging.getLogger(__name__)


class AddAccount(StatesGroup):
    phone = State()
    code = State()
    password = State()


class ImportSession(StatesGroup):
    payload = State()


def register(router: Router, ctx: AppContext) -> None:
    tz = ctx.cfg.tz()

    async def _after_added(target, state: FSMContext, runner) -> None:
        await state.clear()
        await ctx.cancel_login()
        await ctx.bot.send_message(
            ctx.cfg.admin_id,
            f"✅ Account added: <b>{runner.safe_id}</b>\nStatus: {runner.status.value}. "
            "The session is saved to persistent storage and will resume after restarts.",
            reply_markup=kb.main_menu())

    # ------------------------------------------------------------------ interactive login
    @router.callback_query(Nav.filter(F.a == "add"))
    async def add_start(cb: CallbackQuery, state: FSMContext) -> None:
        await ctx.cancel_login()
        await state.set_state(AddAccount.phone)
        await show(cb, "➕ <b>Add account</b>\nSend the phone number of the Telegram account in international "
                       "format, e.g. <code>+919876543210</code>.\n\nOnly add accounts you own.", kb.cancel())

    @router.message(AddAccount.phone, F.text)
    async def add_phone(message: Message, state: FSMContext) -> None:
        await safe_delete(message)
        try:
            phone = parse_phone(message.text or "")
        except ValidationError as exc:
            await message.answer(f"❌ {exc}", reply_markup=kb.cancel())
            return
        flow = LoginFlow(ctx.manager)
        ctx.login = flow
        try:
            await flow.start(phone)
        except LoginError as exc:
            await ctx.cancel_login()
            await state.clear()
            await message.answer(exc.user_message, reply_markup=kb.home_only())
            return
        await state.set_state(AddAccount.code)
        await message.answer(
            "📨 Telegram sent a login code to that account (check the Telegram app, or SMS).\n"
            "Send it <b>with spaces between the digits</b>, e.g. <code>1 2 3 4 5</code>. "
            "If you send it as one plain number, Telegram may detect it and invalidate the code.",
            reply_markup=kb.code_step())

    @router.callback_query(Nav.filter(F.a == "resend"), AddAccount.code)
    async def add_resend(cb: CallbackQuery, state: FSMContext) -> None:
        flow = ctx.login
        if flow is None:
            await state.clear()
            await show(cb, "No login in progress.", kb.home_only())
            return
        try:
            await flow.resend_code()
        except LoginError as exc:
            if exc.terminal:
                await ctx.cancel_login()
                await state.clear()
                await show(cb, exc.user_message, kb.home_only())
            else:
                await show(cb, exc.user_message, kb.code_step())
            return
        await show(cb, "📨 A new code was requested. Send it with spaces between digits.", kb.code_step())

    @router.message(AddAccount.code, F.text)
    async def add_code(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        await safe_delete(message)  # remove the code from the chat immediately
        flow = ctx.login
        if flow is None:
            await state.clear()
            await message.answer("No login in progress.", reply_markup=kb.home_only())
            return
        try:
            outcome = await flow.submit_code(raw)
        except LoginError as exc:
            if exc.terminal:
                await ctx.cancel_login()
                await state.clear()
                await message.answer(exc.user_message, reply_markup=kb.home_only())
            else:
                await message.answer(exc.user_message, reply_markup=kb.code_step())
            return
        finally:
            del raw
        if outcome is LoginOutcome.NEED_PASSWORD:
            await state.set_state(AddAccount.password)
            await message.answer("🔐 This account has two-step verification. Send the password "
                                 "(your message will be deleted right away).", reply_markup=kb.cancel())
            return
        await _after_added(message, state, flow.result)

    @router.message(AddAccount.password, F.text)
    async def add_password(message: Message, state: FSMContext) -> None:
        password = message.text or ""
        await safe_delete(message)
        flow = ctx.login
        if flow is None:
            await state.clear()
            await message.answer("No login in progress.", reply_markup=kb.home_only())
            return
        try:
            await flow.submit_password(password)
        except LoginError as exc:
            if exc.terminal:
                await ctx.cancel_login()
                await state.clear()
                await message.answer(exc.user_message, reply_markup=kb.home_only())
            else:
                await message.answer(exc.user_message, reply_markup=kb.cancel())
            return
        finally:
            del password
        await _after_added(message, state, flow.result)

    # ------------------------------------------------------------------ import
    @router.callback_query(Nav.filter(F.a == "import"))
    async def import_start(cb: CallbackQuery, state: FSMContext) -> None:
        await ctx.cancel_login()
        await state.set_state(ImportSession.payload)
        await show(cb,
                   "📥 <b>Import session</b>\nSend either:\n"
                   "• a Telethon <b>StringSession</b> as a text message, or\n"
                   "• a Telethon <b>.session</b> file as a document.\n\n"
                   "Treat it like a password: anyone holding it controls the account. "
                   "Your message is deleted as soon as it is received, and the secret is never logged or echoed.\n"
                   "Best results: the session should have been created with the same API_ID/API_HASH this app uses.",
                   kb.cancel())

    @router.message(ImportSession.payload, F.text)
    async def import_text(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        await safe_delete(message)
        try:
            runner = await import_string_session(ctx.manager, raw)
        except ImportFailed as exc:
            await message.answer(f"❌ {exc}", reply_markup=kb.cancel())
            return
        finally:
            del raw
        await _after_added(message, state, runner)

    @router.message(ImportSession.payload, F.document)
    async def import_file(message: Message, state: FSMContext) -> None:
        doc = message.document
        if doc is None:
            return
        if doc.file_size and doc.file_size > 1_000_000:
            await safe_delete(message)
            await message.answer("❌ That file is too large to be a Telethon session.", reply_markup=kb.cancel())
            return
        dest = ctx.cfg.pending_dir / f"upload_{uuid.uuid4().hex}.session"
        try:
            await ctx.bot.download(doc, destination=dest)
        except Exception as exc:  # noqa: BLE001
            log.warning("session upload download failed: %s", type(exc).__name__)
            await message.answer("❌ Could not download that file from Telegram.", reply_markup=kb.cancel())
            return
        finally:
            await safe_delete(message)  # the chat copy of the session file is removed either way
        try:
            runner = await import_session_file(ctx.manager, dest)
        except ImportFailed as exc:
            await message.answer(f"❌ {exc}", reply_markup=kb.cancel())
            return
        await _after_added(message, state, runner)

    # ------------------------------------------------------------------ list / detail
    async def _list(target) -> None:
        views_ = [r.view() for r in ctx.manager.all()]
        raw = await ctx.repo.get_setting("monitor_account_id")
        mon = int(raw) if raw.isdigit() else None
        text = views.account_list(views_, tz, mon)
        markup = kb.account_picker(views_, "acct") if views_ else kb.home_only()
        await show(target, text, markup)

    @router.callback_query(Nav.filter(F.a == "accts"))
    async def cb_accounts(cb: CallbackQuery) -> None:
        await _list(cb)

    @router.callback_query(Nav.filter(F.a == "acct"))
    async def cb_account(cb: CallbackQuery, callback_data: Nav) -> None:
        runner = ctx.manager.get(int(callback_data.v))
        if runner is None:
            await _list(cb)
            return
        mon = (await ctx.repo.get_setting("monitor_account_id")) == str(runner.account_id)
        await show(cb, views.account_detail(runner.view(), tz, mon),
                   kb.account_detail(runner.account_id, can_reconnect=not runner.permanent))

    @router.callback_query(Nav.filter(F.a == "reconnect"))
    async def cb_reconnect(cb: CallbackQuery, callback_data: Nav) -> None:
        runner = ctx.manager.get(int(callback_data.v))
        if runner is None or runner.permanent:
            await _list(cb)
            return
        await runner.restart()
        await show(cb, f"🔌 Reconnecting {runner.safe_id}…", kb.account_picker([r.view() for r in ctx.manager.all()], "acct"))

    # ------------------------------------------------------------------ remove
    @router.callback_query(Nav.filter(F.a == "rm_list"))
    async def cb_rm_list(cb: CallbackQuery) -> None:
        views_ = [r.view() for r in ctx.manager.all()]
        if not views_:
            await show(cb, "No accounts to remove.", kb.home_only())
            return
        await show(cb, "🗑 <b>Remove which account?</b>", kb.account_picker(views_, "rm_ask"))

    @router.callback_query(Nav.filter(F.a == "rm_ask"))
    async def cb_rm_ask(cb: CallbackQuery, callback_data: Nav) -> None:
        runner = ctx.manager.get(int(callback_data.v))
        if runner is None:
            await _list(cb)
            return
        can_logout = runner.status is AccStatus.CONNECTED
        text = (f"Remove <b>{runner.safe_id}</b>?\nThe saved session file is deleted from storage. "
                "If it is the monitoring account, monitoring stops.\n\n" + views.REVOCATION_GUIDANCE)
        markup = kb.remove_choice(runner.account_id)
        if not can_logout:
            markup = kb.remove_choice(runner.account_id)
        await show(cb, text, markup)

    async def _remove(cb: CallbackQuery, callback_data: Nav, logout: bool) -> None:
        account_id = int(callback_data.v)
        runner = ctx.manager.get(account_id)
        label = runner.safe_id if runner else str(account_id)
        was_monitor = (await ctx.repo.get_setting("monitor_account_id")) == str(account_id)
        if was_monitor:
            await ctx.monitor.stop()
            await ctx.repo.set_setting("monitor_account_id", "")
        result = await ctx.manager.remove_account(account_id, logout=logout)
        note = ("Telegram confirmed the session was logged out." if result["logged_out"]
                else "The Telegram-side login may still be active — terminate it under Settings → Devices.")
        extra = "\nMonitoring was stopped because this was the monitoring account." if was_monitor else ""
        await show(cb, f"🗑 Removed <b>{label}</b>.\n{note}{extra}", kb.home_only())

    @router.callback_query(Nav.filter(F.a == "rm_do"))
    async def cb_rm_do(cb: CallbackQuery, callback_data: Nav) -> None:
        await _remove(cb, callback_data, logout=False)

    @router.callback_query(Nav.filter(F.a == "rm_logout"))
    async def cb_rm_logout(cb: CallbackQuery, callback_data: Nav) -> None:
        await _remove(cb, callback_data, logout=True)
