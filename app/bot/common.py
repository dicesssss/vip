"""Helpers shared by handler modules."""
from __future__ import annotations

import contextlib
import logging
from typing import Optional, Union

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .context import AppContext

log = logging.getLogger(__name__)


async def show(target: Union[Message, CallbackQuery], text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    """Edit the dashboard message for button presses; send a new message for typed input."""
    if isinstance(target, CallbackQuery):
        message = target.message
        if isinstance(message, Message):
            try:
                await message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest as exc:
                if "not modified" not in str(exc).lower():
                    await message.answer(text, reply_markup=kb)
        else:  # inaccessible/old message: fall back to a fresh one
            await target.bot.send_message(target.from_user.id, text, reply_markup=kb)
        with contextlib.suppress(Exception):
            await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


async def safe_delete(message: Message) -> None:
    """Delete a message that may contain a secret (code, password, session string)."""
    with contextlib.suppress(Exception):
        await message.delete()


def monitor_runner(ctx: AppContext):
    """Read-only lookup of the configured monitoring account runner (no side effects)."""
    return ctx._monitor_runner_cache() if hasattr(ctx, "_monitor_runner_cache") else None


async def monitor_account_label(ctx: AppContext) -> Optional[str]:
    raw = await ctx.repo.get_setting("monitor_account_id")
    if not raw:
        return None
    try:
        runner = ctx.manager.get(int(raw))
    except ValueError:
        return None
    return runner.safe_id if runner else f"id {raw} (removed)"
