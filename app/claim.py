"""Profile-username change on an account the administrator owns.

Deliberately separate from monitoring. Nothing here runs automatically, never
creates a channel, and never promises success: the exact server outcome is reported.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from .accounts import AccountUnavailable, RateLimited
from .errors import mro_names
from .validators import ValidationError, normalize_username

log = logging.getLogger(__name__)

MANUAL_FALLBACK = (
    "Manual fallback: open Telegram with that account → Settings → My Profile / Edit Profile → Username, "
    "and try the name there. Telegram shows the authoritative error."
)


class ClaimOutcome(str, Enum):
    SUCCESS = "success"
    TAKEN = "taken"
    INVALID = "invalid"
    NOT_MODIFIED = "not_modified"
    RATE_LIMITED = "rate_limited"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    IDENTITY_MISMATCH = "identity_mismatch"
    REFUSED = "refused"


@dataclass
class ClaimResult:
    outcome: ClaimOutcome
    message: str
    wait_until: Optional[datetime] = None

    @property
    def ok(self) -> bool:
        return self.outcome is ClaimOutcome.SUCCESS


def _default_update_request(username: str) -> Any:
    from telethon.tl.functions.account import UpdateUsernameRequest

    return UpdateUsernameRequest(username=username)


async def describe_account(runner: Any) -> dict:
    """Fresh identity straight from Telegram, used for the confirmation screen."""
    me = await runner.fetch_me()
    return {"id": int(me.id), "username": getattr(me, "username", None), "first_name": getattr(me, "first_name", None)}


async def change_username(runner: Any, expected_account_id: int, username_raw: str,
                          update_request: Callable[[str], Any] = _default_update_request) -> ClaimResult:
    try:
        username = normalize_username(username_raw)
    except ValidationError as exc:
        return ClaimResult(ClaimOutcome.INVALID, str(exc))

    try:
        me = await describe_account(runner)
    except AccountUnavailable as exc:
        return ClaimResult(ClaimOutcome.ACCOUNT_UNAVAILABLE, f"The account is not connected ({exc}). {MANUAL_FALLBACK}")
    except RateLimited as rl:
        return ClaimResult(ClaimOutcome.RATE_LIMITED, "Rate limited; try again after the wait.", rl.until)
    except Exception as exc:  # noqa: BLE001
        info = await runner.report_error("get_me", exc)
        return ClaimResult(ClaimOutcome.ACCOUNT_UNAVAILABLE, f"Could not confirm the account identity ({info.category.value}).")

    if me["id"] != expected_account_id or runner.account_id != expected_account_id:
        return ClaimResult(ClaimOutcome.IDENTITY_MISMATCH,
                           "The connected account is not the one you confirmed. Nothing was changed.")
    if (me.get("username") or "").lower() == username:
        return ClaimResult(ClaimOutcome.NOT_MODIFIED, "That is already this account's username. Nothing changed.")

    try:
        await runner.request("update_username", update_request(username))
    except RateLimited as rl:
        return ClaimResult(ClaimOutcome.RATE_LIMITED,
                           "Telegram rate-limited this change. It was not retried and will not be until the wait ends.",
                           rl.until)
    except AccountUnavailable as exc:
        return ClaimResult(ClaimOutcome.ACCOUNT_UNAVAILABLE, f"The account is not connected ({exc}). {MANUAL_FALLBACK}")
    except Exception as exc:  # noqa: BLE001
        names = mro_names(exc)
        text = str(exc).upper()
        if "UsernameOccupiedError" in names or "USERNAME_OCCUPIED" in text:
            return ClaimResult(ClaimOutcome.TAKEN, f"@{username} is currently taken (USERNAME_OCCUPIED). {MANUAL_FALLBACK}")
        if "UsernameInvalidError" in names:
            return ClaimResult(ClaimOutcome.INVALID, "Telegram says this username is invalid.")
        if "UsernameNotModifiedError" in names:
            return ClaimResult(ClaimOutcome.NOT_MODIFIED, "Telegram reports the username is unchanged.")
        if "USERNAME_PURCHASE_AVAILABLE" in text:
            return ClaimResult(ClaimOutcome.TAKEN, f"@{username} is listed for purchase and cannot be set normally. {MANUAL_FALLBACK}")
        info = await runner.report_error("update_username", exc)
        return ClaimResult(ClaimOutcome.REFUSED,
                           f"Telegram refused the change ({type(exc).__name__}; category {info.category.value}). {MANUAL_FALLBACK}")

    runner.username = username  # keep the in-memory identity current
    return ClaimResult(ClaimOutcome.SUCCESS, f"@{username} is now this account's username.")
