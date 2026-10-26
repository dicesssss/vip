"""Administrator-only gate. Non-admins get no reply at all (nothing to probe or leak)."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

log = logging.getLogger(__name__)


class AdminOnly(BaseMiddleware):
    def __init__(self, admin_id: int) -> None:
        self.admin_id = admin_id
        self._last_log: Dict[int, float] = {}

    async def __call__(self, handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]], event: Any,
                       data: Dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id != self.admin_id:
            if user is not None:
                now = time.monotonic()
                if now - self._last_log.get(user.id, 0.0) > 600:
                    self._last_log[user.id] = now
                    log.warning("ignored update from non-admin user id %s", user.id)
            return None
        chat = event.chat if isinstance(event, Message) else (
            event.message.chat if isinstance(event, CallbackQuery) and event.message is not None else None)
        if chat is not None and chat.type != "private":
            return None  # the control panel only works in the administrator's private chat
        return await handler(event, data)
