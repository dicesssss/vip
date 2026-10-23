"""Interactive login: phone -> code -> optional 2FA password.

Nothing sensitive is stored: the login code and password exist only as local
variables for the duration of one call and are never logged. The phone number is
kept in memory for the life of the flow and only a masked hint reaches the DB.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .accounts import AccountManager, AccountRunner, DuplicateAccount
from .errors import mro_names
from .timeutil import human_duration
from .validators import ValidationError, parse_login_code

log = logging.getLogger(__name__)

NET_TIMEOUT = 30.0


class LoginError(Exception):
    def __init__(self, user_message: str, *, terminal: bool = False, can_resend: bool = False) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.terminal = terminal
        self.can_resend = can_resend


class LoginOutcome(str, Enum):
    NEED_PASSWORD = "need_password"
    DONE = "done"


def map_login_error(exc: BaseException) -> LoginError:
    """Translate Telethon/network exceptions into admin-friendly messages (by class name)."""
    names = mro_names(exc)
    if "PhoneCodeInvalidError" in names or "PhoneCodeEmptyError" in names:
        return LoginError("❌ That code is wrong. Check the latest code and send it again.")
    if "PhoneCodeExpiredError" in names:
        return LoginError("⌛ That code has expired. Tap “Resend code” to get a new one.", can_resend=True)
    if "PasswordHashInvalidError" in names:
        return LoginError("❌ Wrong two-step verification password. Try again.")
    if "PhoneNumberInvalidError" in names:
        return LoginError("❌ Telegram says this phone number is invalid.", terminal=True)
    if "PhoneNumberBannedError" in names:
        return LoginError("🚫 This phone number is banned by Telegram.", terminal=True)
    if "PhoneNumberUnoccupiedError" in names:
        return LoginError("This number has no Telegram account. Creating accounts is not supported.", terminal=True)
    if "PhoneNumberFloodError" in names:
        return LoginError("⏳ Too many login attempts for this number. Wait a while before trying again.", terminal=True)
    if names & {"ApiIdInvalidError", "ApiIdPublishedFloodError"}:
        return LoginError("🔑 Telegram rejected API_ID/API_HASH. Check the Railway variables.", terminal=True)
    if "FloodWaitError" in names or "FloodError" in names:
        seconds = getattr(exc, "seconds", None)
        wait = f" Wait {human_duration(seconds)} before trying again." if isinstance(seconds, int) else ""
        return LoginError(f"⏳ Telegram rate-limited this login.{wait}", terminal=True)
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return LoginError("🔌 Network error while talking to Telegram. Please start again.", terminal=True)
    return LoginError(f"⚠️ Unexpected error ({type(exc).__name__}). Login cancelled.", terminal=True)


class LoginFlow:
    TTL_SECONDS = 600
    MAX_CODE_TRIES = 5
    MAX_PASSWORD_TRIES = 5

    def __init__(self, manager: AccountManager, clock: Callable[[], float] = time.monotonic) -> None:
        self._manager = manager
        self._clock = clock
        self._started = clock()
        self._client: Any = None
        self._pending: Optional[Path] = None
        self._phone: Optional[str] = None
        self._hash: Optional[str] = None
        self._code_tries = 0
        self._password_tries = 0
        self.result: Optional[AccountRunner] = None

    # ------------------------------------------------------------------ steps
    async def start(self, phone: str) -> None:
        self._phone = phone
        self._pending = self._manager.new_pending_path()
        self._client = self._manager.make_client(str(self._pending))
        try:
            await asyncio.wait_for(self._client.connect(), NET_TIMEOUT)
            sent = await asyncio.wait_for(self._client.send_code_request(phone), NET_TIMEOUT)
            self._hash = sent.phone_code_hash
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            error = map_login_error(exc)
            log.warning("login start failed: %s", type(exc).__name__)
            await self.cancel()
            raise error from exc

    async def resend_code(self) -> None:
        self._check_alive()
        try:
            sent = await asyncio.wait_for(self._client.send_code_request(self._phone), NET_TIMEOUT)
            self._hash = sent.phone_code_hash
            self._code_tries = 0
        except Exception as exc:  # noqa: BLE001
            error = map_login_error(exc)
            if error.terminal:
                await self.cancel()
            raise error from exc

    async def submit_code(self, raw_code: str) -> LoginOutcome:
        self._check_alive()
        try:
            code = parse_login_code(raw_code)
        except ValidationError as exc:
            raise LoginError(f"❌ {exc}") from exc
        self._code_tries += 1
        try:
            await asyncio.wait_for(
                self._client.sign_in(phone=self._phone, code=code, phone_code_hash=self._hash), NET_TIMEOUT)
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            if "SessionPasswordNeededError" in mro_names(exc):
                return LoginOutcome.NEED_PASSWORD
            error = map_login_error(exc)
            if not error.terminal and self._code_tries >= self.MAX_CODE_TRIES:
                await self.cancel()
                raise LoginError("Too many wrong codes. Login cancelled; start again.", terminal=True) from exc
            if error.terminal:
                await self.cancel()
            raise error from exc
        finally:
            del code  # drop the only reference to the code
        await self._finish()
        return LoginOutcome.DONE

    async def submit_password(self, password: str) -> LoginOutcome:
        self._check_alive()
        self._password_tries += 1
        try:
            await asyncio.wait_for(self._client.sign_in(password=password), NET_TIMEOUT)
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            error = map_login_error(exc)
            if not error.terminal and self._password_tries >= self.MAX_PASSWORD_TRIES:
                await self.cancel()
                raise LoginError("Too many wrong passwords. Login cancelled; start again.", terminal=True) from exc
            if error.terminal:
                await self.cancel()
            raise error from exc
        finally:
            del password
        await self._finish()
        return LoginOutcome.DONE

    async def cancel(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(client.disconnect(), 10)
        pending, self._pending = self._pending, None
        if pending is not None:
            for suffix in ("", "-journal", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError, OSError):
                    Path(str(pending) + suffix).unlink()
        self._hash = None

    # ------------------------------------------------------------------ internals
    def _check_alive(self) -> None:
        if self._client is None:
            raise LoginError("No login is in progress. Start again from the dashboard.", terminal=True)
        if self._clock() - self._started > self.TTL_SECONDS:
            raise LoginError("⌛ This login attempt timed out. Start again.", terminal=True)

    async def _finish(self) -> None:
        assert self._client is not None and self._pending is not None
        try:
            me = await asyncio.wait_for(self._client.get_me(), NET_TIMEOUT)
            if me is None:
                raise LoginError("Login did not complete. Start again.", terminal=True)
            await asyncio.wait_for(self._client.disconnect(), 10)  # release the session file before moving it
            self._client = None
            pending = self._pending
            self.result = await self._manager.register_session(
                user_id=int(me.id), username=getattr(me, "username", None), first_name=getattr(me, "first_name", None),
                phone=self._phone, pending_path=pending)
            self._pending = None
        except DuplicateAccount as dup:
            await self.cancel()
            raise LoginError(f"This account is already added as {dup.existing_label}.", terminal=True) from dup
        except LoginError:
            await self.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("login finish failed: %s", type(exc).__name__)
            await self.cancel()
            raise LoginError(f"⚠️ Could not save the session ({type(exc).__name__}).", terminal=True) from exc
        finally:
            self._phone = None
            self._hash = None
