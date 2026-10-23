"""Classify exceptions into the categories the administrator cares about.

Classification works on exception *class names* (walking the MRO) so it does not
import Telethon, stays stable across Telethon releases, and is unit-testable.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .logging_setup import redact


class Category(str, Enum):
    FLOOD_WAIT = "flood_wait"
    CONNECTION = "connection"
    AUTHORIZATION = "authorization"
    INVALID_USERNAME = "invalid_username"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class ErrorInfo:
    category: Category
    message: str
    wait_seconds: Optional[int] = None
    permanent: bool = False


_FLOOD = {"FloodWaitError", "FloodError", "SlowModeWaitError", "FloodPremiumWaitError", "FloodTestPhoneWaitError"}
_AUTH = {
    "AuthKeyUnregisteredError", "AuthKeyInvalidError", "AuthKeyDuplicatedError", "SessionRevokedError",
    "SessionExpiredError", "UserDeactivatedError", "UserDeactivatedBanError", "UnauthorizedError",
}
_INVALID_USERNAME = {"UsernameInvalidError"}
_CONNECTION_NAMES = {"ServerError", "TimedOutError", "RPCTimeoutError"}

_WAIT_RE = re.compile(r"wait of (\d+) seconds", re.IGNORECASE)


def mro_names(exc: BaseException) -> set:
    return {cls.__name__ for cls in type(exc).__mro__}


def classify(exc: BaseException) -> ErrorInfo:
    names = mro_names(exc)
    short = redact(str(exc))[:200]
    label = f"{type(exc).__name__}: {short}" if short else type(exc).__name__

    if names & _FLOOD:
        seconds = getattr(exc, "seconds", None)
        if not isinstance(seconds, int):
            match = _WAIT_RE.search(str(exc))
            seconds = int(match.group(1)) if match else None
        return ErrorInfo(Category.FLOOD_WAIT, label, wait_seconds=seconds)
    if names & _AUTH:
        return ErrorInfo(Category.AUTHORIZATION, label, permanent=True)
    if names & _INVALID_USERNAME:
        return ErrorInfo(Category.INVALID_USERNAME, label)
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)) or names & _CONNECTION_NAMES:
        return ErrorInfo(Category.CONNECTION, label)
    return ErrorInfo(Category.UNEXPECTED, label)
