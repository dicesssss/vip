"""Automatic channel creation and public channel username claiming.

Creates a public channel if none exists or uses an existing one, 
and claims the target username automatically without manual confirmation steps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from .accounts import AccountUnavailable, RateLimited
from .errors import mro_names
from .validators import ValidationError, normalize_username

log = logging.getLogger(__name__)


class ClaimOutcome(str, Enum):
    SUCCESS = "success"
    TAKEN = "taken"
    INVALID = "invalid"
    NOT_MODIFIED = "not_modified"
    RATE_LIMITED = "rate_limited"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    REFUSED = "refused"


@dataclass
class ClaimResult:
    outcome: ClaimOutcome
    message: str
    wait_until: Optional[datetime] = None

    @property
    def ok(self) -> bool:
        return self.outcome is ClaimOutcome.SUCCESS


async def _get_or_create_claiming_channel(runner: Any) -> int:
    """Finds an existing channel owned by this account or creates a new public channel automatically."""
    from telethon.tl.functions.channels import CreateChannelRequest
    from telethon.tl.types import InputPeerChannel

    client = runner.client
    
    # 📝 First, check if the runner already cached or has a channel we can use
    # Or list dialogs to find a channel created by this bot previously
    try:
        async for dialog in client.iter_dialogs(limit=20):
            if dialog.is_channel and dialog.entity.creator:
                # Check if it's a megagroup or channel, prefer channel
                if getattr(dialog.entity, 'broadcast', False):
                    return dialog.id
    except Exception as exc:
        log.warning("Failed to iterate dialogs to find existing channel: %s", exc)

    # 🚀 If no suitable channel exists, bot creates a fresh public channel automatically!
    try:
        result = await runner.request(
            "create_channel",
            CreateChannelRequest(
                title="Username Snipe Channel",
                about="Auto-claiming channel for sniper bot.",
                megagroup=False
            )
        )
        for chat in result.chats:
            if getattr(chat, "id", None):
                # Return negative channel ID format used by Telethon for channels (-100...)
                return int(f"-100{chat.id}")
    except Exception as exc:
        log.exception("Failed to automatically create public channel: %s", exc)
        raise

    raise RuntimeError("Could not find or create a public channel for claiming.")


async def change_username(runner: Any, expected_account_id: int, username_raw: str) -> ClaimResult:
    """Automatically targets a public channel (creates one if needed) and sets the username."""
    from telethon.tl.functions.channels import UpdateUsernameRequest
    from telethon.tl.types import InputChannel

    try:
        username = normalize_username(username_raw)
    except ValidationError as exc:
        return ClaimResult(ClaimOutcome.INVALID, str(exc))

    try:
        # Ensure account is connected
        if not runner.is_connected:
            await runner.connect()
    except AccountUnavailable as exc:
        return ClaimResult(ClaimOutcome.ACCOUNT_UNAVAILABLE, f"The account is not connected ({exc}).")
    except RateLimited as rl:
        return ClaimResult(ClaimOutcome.RATE_LIMITED, "Rate limited; try again after the wait.", rl.until)
    except Exception as exc:
        return ClaimResult(ClaimOutcome.ACCOUNT_UNAVAILABLE, f"Could not connect account: {exc}")

    try:
        # Get or automatically create the public channel for this account
        channel_id = await _get_or_create_claiming_channel(runner)
        
        # Resolve channel entity to input channel
        entity = await runner.client.get_entity(channel_id)
        input_channel = InputChannel(entity.id, entity.access_hash)

        # Attempt to set the username on the public channel
        await runner.request(
            "update_channel_username",
            UpdateUsernameRequest(
                channel=input_channel,
                username=username
            )
        )
    except RateLimited as rl:
        return ClaimResult(ClaimOutcome.RATE_LIMITED,
                           "Telegram rate-limited this change.",
                           rl.until)
    except AccountUnavailable as exc:
        return ClaimResult(ClaimOutcome.ACCOUNT_UNAVAILABLE, f"The account is not connected ({exc}).")
    except Exception as exc:  # noqa: BLE001
        names = mro_names(exc)
        text = str(exc).upper()
        if "UsernameOccupiedError" in names or "USERNAME_OCCUPIED" in text:
            return ClaimResult(ClaimOutcome.TAKEN, f"@{username} is currently taken (USERNAME_OCCUPIED).")
        if "UsernameInvalidError" in names:
            return ClaimResult(ClaimOutcome.INVALID, "Telegram says this username is invalid.")
        if "UsernameNotModifiedError" in names:
            return ClaimResult(ClaimOutcome.NOT_MODIFIED, "Telegram reports the username is unchanged.")
        if "CHANNELS_ADMIN_PUBLIC_TOO_MUCH" in text:
            return ClaimResult(ClaimOutcome.REFUSED, "This account has too many public channels created. Free up one of its public channels.")
        
        info = await runner.report_error("update_channel_username", exc)
        return ClaimResult(ClaimOutcome.REFUSED,
                           f"Telegram refused the change ({type(exc).__name__}; category {info.category.value}).")

    return ClaimResult(ClaimOutcome.SUCCESS, f"@{username} was successfully claimed on the public channel!")
