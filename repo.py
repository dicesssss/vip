"""Typed-ish data access on top of :class:`app.db.Database`."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .db import Database
from .timeutil import now_iso

DEFAULT_SETTINGS: Dict[str, str] = {
    "interval_seconds": "30",
    "monitor_account_id": "",
    "monitoring_enabled": "0",
    "notify_flood": "1",
    "notify_errors": "1",
    "current_target_id": "",
}

_TARGET_FIELDS = {
    "state", "confirmed_state", "state_changed_at", "last_check_at", "last_success_at",
    "last_error", "last_error_at", "checks_total",
}

HISTORY_KEEP = 2000
EVENTS_KEEP = 1000


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ settings
    async def get_setting(self, key: str) -> str:
        row = await self.db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        if row is not None:
            return str(row["value"])
        return DEFAULT_SETTINGS.get(key, "")

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    async def get_bool(self, key: str) -> bool:
        return (await self.get_setting(key)) == "1"

    async def set_bool(self, key: str, value: bool) -> None:
        await self.set_setting(key, "1" if value else "0")

    async def get_int(self, key: str, default: int) -> int:
        raw = await self.get_setting(key)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------ accounts
    async def upsert_account(self, *, account_id: int, label: str, username: Optional[str],
                             first_name: Optional[str], phone_hint: Optional[str], session_file: str) -> None:
        await self.db.execute(
            """INSERT INTO accounts(id,label,username,first_name,phone_hint,session_file,status,permanent_failure,created_at)
               VALUES(?,?,?,?,?,?, 'disconnected', 0, ?)
               ON CONFLICT(id) DO UPDATE SET label=excluded.label, username=excluded.username,
                 first_name=excluded.first_name, phone_hint=excluded.phone_hint,
                 session_file=excluded.session_file, status='disconnected', status_detail=NULL,
                 permanent_failure=0, flood_until=NULL""",
            (account_id, label, username, first_name, phone_hint, session_file, now_iso()),
        )

    async def get_account(self, account_id: int) -> Optional[Dict[str, Any]]:
        return await self.db.fetchone("SELECT * FROM accounts WHERE id=?", (account_id,))

    async def list_accounts(self) -> List[Dict[str, Any]]:
        return await self.db.fetchall("SELECT * FROM accounts ORDER BY id")

    async def delete_account(self, account_id: int) -> None:
        await self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    async def update_account_status(self, account_id: int, status: str, detail: Optional[str] = None, *,
                                    permanent: Optional[bool] = None, connected: bool = False) -> None:
        sets = ["status=?", "status_detail=?"]
        params: List[Any] = [status, detail]
        if permanent is not None:
            sets.append("permanent_failure=?")
            params.append(1 if permanent else 0)
        if connected:
            sets.append("last_connected_at=?")
            params.append(now_iso())
        params.append(account_id)
        await self.db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", params)

    async def set_flood_until(self, account_id: int, iso: Optional[str]) -> None:
        await self.db.execute("UPDATE accounts SET flood_until=? WHERE id=?", (iso, account_id))

    async def update_account_identity(self, account_id: int, *, label: str, username: Optional[str],
                                      first_name: Optional[str]) -> None:
        await self.db.execute(
            "UPDATE accounts SET label=?, username=?, first_name=? WHERE id=?",
            (label, username, first_name, account_id),
        )

    # ------------------------------------------------------------ targets
    async def get_or_create_target(self, username: str) -> Dict[str, Any]:
        await self.db.execute(
            "INSERT INTO targets(username, created_at) VALUES(?,?) ON CONFLICT(username) DO NOTHING",
            (username, now_iso()),
        )
        row = await self.db.fetchone("SELECT * FROM targets WHERE username=?", (username,))
        assert row is not None
        return row

    async def get_target(self, target_id: int) -> Optional[Dict[str, Any]]:
        return await self.db.fetchone("SELECT * FROM targets WHERE id=?", (target_id,))

    async def get_current_target(self) -> Optional[Dict[str, Any]]:
        raw = await self.get_setting("current_target_id")
        if not raw:
            return None
        try:
            return await self.get_target(int(raw))
        except ValueError:
            return None

    async def set_current_target(self, target_id: Optional[int]) -> None:
        await self.set_setting("current_target_id", "" if target_id is None else str(target_id))

    async def update_target(self, target_id: int, **fields: Any) -> None:
        bad = set(fields) - _TARGET_FIELDS
        if bad:
            raise ValueError(f"unknown target fields: {sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE targets SET {cols} WHERE id=?", [*fields.values(), target_id])

    # ------------------------------------------------------------ history / events
    async def add_history(self, target_id: int, state: str, detail: Optional[str], account_id: Optional[int]) -> None:
        def _do(conn: Any) -> None:
            conn.execute(
                "INSERT INTO history(target_id,ts,state,detail,account_id) VALUES(?,?,?,?,?)",
                (target_id, now_iso(), state, detail, account_id),
            )
            conn.execute(
                "DELETE FROM history WHERE target_id=? AND id <= "
                "(SELECT id FROM history WHERE target_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (target_id, target_id, HISTORY_KEEP),
            )

        await self.db.run(_do)

    async def list_history(self, target_id: int, limit: int = 15) -> List[Dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT * FROM history WHERE target_id=? ORDER BY id DESC LIMIT ?", (target_id, limit)
        )

    async def add_event(self, *, kind: str, op: str, category: str, account_id: Optional[int],
                        wait_seconds: Optional[int], detail: Optional[str], next_retry_at: Optional[str]) -> None:
        def _do(conn: Any) -> None:
            conn.execute(
                "INSERT INTO events(ts,account_id,kind,op,category,wait_seconds,detail,next_retry_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (now_iso(), account_id, kind, op, category, wait_seconds, detail, next_retry_at),
            )
            conn.execute(
                "DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (EVENTS_KEEP,),
            )

        await self.db.run(_do)

    async def recent_events(self, kind: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        if kind:
            return await self.db.fetchall("SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        return await self.db.fetchall("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
