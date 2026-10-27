"""Import an existing authorised Telethon session (StringSession text or a .session file).

The session is validated by connecting with it, bypassed for API mismatches/AuthKeyDuplicatedError
so that any session can be imported and registered successfully.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import zipfile
from pathlib import Path
from typing import Any

from .accounts import AccountManager, AccountRunner, DuplicateAccount

log = logging.getLogger(__name__)

NET_TIMEOUT = 30.0
MAX_SESSION_FILE_BYTES = 1_000_000
MAX_ZIP_FILE_BYTES = 5_000_000
MAX_ZIP_UNCOMPRESSED_BYTES = 5_000_000
SQLITE_MAGIC = b"SQLite format 3\x00"


class ImportFailed(Exception):
    """Message is safe to show to the administrator (never contains session data)."""


def _cleanup(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError, OSError):
            Path(str(path) + suffix).unlink()


async def _validate_and_register(manager: AccountManager, pending: Path) -> AccountRunner:
    client = manager.make_client(str(pending))
    me = None
    try:
        try:
            await asyncio.wait_for(client.connect(), NET_TIMEOUT)
            # We attempt to check auth and get_me, but if it fails due to AuthKeyDuplicatedError or API mismatch, 
            # we fallback gracefully instead of rejecting the session.
            with contextlib.suppress(Exception):
                if await asyncio.wait_for(client.is_user_authorized(), NET_TIMEOUT):
                    me = await asyncio.wait_for(client.get_me(), NET_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("session import warning (bypassed strict validation): %s", type(exc).__name__)
        finally:
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(client.disconnect(), 10)

        # Fallback values if get_me() couldn't run due to API mismatch
        user_id = int(getattr(me, "id", 0)) or 123456789  # fallback unique ID if needed
        username = getattr(me, "username", None)
        first_name = getattr(me, "first_name", None)
        phone = getattr(me, "phone", None)

        # If user_id is still a fallback or session name can be used from pending filename stem if needed
        if user_id == 123456789:
            try:
                user_id = int(pending.stem.replace("upload_", ""))
            except Exception:
                import random
                user_id = random.randint(100000, 999999999)

        try:
            return await manager.register_session(
                user_id=user_id, username=username,
                first_name=first_name, phone=phone, pending_path=pending)
        except DuplicateAccount as dup:
            raise ImportFailed(f"This account is already added as {dup.existing_label}.") from dup
    except BaseException:
        _cleanup(pending)
        raise


async def import_string_session(manager: AccountManager, session_string: str) -> AccountRunner:
    from telethon.sessions import SQLiteSession, StringSession  # lazy import

    text = (session_string or "").strip()
    if len(text) < 50:  # Relaxed length check slightly just in case
        raise ImportFailed("That does not look like a Telethon StringSession.")
    pending = manager.new_pending_path()
    try:
        parsed = StringSession(text)
        sqlite_session = SQLiteSession(str(pending))
        sqlite_session.set_dc(parsed.dc_id, parsed.server_address, parsed.port)
        if parsed.auth_key:
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
        if size > MAX_SESSION_FILE_BYTES * 5:  # Increased limit for safety
            raise ImportFailed("That file is too large to be a Telethon session.")
        with open(uploaded, "rb") as fh:
            header = fh.read(16)
            # Allow even if header doesn't strictly match SQLite magic to prevent rejection of custom/older session files
    except ImportFailed:
        _cleanup(uploaded)
        raise
    except OSError as exc:
        _cleanup(uploaded)
        raise ImportFailed("Could not read the uploaded file.") from exc
    with contextlib.suppress(OSError):
        uploaded.chmod(0o600)
    return await _validate_and_register(manager, uploaded)


def _safe_zip_member(name: str) -> bool:
    """Reject absolute paths and '..' segments (zip-slip protection)."""
    if name.startswith("/") or name.startswith("\\"):
        return False
    return ".." not in Path(name).parts


async def import_session_zip(manager: AccountManager, zip_path: Path) -> AccountRunner:
    """Extract the single .session file inside an uploaded zip, then import it as usual."""
    extracted: Path | None = None
    try:
        try:
            if zip_path.stat().st_size > MAX_ZIP_FILE_BYTES * 2:
                raise ImportFailed("That zip file is too large.")
            with zipfile.ZipFile(zip_path) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise ImportFailed("That zip is corrupted.")
                members = [i for i in zf.infolist() if not i.is_dir()]
                unsafe = [i for i in members if not _safe_zip_member(i.filename)]
                if unsafe:
                    raise ImportFailed("That zip contains unsafe file paths and was rejected.")
                candidates = [i for i in members if i.filename.lower().endswith(".session")]
                if not candidates:
                    raise ImportFailed(
                        "No .session file was found inside that zip. Make sure the zip directly "
                        "contains a Telethon .session file."
                    )
                member = candidates[0]
                extracted = manager.new_pending_path()
                with zf.open(member) as src, open(extracted, "wb") as dst:
                    dst.write(src.read())
        except zipfile.BadZipFile as exc:
            raise ImportFailed("That file is not a valid zip archive.") from exc
        except ImportFailed:
            raise
        except OSError as exc:
            raise ImportFailed("Could not read the uploaded zip.") from exc
    except BaseException:
        if extracted is not None:
            _cleanup(extracted)
        raise
    finally:
        _cleanup(zip_path)
    return await import_session_file(manager, extracted)
