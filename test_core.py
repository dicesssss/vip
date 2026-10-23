"""Offline tests for the core logic. Run: python -m unittest discover -s tests -v

Uses only the standard library and fakes: no Telegram, Telethon or aiogram needed.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import storage
from app.accounts import AccStatus, AccountRunner, RateLimited
from app.backoff import Backoff
from app.checker import State, UsernameChecker
from app.config import ConfigError, load_config
from app.db import Database, DatabaseError
from app.errors import Category, classify
from app.logging_setup import REDACTOR, redact
from app.monitor import Monitor
from app.notifier import Notifier
from app.repo import Repo
from app.validators import ValidationError, mask_phone, normalize_username, parse_interval, parse_phone

GOOD_ENV = {
    "BOT_TOKEN": "123456789:" + "A" * 35,
    "ADMIN_ID": "42",
    "API_ID": "12345",
    "API_HASH": "a" * 32,
    "DATA_DIR": "/tmp/x",
    "REQUIRE_MOUNTED_VOLUME": "false",
}


# Fake exception classes named like Telethon's (classification is by class name).
class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


class UsernameNotOccupiedError(Exception):
    pass


class UsernameInvalidError(Exception):
    pass


class AuthKeyUnregisteredError(Exception):
    pass


def run(coro):
    return asyncio.run(coro)


class ConfigTests(unittest.TestCase):
    def test_valid(self):
        cfg = load_config(GOOD_ENV)
        self.assertEqual(cfg.admin_id, 42)
        self.assertNotIn("AAAA", repr(cfg))  # secrets are not in repr

    def test_missing_and_bad(self):
        with self.assertRaises(ConfigError) as cm:
            load_config({})
        self.assertIn("BOT_TOKEN is not set", str(cm.exception))
        bad = dict(GOOD_ENV, API_HASH="zzz", DATA_DIR="relative")
        with self.assertRaises(ConfigError) as cm:
            load_config(bad)
        self.assertNotIn("zzz", str(cm.exception))  # the value is never echoed


class ValidatorTests(unittest.TestCase):
    def test_usernames(self):
        self.assertEqual(normalize_username("@Hello_World"), "hello_world")
        self.assertEqual(normalize_username("https://t.me/abcde"), "abcde")
        for bad in ["", "@abc", "1abcde", "abcde_", "ab__cde", "abc-de", "a" * 33]:
            with self.assertRaises(ValidationError, msg=bad):
                normalize_username(bad)

    def test_interval(self):
        self.assertEqual(parse_interval("45", 5, 3600), 45)
        self.assertEqual(parse_interval("10s", 5, 3600), 10)
        for bad in ["4", "3601", "abc", "-5", "1.5", ""]:
            with self.assertRaises(ValidationError, msg=bad):
                parse_interval(bad, 5, 3600)

    def test_phone(self):
        self.assertEqual(parse_phone("+91 98765-43210"), "+919876543210")
        with self.assertRaises(ValidationError):
            parse_phone("12")
        self.assertNotIn("98765", mask_phone("+919876543210"))


class ErrorTests(unittest.TestCase):
    def test_classification(self):
        i = classify(FloodWaitError(37))
        self.assertEqual((i.category, i.wait_seconds), (Category.FLOOD_WAIT, 37))
        self.assertTrue(classify(AuthKeyUnregisteredError("x")).permanent)
        self.assertEqual(classify(UsernameInvalidError("x")).category, Category.INVALID_USERNAME)
        self.assertEqual(classify(ConnectionError("x")).category, Category.CONNECTION)
        self.assertEqual(classify(ValueError("x")).category, Category.UNEXPECTED)

    def test_backoff_bounded(self):
        b = Backoff(base=2, cap=30, jitter=0)
        delays = [b.next() for _ in range(10)]
        self.assertEqual(delays[:4], [2, 4, 8, 16])
        self.assertTrue(all(d <= 30 for d in delays))
        b.reset()
        self.assertEqual(b.next(), 2)


class RedactionTests(unittest.TestCase):
    def test_redacts(self):
        token = "123456789:" + "B" * 35
        self.assertNotIn(token, redact(f"url /bot{token}/getMe"))
        self.assertNotIn("a" * 32, redact("hash " + "a" * 32))
        self.assertNotIn("919876543210", redact("phone +919876543210"))
        REDACTOR.add_secret("supersecretvalue")
        self.assertNotIn("supersecretvalue", redact("x supersecretvalue y"))


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_migrate_idempotent_and_data_survives_restart(self):
        async def go():
            db = Database(self.tmp / "m.db")
            db.open()
            self.assertEqual(await db.migrate(self.tmp / "b"), 1)
            repo = Repo(db)
            await repo.set_setting("interval_seconds", "15")
            await db.close()
            db2 = Database(self.tmp / "m.db")
            db2.open()
            self.assertEqual(await db2.migrate(self.tmp / "b"), 1)  # second start: no-op
            self.assertEqual(await Repo(db2).get_setting("interval_seconds"), "15")
            ok, _ = await db2.integrity_check()
            self.assertTrue(ok)
            await db2.close()

        run(go())

    def test_future_schema_refused(self):
        async def go():
            db = Database(self.tmp / "f.db")
            db.open()
            await db.migrate(self.tmp / "b")
            await db.execute("INSERT INTO schema_migrations VALUES (99,'future','now')")
            with self.assertRaises(DatabaseError):
                await db.migrate(self.tmp / "b")
            await db.close()

        run(go())

    def test_corrupt_file_not_deleted(self):
        p = self.tmp / "bad.db"
        p.write_bytes(b"this is not sqlite" * 100)
        db = Database(p)
        with self.assertRaises(Exception):
            db.open()
            run(db.integrity_check())
        self.assertTrue(p.exists())  # never erased

    def test_rollback_on_error(self):
        async def go():
            db = Database(self.tmp / "r.db")
            db.open()
            await db.migrate(self.tmp / "b")

            def bad(conn):
                conn.execute("INSERT INTO settings VALUES ('k','v')")
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                await db.run(bad)
            self.assertIsNone(await db.fetchone("SELECT * FROM settings WHERE key='k'"))
            await db.close()

        run(go())


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cfg(self, **over):
        env = dict(GOOD_ENV, DATA_DIR=str(self.tmp / "data"), **over)
        return load_config(env)

    def test_prepares_and_keeps_marker_and_sessions(self):
        cfg = self.cfg()
        info1 = storage.verify_and_prepare(cfg, env={})
        (cfg.sessions_dir / "1.session").write_bytes(b"keep")
        (cfg.pending_dir / "stale.session").write_bytes(b"x")
        info2 = storage.verify_and_prepare(cfg, env={})
        self.assertEqual(info1.volume_id, info2.volume_id)  # not erased at startup
        self.assertTrue((cfg.sessions_dir / "1.session").exists())
        self.assertFalse((cfg.pending_dir / "stale.session").exists())

    def test_required_volume_missing_fails_loudly(self):
        cfg = self.cfg(REQUIRE_MOUNTED_VOLUME="true")
        with self.assertRaises(storage.StorageError) as cm:
            storage.verify_and_prepare(cfg, env={})
        self.assertIn("does not exist", str(cm.exception))

    def test_railway_without_volume_fails(self):
        cfg = self.cfg(REQUIRE_MOUNTED_VOLUME="true")
        with self.assertRaises(storage.StorageError) as cm:
            storage.verify_and_prepare(cfg, env={"RAILWAY_ENVIRONMENT": "production"})
        self.assertIn("No Railway volume", str(cm.exception))

    @unittest.skipIf(os.geteuid() == 0 if hasattr(os, "geteuid") else False, "root ignores permissions")
    def test_unwritable(self):
        cfg = self.cfg()
        cfg.data_dir.mkdir()
        cfg.data_dir.chmod(0o500)
        self.addCleanup(cfg.data_dir.chmod, 0o700)
        with self.assertRaises(storage.StorageError):
            storage.verify_and_prepare(cfg, env={})

    def test_instance_lock(self):
        async def go():
            a, b = storage.InstanceLock(self.tmp / "l"), storage.InstanceLock(self.tmp / "l")
            await a.acquire()
            with self.assertRaises(storage.StorageError):
                await b.acquire(timeout=0.3, poll=0.1)
            a.release()
            await b.acquire(timeout=1)
            b.release()

        run(go())


# --------------------------------------------------------------------------- fakes for runner/checker/monitor
class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += timedelta(seconds=s)


class FakeRepo:
    def __init__(self):
        self.s = {"interval_seconds": "5", "monitor_account_id": "", "monitoring_enabled": "0"}
        self.target = {"id": 1, "username": "target_name", "state": "unknown", "confirmed_state": None,
                       "checks_total": 0}
        self.events, self.history, self.statuses = [], [], []
        self.flood = None

    async def get_setting(self, k): return self.s.get(k, "")
    async def set_setting(self, k, v): self.s[k] = str(v)
    async def get_bool(self, k): return self.s.get(k) == "1"
    async def set_bool(self, k, v): self.s[k] = "1" if v else "0"
    async def get_int(self, k, d): return int(self.s.get(k) or d)
    async def get_current_target(self): return self.target
    async def update_target(self, tid, **f): self.target.update(f)
    async def add_history(self, tid, state, detail, aid): self.history.append(state)
    async def add_event(self, **kw): self.events.append(kw)
    async def update_account_status(self, *a, **k): self.statuses.append(a[1])
    async def set_flood_until(self, aid, iso): self.flood = iso


class FakeNotifier:
    def __init__(self): self.errors, self.messages = [], []
    async def notify_error(self, **kw): self.errors.append(kw)
    async def send(self, text, markup=None): self.messages.append(text)


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.connected = True
    def is_connected(self): return self.connected
    async def __call__(self, req):
        self.calls.append(req)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_runner(client, clock, repo=None, notifier=None):
    row = {"id": 7, "username": "acct", "label": "@acct", "session_file": "7.session",
           "permanent_failure": 0, "flood_until": None}
    r = AccountRunner(row=row, sessions_dir=Path("/tmp"), client_factory=lambda p: client,
                      repo=repo or FakeRepo(), notifier=notifier or FakeNotifier(), clock=clock)
    r.client, r.status = client, AccStatus.CONNECTED
    return r


class FloodWaitTests(unittest.TestCase):
    def test_flood_blocks_requests_until_elapsed(self):
        async def go():
            clock, repo, note = FakeClock(), FakeRepo(), FakeNotifier()
            client = FakeClient([FloodWaitError(30), True])
            r = make_runner(client, clock, repo, note)
            with self.assertRaises(RateLimited):
                await r.request("check_username", "req1")
            self.assertEqual(len(client.calls), 1)
            # inside the window: refused locally, Telegram is NOT contacted
            clock.advance(20)
            with self.assertRaises(RateLimited):
                await r.request("check_username", "req2")
            self.assertEqual(len(client.calls), 1)
            # after the window (+1s margin) requests flow again
            clock.advance(12)
            self.assertTrue(await r.request("check_username", "req3"))
            self.assertEqual(len(client.calls), 2)
            # exactly one notification with the required fields
            self.assertEqual(len(note.errors), 1)
            e = note.errors[0]
            self.assertEqual(e["op"], "check_username")
            self.assertEqual(e["info"].wait_seconds, 30)
            self.assertIsNotNone(e["next_retry"])
            self.assertEqual(repo.events[0]["kind"], "flood_wait")
            self.assertIsNotNone(repo.flood)  # persisted so a restart still honours it

        run(go())

    def test_permanent_auth_failure_stops(self):
        async def go():
            r = make_runner(FakeClient([]), FakeClock())
            await r.report_error("check_username", AuthKeyUnregisteredError("revoked"))
            self.assertTrue(r.permanent)
            self.assertEqual(r.status, AccStatus.AUTH_FAILED)
            await r.start()  # must not spawn a retry task
            self.assertIsNone(r._task)

        run(go())


class CheckerTests(unittest.TestCase):
    def check(self, script):
        async def go():
            r = make_runner(FakeClient(script), FakeClock())
            return await UsernameChecker(lambda u: "check", lambda u: "resolve").check(r, "target_name")

        return run(go())

    def test_available_needs_both_signals(self):
        res = self.check([True, UsernameNotOccupiedError()])
        self.assertEqual(res.state, State.AVAILABLE)

    def test_free_but_resolves_to_holder_is_unknown(self):
        self.assertEqual(self.check([True, object()]).state, State.UNKNOWN)

    def test_unexpected_response_is_never_available(self):
        self.assertEqual(self.check(["weird"]).state, State.UNKNOWN)
        self.assertEqual(self.check([None]).state, State.UNKNOWN)

    def test_taken(self):
        self.assertEqual(self.check([False]).state, State.UNAVAILABLE)

    def test_invalid_username_flagged(self):
        res = self.check([UsernameInvalidError()])
        self.assertTrue(res.invalid)
        self.assertNotEqual(res.state, State.AVAILABLE)

    def test_flood_is_rate_limited_not_available(self):
        res = self.check([FloodWaitError(10)])
        self.assertEqual(res.state, State.RATE_LIMITED)
        self.assertIsNotNone(res.wait_until)

    def test_random_exception_is_unknown_and_reported(self):
        res = self.check([RuntimeError("boom")])
        self.assertEqual(res.state, State.UNKNOWN)
        self.assertTrue(res.error_reported)


class FakeManager:
    def __init__(self, runner): self.runner = runner
    def get(self, i): return self.runner if i == self.runner.account_id else None
    def all(self): return [self.runner]


class FakeChecker:
    def __init__(self, results):
        self.results, self.calls = list(results), 0
    async def check(self, runner, username):
        self.calls += 1
        await asyncio.sleep(0.01)  # real checks await the network; never starve the loop
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]  # repeat the last answer


class MonitorTests(unittest.TestCase):
    def test_single_task_notify_once_and_resume_flag(self):
        from app.checker import CheckResult

        async def go():
            repo, note = FakeRepo(), FakeNotifier()
            runner = make_runner(FakeClient([]), FakeClock(), repo, note)
            checker = FakeChecker([CheckResult(State.UNAVAILABLE, "taken"),
                                   CheckResult(State.AVAILABLE, "both agree"),
                                   CheckResult(State.AVAILABLE, "both agree")] + [CheckResult(State.AVAILABLE, "x")] * 50)
            mon = Monitor(repo=repo, manager=FakeManager(runner), checker=checker, notifier=note,
                          min_interval=0, idle_poll=0.01)
            repo.s["interval_seconds"] = "0"
            self.assertTrue(await mon.start())
            self.assertFalse(await mon.start())  # duplicate start refused
            await asyncio.sleep(0.3)
            await mon.shutdown()  # process shutdown keeps the persisted flag
            self.assertEqual(repo.s["monitoring_enabled"], "1")
            self.assertGreaterEqual(checker.calls, 3)
            avail = [m for m in note.messages if "AVAILABLE" in m]
            self.assertEqual(len(avail), 1)  # only the transition notifies
            self.assertIn("not a reservation", avail[0])
            await mon.stop()  # admin stop clears it
            self.assertEqual(repo.s["monitoring_enabled"], "0")

        run(go())

    def test_waits_during_flood_and_never_switches_account(self):
        async def go():
            repo, note, clock = FakeRepo(), FakeNotifier(), FakeClock()
            runner = make_runner(FakeClient([]), clock, repo, note)
            runner.flood_until = clock() + timedelta(hours=1)
            checker = FakeChecker([])
            mon = Monitor(repo=repo, manager=FakeManager(runner), checker=checker, notifier=note,
                          min_interval=0, idle_poll=0.01)
            await mon.start()
            await asyncio.sleep(0.15)
            self.assertEqual(checker.calls, 0)  # no check while rate-limited
            self.assertEqual(mon.status.value, "rate_limited")
            await mon.shutdown()

        run(go())

    def test_invalid_username_stops_monitoring(self):
        from app.checker import CheckResult

        async def go():
            repo, note = FakeRepo(), FakeNotifier()
            runner = make_runner(FakeClient([]), FakeClock(), repo, note)
            checker = FakeChecker([CheckResult(State.UNKNOWN, "invalid", invalid=True)])
            mon = Monitor(repo=repo, manager=FakeManager(runner), checker=checker, notifier=note,
                          min_interval=0, idle_poll=0.01)
            repo.s["interval_seconds"] = "0"
            await mon.start()
            await asyncio.sleep(0.15)
            self.assertFalse(mon.running)
            self.assertEqual(repo.s["monitoring_enabled"], "0")
            self.assertEqual(note.errors[0]["info"].category, Category.INVALID_USERNAME)

        run(go())


class NotifierTests(unittest.TestCase):
    def test_identical_errors_grouped(self):
        async def go():
            sent = []

            async def send(text, markup): sent.append(text)

            repo = FakeRepo()
            repo.s.update(notify_flood="1", notify_errors="1")
            t = [0.0]
            n = Notifier(send, repo, cooldown=600, now=lambda: t[0])
            info = classify(ConnectionError("down"))
            kw = dict(account_label="a", account_id=1, op="connect", info=info, status="disconnected")
            self.assertTrue(await n.notify_error(**kw))
            for _ in range(5):
                self.assertFalse(await n.notify_error(**kw))
            t[0] = 601
            self.assertTrue(await n.notify_error(**kw))
            self.assertIn("5 identical", sent[-1])
            # disabled category stays silent, authorization always goes through
            repo.s["notify_errors"] = "0"
            t[0] = 5000
            self.assertFalse(await n.notify_error(**kw))
            auth = classify(AuthKeyUnregisteredError("x"))
            self.assertTrue(await n.notify_error(**dict(kw, info=auth)))

        run(go())


if __name__ == "__main__":
    unittest.main()
