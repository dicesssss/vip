"""Username availability check.

``available`` is only reported when TWO independent server answers agree:

1. ``account.checkUsername`` says the name is free, and
2. ``contacts.resolveUsername`` says nobody currently owns it (USERNAME_NOT_OCCUPIED).

Any other combination is ``unknown`` (or ``unavailable`` when Telegram says so), so an
unexpected response can never be mistaken for "claimable". Even ``available`` is only
a snapshot: nothing here reserves the name.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from .accounts import AccountUnavailable, RateLimited
from .errors import mro_names

log = logging.getLogger(__name__)


class State(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    RATE_LIMITED = "rate_limited"


@dataclass
class CheckResult:
    state: State
    detail: str
    wait_until: Optional[datetime] = None
    invalid: bool = False   # Telegram rejected the username itself -> monitoring must stop
    error_reported: bool = False


def _default_check_request(username: str) -> Any:
    from telethon.tl.functions.account import CheckUsernameRequest

    return CheckUsernameRequest(username=username)


def _default_resolve_request(username: str) -> Any:
    from telethon.tl.functions.contacts import ResolveUsernameRequest

    return ResolveUsernameRequest(username=username)


class UsernameChecker:
    def __init__(self, check_request: Callable[[str], Any] = _default_check_request,
                 resolve_request: Callable[[str], Any] = _default_resolve_request) -> None:
        self._check_request = check_request
        self._resolve_request = resolve_request

    async def check(self, runner: Any, username: str) -> CheckResult:
        # ---- signal 1: account.checkUsername
        try:
            answer = await runner.request("check_username", self._check_request(username))
        except RateLimited as rl:
            return CheckResult(State.RATE_LIMITED, f"FloodWait active until {rl.until.isoformat(timespec='seconds')}",
                               wait_until=rl.until, error_reported=True)
        except AccountUnavailable as exc:
            return CheckResult(State.UNKNOWN, f"monitoring account unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001
            return await self._handle_exception(runner, "check_username", exc)

        if answer is False:
            return CheckResult(State.UNAVAILABLE, "taken, reserved or otherwise not claimable (server said no)")
        if answer is not True:
            return CheckResult(State.UNKNOWN, f"unexpected response type {type(answer).__name__} from checkUsername")

        # ---- signal 2: contacts.resolveUsername (only runs when signal 1 said "free")
        try:
            await runner.request("resolve_username", self._resolve_request(username))
        except RateLimited as rl:
            return CheckResult(State.RATE_LIMITED, "checkUsername says free, but confirmation is rate-limited; unconfirmed",
                               wait_until=rl.until, error_reported=True)
        except AccountUnavailable as exc:
            return CheckResult(State.UNKNOWN, f"monitoring account unavailable during confirmation: {exc}")
        except Exception as exc:  # noqa: BLE001
            if "UsernameNotOccupiedError" in mro_names(exc):
                return CheckResult(State.AVAILABLE, "checkUsername and resolveUsername agree: no current holder")
            return await self._handle_exception(runner, "resolve_username", exc)
        # resolve succeeded => someone owns it, contradicting checkUsername
        return CheckResult(State.UNKNOWN, "signals disagree: checkUsername says free but the name resolves to a holder")

    async def _handle_exception(self, runner: Any, op: str, exc: BaseException) -> CheckResult:
        names = mro_names(exc)
        if "UsernameInvalidError" in names:
            return CheckResult(State.UNKNOWN, "Telegram rejected the username as invalid", invalid=True)
        if "UsernameOccupiedError" in names:
            return CheckResult(State.UNAVAILABLE, "username is occupied")
        text = str(exc).upper()
        if "USERNAME_PURCHASE_AVAILABLE" in text or "UsernamePurchaseAvailable" in type(exc).__name__:
            return CheckResult(State.UNAVAILABLE, "listed for purchase/auction; not claimable the normal way")
        info = await runner.report_error(op, exc)
        return CheckResult(State.UNKNOWN, f"{info.category.value}: {info.message}"[:300], error_reported=True)
