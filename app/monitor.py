"""Availability monitor: one background task, FloodWait-aware, restorable after restart.

* Rotating accounts monitoring (all connected accounts with configured gap + random jitter).
* Auto-claims the username on the account itself when available and reports elapsed time.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import random
import time
from enum import Enum
from typing import Any, Callable, Dict, Optional

from .accounts import AccStatus
from .checker import CheckResult, State
from .errors import Category, ErrorInfo
from .timeutil import fmt_time, utcnow, now_iso
from .claim import change_username, ClaimOutcome

log = logging.getLogger(__name__)

WAITING_REPORT_AFTER = 60.0   # seconds without a usable account before the admin is told


class MonitorStatus(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    WAITING_ACCOUNT = "waiting_for_account"
    RATE_LIMITED = "rate_limited"


class Monitor:
    def __init__(self, *, repo: Any, manager: Any, checker: Any, notifier: Any, tz: Any = None,
                 min_interval: int = 5, max_interval: int = 3600, idle_poll: float = 5.0,
                 mono: Callable[[], float] = time.monotonic,
                 availability_markup: Optional[Callable[[str], Any]] = None) -> None:
        self.repo = repo
        self.manager = manager
        self.checker = checker
        self.notifier = notifier
        self.tz = tz
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.idle_poll = idle_poll
        self._mono = mono
        self._markup = availability_markup
        self.status = MonitorStatus.STOPPED
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self.last_check_wall: Optional[str] = None

    # ------------------------------------------------------------------ control
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """Start sniping the current target automatically. Returns False if already running / no target."""
        if self.running:
            return False
        if await self.repo.get_current_target() is None:
            return False
        await self.repo.set_bool("monitoring_enabled", True)
        self._task = asyncio.create_task(self._run(), name="monitor")
        return True

    async def stop(self) -> None:
        """Administrator stop: persisted, so it stays stopped after a restart."""
        await self.repo.set_bool("monitoring_enabled", False)
        await self._cancel()

    async def shutdown(self) -> None:
        """Process shutdown: cancel the task but keep the persisted flag for the next start."""
        await self._cancel()

    async def restart(self) -> bool:
        await self._cancel()
        return await self.start()

    async def resume_from_db(self) -> bool:
        if await self.repo.get_bool("monitoring_enabled") and await self.repo.get_current_target() is not None:
            log.info("restoring sniper from saved configuration")
            return await self.start()
        return False

    def wake(self) -> None:
        self._wake.set()

    async def _cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.status = MonitorStatus.STOPPED

    # ------------------------------------------------------------------ loop
    async def _sleep_or_wake(self, delay: float) -> None:
        self._wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, delay))

    async def _pick_runners(self) -> list[Any]:
        """Pick all connected, non-permanent accounts automatically for full sequence rotation."""
        runners = []
        for runner in self.manager.all():
            if not runner.permanent and runner.status is AccStatus.CONNECTED:
                if runner.flood_remaining() <= 0:
                    runners.append(runner)
        return runners

    async def _run(self) -> None:
        log.info("sniper rotation loop started with full sequence rotation and human jitter")
        waiting_since: Optional[float] = None
        start_wall_time = time.time()  # Track elapsed time

        while True:
            try:
                target = await self.repo.get_current_target()
                if target is None:
                    self.status = MonitorStatus.STOPPED
                    return

                runners = await self._pick_runners()
                if not runners:
                    self.status = MonitorStatus.WAITING_ACCOUNT
                    now = self._mono()
                    waiting_since = waiting_since if waiting_since is not None else now
                    if now - waiting_since >= WAITING_REPORT_AFTER:
                        await self._report_waiting(None)
                    await self._sleep_or_wake(self.idle_poll)
                    continue
                waiting_since = None

                # Fetch configured interval gap from repo (user selected seconds)
                base_interval = await self.repo.get_int("interval_seconds", 30)

                # Rotate through all connected runners in sequence automatically
                for index, runner in enumerate(runners):
                    target = await self.repo.get_current_target()
                    if target is None:
                        self.status = MonitorStatus.STOPPED
                        return

                    gate = runner.flood_remaining()
                    if gate > 0:
                        continue

                    self.status = MonitorStatus.RUNNING
                    result = await self.checker.check(runner, target["username"])
                    self.last_check_wall = now_iso()

                    elapsed_seconds = time.time() - start_wall_time
                    await self._record(target, runner, result, elapsed_seconds)

                    if result.state is State.AVAILABLE:
                        log.info(f"Username @{target['username']} secured instantly! Stopping sniper.")
                        await self.repo.set_bool("monitoring_enabled", False)
                        self.status = MonitorStatus.STOPPED
                        return

                    if result.invalid:
                        await self._stop_invalid(target)
                        return

                    # Add human-like random jitter (1 to 2 seconds extra) to the configured gap
                    jitter = random.uniform(1.0, 2.0)
                    delay = float(base_interval) + jitter

                    # Wait before checking with the next account in sequence rotation
                    await self._sleep_or_wake(delay)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("sniper iteration failed")
                await self._report_unexpected(exc)
                await self._sleep_or_wake(10.0)

    # ------------------------------------------------------------------ recording & auto-claim
    async def _record(self, target: Dict[str, Any], runner: Any, result: CheckResult, elapsed_seconds: float) -> None:
        now = now_iso()
        fields: Dict[str, Any] = {
            "state": result.state.value,
            "last_check_at": now,
            "checks_total": int(target.get("checks_total") or 0) + 1,
        }
        previous_state = target.get("state")
        previous_confirmed = target.get("confirmed_state")
        became_available = False
        became_unavailable_from_available = False

        if result.state in (State.AVAILABLE, State.UNAVAILABLE):
            fields["last_success_at"] = now
            fields["confirmed_state"] = result.state.value
            if previous_confirmed != result.state.value:
                fields["state_changed_at"] = now
                became_available = result.state is State.AVAILABLE
                became_unavailable_from_available = (
                    result.state is State.UNAVAILABLE and previous_confirmed == State.AVAILABLE.value)
        else:
            fields["last_error"] = result.detail[:300]
            fields["last_error_at"] = now

        await self.repo.update_target(target["id"], **fields)
        if previous_state != result.state.value:
            await self.repo.add_history(target["id"], result.state.value, result.detail[:300], runner.account_id)

        if became_available:
            # AUTO-CLAIM ON THE ACCOUNT IMMEDIATELY
            claim_success = False
            claim_msg = ""
            try:
                claim_result = await change_username(runner, runner.account_id, target["username"])
                claim_success = claim_result.ok
                claim_msg = claim_result.message
            except Exception as e:
                claim_msg = str(e)
                log.exception(f"Failed to auto-claim username on account {runner.account_id}")

            await self._notify_acquired(target["username"], elapsed_seconds, claim_success, claim_msg, runner.safe_id)

        elif became_unavailable_from_available:
            await self.notifier.send(
                f"🔴 <b>@{html.escape(target['username'])}</b> is no longer available "
                f"(taken or withdrawn) as of {fmt_time(now, self.tz)}.")

    async def _notify_acquired(self, username: str, elapsed_seconds: float, claimed: bool, message: str, account_label: str) -> None:
        mins, secs = divmod(int(elapsed_seconds), 60)
        time_str = f"{mins}m {secs}s" if mins > 0 else f"{elapsed_seconds:.2f} seconds"

        status_text = f"✅ <b>Auto-Claim Result:</b> {html.escape(message)}" if claimed else f"⚠️ <b>Available, but claim issue:</b> {html.escape(message)}"

        text = (
            f"🟢 <b>@{html.escape(username)} was AVAILABLE and targeted!</b>\n"
            f"⏱️ <b>Time taken:</b> {time_str}\n"
            f"👤 <b>Account used:</b> {html.escape(str(account_label))}\n"
            f"{status_text}\n\n"
            f"Secured at: {fmt_time(utcnow(), self.tz)}"
        )
        markup = self._markup(username) if self._markup else None
        await self.notifier.send(text, markup)

    async def _stop_invalid(self, target: Dict[str, Any]) -> None:
        await self.repo.set_bool("monitoring_enabled", False)
        self.status = MonitorStatus.STOPPED
        info = ErrorInfo(Category.INVALID_USERNAME, f"Telegram rejected @{target['username']}")
        await self.notifier.notify_error(account_label=None, account_id=None, op="check_username", info=info,
                                         status="sniping stopped")

    async def _report_waiting(self, runner: Any) -> None:
        reason = "no connected accounts available for sequence rotation" if runner is None else (
            f"account {runner.safe_id} is {runner.status.value}")
        info = ErrorInfo(Category.CONNECTION, f"Sniping paused: {reason}. Add or fix accounts.")
        await self.notifier.notify_error(account_label=getattr(runner, "safe_id", None),
                                         account_id=getattr(runner, "account_id", None), op="sniper",
                                         status=self.status.value)

    async def _report_unexpected(self, exc: BaseException) -> None:
        from .errors import classify

        info = classify(exc)
        with contextlib.suppress(Exception):
            await self.notifier.notify_error(account_label=None, account_id=None, op="sniper_loop",
                                             info=info, status=self.status.value)
