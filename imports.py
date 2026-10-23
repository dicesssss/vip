"""Import an existing authorised Telethon session (StringSession text or a .session file).

The session is validated by actually connecting with it, and is only moved into
persistent storage if it is authorised and not a duplicate. The admin's message
containing the secret should be deleted by the caller immediately.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from .accounts import AccountManager, AccountRunner, DuplicateAccount

log = logging.getLogger(__name__)

NET_TIMEOUT = 30.0
MAX_SESSION_FILE_BYTES = 1_000_000
SQLITE_MAGIC = b"SQLite format 3\x00"


class ImportFailed(Exception):
    """Message is safe to show to the administrator (never contains session data)."""


def _cleanup(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError, OSError):
            Path(str(path) + suffix).unlink()


async def _validate_and_register(manager: AccountManager, pending: Path) -> AccountRunner:
    client = manager.make_client(str(pending))
    try:
        try:
            await asyncio.wait_for(client.connect(), NET_TIMEOUT)
            if not await asyncio.wait_for(client.is_user_authorized(), NET_TIMEOUT):
                raise ImportFailed("This session is not authorized (it was logged out or revoked).")
            me = await asyncio.wait_for(client.get_me(), NET_TIMEOUT)
        except ImportFailed:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("session import validation failed: %s", type(exc).__name__)
            raise ImportFailed(
                f"Could not validate the session ({type(exc).__name__}). If it was created with a different "
                "API_ID/API_HASH, or is revoked or malformed, generate a fresh one."
            ) from exc
        finally:
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(client.disconnect(), 10)
        try:
            return await manager.register_session(
                user_id=int(me.id), username=getattr(me, "username", None),
                first_name=getattr(me, "first_name", None), phone=getattr(me, "phone", None), pending_path=pending)
        except DuplicateAccount as dup:
            raise ImportFailed(f"This account is already added as {dup.existing_label}.") from dup
    except BaseException:
        _cleanup(pending)
        raise


async def import_string_session(manager: AccountManager, session_string: str) -> AccountRunner:
    from telethon.sessions import SQLiteSession, StringSession  # lazy import

    text = (session_string or "").strip()
    if len(text) < 100 or any(ch.isspace() for ch in text):
        raise ImportFailed("That does not look like a Telethon StringSession (one long line of text).")
    pending = manager.new_pending_path()
    try:
        parsed = StringSession(text)
        if not parsed.auth_key:
            raise ValueError("no auth key")
        sqlite_session = SQLiteSession(str(pending))
        sqlite_session.set_dc(parsed.dc_id, parsed.server_address, parsed.port)
        sqlite_session.auth_key = parsed.auth_key
        sqlite_session.save()
        sqlite_session.close()
    except Exception as exc:  # noqa: BLE001
        _cleanup(pending)
        raise ImportFailed("That string could not be parsed as a Telethon StringSession.") from exc
    return await _validate_and_register(manager, pending)


async def import_session_file(manager: AccountManager, uploaded: Path) -> AccountRunner:
    """``uploaded`` must already be inside the pending directory (the caller downloads it there)."""
    try:
        size = uploaded.stat().st_size
        if size > MAX_SESSION_FILE_BYTES:
            raise ImportFailed("That file is too large to be a Telethon session.")
        with open(uploaded, "rb") as fh:
            if fh.read(16) != SQLITE_MAGIC:
                raise ImportFailed("That file is not a Telethon .session (SQLite) file.")
    except ImportFailed:
        _cleanup(uploaded)
        raise
    except OSError as exc:
        _cleanup(uploaded)
        raise ImportFailed("Could not read the uploaded file.") from exc
    with contextlib.suppress(OSError):
        uploaded.chmod(0o600)
    return await _validate_and_register(manager, uploaded)
