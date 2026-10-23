"""Administrator notifications with loop protection.

Identical errors (same account + operation + category) are grouped: the first one
is sent immediately, repeats inside the cooldown window are counted and folded
into the next message that is allowed through.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from .errors import Category, ErrorInfo
from .timeutil import fmt_time, human_duration, utcnow

log = logging.getLogger(__name__)

SendFn = Callable[[str, Any], Awaitable[None]]


@dataclass
class _Seen:
    last_sent: float
    suppressed: int = 0


class Notifier:
    def __init__(self, send: SendFn, repo: Any, *, tz: Any = None, cooldown: float = 600.0,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._send = send
        self._repo = repo
        self._tz = tz
        self._cooldown = cooldown
        self._now = now
        self._seen: Dict[Tuple[Any, str, str], _Seen] = {}

    async def send(self, text: str, reply_markup: Any = None) -> bool:
        """Deliver a message to the administrator, honouring Bot API retry-after once."""
        for attempt in (0, 1):
            try:
                await self._send(text, reply_markup)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                retry_after = getattr(exc, "retry_after", None)
                if retry_after and attempt == 0:
                    await asyncio.sleep(min(float(retry_after), 60.0))
                    continue
                log.warning("could not notify administrator: %s", type(exc).__name__)
                return False
        return False

    async def _allowed(self, category: Category) -> bool:
        if category is Category.AUTHORIZATION:
            return True  # a dead session must always be reported
        key = "notify_flood" if category is Category.FLOOD_WAIT else "notify_errors"
        return await self._repo.get_bool(key)

    async def notify_error(self, *, account_label: Optional[str], account_id: Optional[int], op: str,
                           info: ErrorInfo, status: str, next_retry: Optional[datetime] = None,
                           error_time: Optional[datetime] = None) -> bool:
        if not await self._allowed(info.category):
            return False
        key = (account_id, op, info.category.value)
        now = self._now()
        seen = self._seen.get(key)
        if seen is not None and now - seen.last_sent < self._cooldown:
            seen.suppressed += 1
            return False
        grouped = seen.suppressed if seen else 0
        self._seen[key] = _Seen(last_sent=now)

        icon = {"flood_wait": "⏳", "authorization": "🔒", "connection": "🔌",
                "invalid_username": "🚫", "unexpected": "⚠️"}[info.category.value]
        title = {"flood_wait": "FloodWait", "authorization": "Authorization problem", "connection": "Connection problem",
                 "invalid_username": "Invalid username", "unexpected": "Unexpected error"}[info.category.value]
        lines = [
            f"{icon} <b>{title}</b>",
            f"Account: {html.escape(account_label or '—')}",
            f"Operation: <code>{html.escape(op)}</code>",
            f"Category: <code>{info.category.value}</code>",
        ]
        if info.wait_seconds is not None:
            lines.append(f"Required wait: {human_duration(info.wait_seconds)}")
        lines.append(f"Error time: {fmt_time(error_time or utcnow(), self._tz)}")
        lines.append(f"Account status: {html.escape(status)}")
        lines.append(f"Next retry: {fmt_time(next_retry, self._tz) if next_retry else 'not scheduled / unknown'}")
        if info.permanent:
            lines.append("This failure is permanent: retries stopped. Re-add the account to resume.")
        lines.append(f"Detail: <code>{html.escape(info.message)[:300]}</code>")
        if grouped:
            lines.append(f"(+{grouped} identical event(s) grouped since the last notice)")
        return await self.send("\n".join(lines))
