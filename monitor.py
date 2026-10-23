"""Availability monitor: one background task, FloodWait-aware, restorable after restart.

* Exactly one monitoring task exists at any time (``start`` is idempotent).
* One fixed monitoring account is used. If it is rate-limited the monitor WAITS;
  it never switches to another account to get around a limit.
* The interval is honoured between checks; a settings change only wakes the loop so
  the new interval applies from the last check onward (never an extra burst).
* ``shutdown()`` (process exit) keeps the persisted "enabled" flag so monitoring is
  restored on the next start; ``stop()`` (admin action) clears it.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from enum import Enum
from typing import Any, Callable, Dict, Optional

from .accounts import AccStatus
from .checker import CheckResult, State
from .errors import Category, ErrorInfo
from .timeutil import fmt_time, utcnow, now_iso

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
        """Start monitoring the current target. Returns False if already running / no target."""
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
            log.info("restoring monitoring from saved configuration")
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

    async def _interval(self) -> int:
        value = await self.repo.get_int("interval_seconds", 30)
        return max(self.min_interval, min(self.max_interval, value))

    async def _pick_runner(self) -> Optional[Any]:
        raw = await self.repo.get_setting("monitor_account_id")
        if raw:
            try:
                return self.manager.get(int(raw))
            except ValueError:
                return None
        for runner in self.manager.all():  # deterministic: lowest id that is not permanently failed
            if not runner.permanent:
                await self.repo.set_setting("monitor_account_id", str(runner.account_id))
                return runner
        return None

    async def _run(self) -> None:
        log.info("monitor loop started")
        last_check: Optional[float] = None
        waiting_since: Optional[float] = None
        interval = self.min_interval
        while True:
            try:
                target = await self.repo.get_current_target()
                if target is None:
                    self.status = MonitorStatus.STOPPED
                    return
                interval = await self._interval()
                runner = await self._pick_runner()

                if runner is None or runner.status is not AccStatus.CONNECTED:
                    self.status = MonitorStatus.WAITING_ACCOUNT
                    now = self._mono()
                    waiting_since = waiting_since if waiting_since is not None else now
                    if now - waiting_since >= WAITING_REPORT_AFTER:
                        await self._report_waiting(runner)
                    await self._sleep_or_wake(self.idle_poll)
                    continue
                waiting_since = None

                gate = runner.flood_remaining()
                if gate > 0:
                    self.status = MonitorStatus.RATE_LIMITED
                    await self._sleep_or_wake(gate)
                    continue

                if last_check is not None:
                    remaining = interval - (self._mono() - last_check)
                    if remaining > 0:
                        if self.status is not MonitorStatus.RATE_LIMITED:
                            self.status = MonitorStatus.RUNNING
                        await self._sleep_or_wake(remaining)
                        continue

                result = await self.checker.check(runner, target["username"])
                last_check = self._mono()
                self.last_check_wall = now_iso()
                await self._record(target, runner, result)
                if result.invalid:
                    await self._stop_invalid(target)
                    return
                self.status = MonitorStatus.RATE_LIMITED if result.state is State.RATE_LIMITED else MonitorStatus.RUNNING
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("monitor iteration failed")
                last_check = self._mono()
                await self._report_unexpected(exc)
                await self._sleep_or_wake(max(interval, 5))

    # ------------------------------------------------------------------ recording
    async def _record(self, target: Dict[str, Any], runner: Any, result: CheckResult) -> None:
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
            await self._notify_available(target["username"], result.detail)
        elif became_unavailable_from_available:
            await self.notifier.send(
                f"🔴 <b>@{html.escape(target['username'])}</b> is no longer available "
                f"(taken or withdrawn) as of {fmt_time(now, self.tz)}.")

    async def _notify_available(self, username: str, detail: str) -> None:
        text = (
            f"🟢 <b>@{html.escape(username)} appears AVAILABLE</b>\n"
            f"Checked: {fmt_time(utcnow(), self.tz)}\n"
            f"Basis: {html.escape(detail)}\n\n"
            "⚠️ This is a snapshot, not a reservation. Someone else may claim it before you do, and Telegram "
            "may still refuse the change. Nothing has been claimed automatically."
        )
        markup = self._markup(username) if self._markup else None
        await self.notifier.send(text, markup)

    async def _stop_invalid(self, target: Dict[str, Any]) -> None:
        await self.repo.set_bool("monitoring_enabled", False)
        self.status = MonitorStatus.STOPPED
        info = ErrorInfo(Category.INVALID_USERNAME, f"Telegram rejected @{target['username']}")
        await self.notifier.notify_error(account_label=None, account_id=None, op="check_username", info=info,
                                         status="monitoring stopped")

    async def _report_waiting(self, runner: Any) -> None:
        reason = "no monitoring account has been added" if runner is None else (
            f"monitoring account {runner.safe_id} is {runner.status.value}")
        info = ErrorInfo(Category.CONNECTION, f"Monitoring paused: {reason}. Choose or fix the account; "
                                              "the monitor does not switch accounts by itself.")
        await self.notifier.notify_error(account_label=getattr(runner, "safe_id", None),
                                         account_id=getattr(runner, "account_id", None), op="monitor",
                                         info=info, status=self.status.value)

    async def _report_unexpected(self, exc: BaseException) -> None:
        from .errors import classify

        info = classify(exc)
        with contextlib.suppress(Exception):
            await self.notifier.notify_error(account_label=None, account_id=None, op="monitor_loop",
                                             info=info, status=self.status.value)
