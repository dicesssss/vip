"""SQLite access: one connection, serialised through a lock, run in a worker thread.

* WAL + synchronous=FULL + foreign keys + busy timeout.
* Explicit ``BEGIN IMMEDIATE`` transactions for every write.
* Versioned, transactional migrations with an automatic pre-migration backup.
* Startup never drops or recreates data.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


class DatabaseError(RuntimeError):
    pass


# (version, description, statements). Never edit an applied migration; append a new one.
MIGRATIONS: List[Tuple[int, str, List[str]]] = [
    (
        1,
        "initial schema",
        [
            """CREATE TABLE settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""",
            """CREATE TABLE accounts (
                id                 INTEGER PRIMARY KEY,           -- Telegram user id
                label              TEXT    NOT NULL,
                username           TEXT,
                first_name         TEXT,
                phone_hint         TEXT,                          -- masked, never the full number
                session_file       TEXT    NOT NULL,              -- file name inside the sessions dir
                status             TEXT    NOT NULL DEFAULT 'disconnected',
                status_detail      TEXT,
                permanent_failure  INTEGER NOT NULL DEFAULT 0,
                flood_until        TEXT,
                last_connected_at  TEXT,
                created_at         TEXT    NOT NULL
            )""",
            """CREATE TABLE targets (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                username         TEXT    NOT NULL UNIQUE,
                state            TEXT    NOT NULL DEFAULT 'unknown',
                confirmed_state  TEXT,
                state_changed_at TEXT,
                last_check_at    TEXT,
                last_success_at  TEXT,
                last_error       TEXT,
                last_error_at    TEXT,
                checks_total     INTEGER NOT NULL DEFAULT 0,
                created_at       TEXT    NOT NULL
            )""",
            """CREATE TABLE history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id  INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
                ts         TEXT    NOT NULL,
                state      TEXT    NOT NULL,
                detail     TEXT,
                account_id INTEGER
            )""",
            "CREATE INDEX idx_history_target ON history(target_id, id DESC)",
            """CREATE TABLE events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT    NOT NULL,
                account_id    INTEGER,
                kind          TEXT    NOT NULL,       -- flood_wait | error
                op            TEXT    NOT NULL,
                category      TEXT    NOT NULL,
                wait_seconds  INTEGER,
                detail        TEXT,
                next_retry_at TEXT
            )""",
            "CREATE INDEX idx_events_kind ON events(kind, id DESC)",
        ],
    ),
]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self.journal_mode = ""

    # ------------------------------------------------------------------ open/close
    def open(self) -> None:
        try:
            conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            self.journal_mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.DatabaseError as exc:
            raise DatabaseError(
                f"Cannot open the database at {self.path}: {exc}. The file was NOT modified or deleted; "
                "restore a backup (see docs/BACKUP_RESTORE.md) or inspect it manually."
            ) from exc
        if self.journal_mode != "wal":
            log.warning("SQLite WAL mode unavailable (got %r); continuing with that journal mode", self.journal_mode)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._conn = conn

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                if self._conn is not None:
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.Error:
                        pass
                    self._conn.close()
                    self._conn = None

        await asyncio.to_thread(_close)

    # ------------------------------------------------------------------ core API
    async def run(self, fn: Callable[[sqlite3.Connection], T], *, write: bool = True) -> T:
        def _call() -> T:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise DatabaseError("database is closed")
                if not write:
                    return fn(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(conn)
                    conn.execute("COMMIT")
                    return result
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise

        return await asyncio.to_thread(_call)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(sql, params)
            return int(cur.lastrowid or 0)

        return await self.run(_do)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

        return await self.run(_do, write=False)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

        return await self.run(_do, write=False)

    # ------------------------------------------------------------------ integrity & migrations
    async def integrity_check(self, quick: bool = False) -> Tuple[bool, List[str]]:
        pragma = "quick_check" if quick else "integrity_check"

        def _do(conn: sqlite3.Connection) -> Tuple[bool, List[str]]:
            msgs = [str(r[0]) for r in conn.execute(f"PRAGMA {pragma}").fetchall()]
            return msgs == ["ok"], msgs

        return await self.run(_do, write=False)

    async def migrate(self, backup_dir: Path) -> int:
        return await asyncio.to_thread(self._migrate_sync, Path(backup_dir))

    def _migrate_sync(self, backup_dir: Path) -> int:
        with self._lock:
            conn = self._conn
            if conn is None:
                raise DatabaseError("database is closed")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            current = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
            latest = MIGRATIONS[-1][0]
            if current > latest:
                raise DatabaseError(
                    f"Database schema v{current} is newer than this application supports (v{latest}). "
                    "Refusing to start so nothing is damaged. Deploy the newer version or restore a matching backup."
                )
            pending = [m for m in MIGRATIONS if m[0] > current]
            if not pending:
                return current
            if current > 0:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                dest = backup_dir / f"pre-migration-v{current}-{stamp}.db"
                self._backup_sync(dest)
                log.info("Created pre-migration backup %s", dest.name)
            for version, description, statements in pending:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in statements:
                        conn.execute(stmt)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
                        (version, description, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                    conn.execute("COMMIT")
                except BaseException as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise DatabaseError(f"Migration v{version} ({description}) failed and was rolled back: {exc}") from exc
                log.info("Applied database migration v%d: %s", version, description)
            return latest

    def _backup_sync(self, dest: Path) -> None:
        conn = self._conn
        if conn is None:
            raise DatabaseError("database is closed")
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            conn.backup(target)
        finally:
            target.close()
        try:
            dest.chmod(0o600)
        except OSError:
            pass

    async def backup_to(self, dest: Path) -> None:
        def _do() -> None:
            with self._lock:
                self._backup_sync(Path(dest))

        await asyncio.to_thread(_do)
