"""Persistent-volume verification, directory preparation and the instance lock.

Design rule: never silently fall back to ephemeral disk on Railway. If the volume
is not mounted we fail loudly, because sessions written to the container's own
filesystem vanish on the next deploy.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional

from .config import Config, on_railway

log = logging.getLogger(__name__)

MIN_FREE_BYTES_FATAL = 5 * 1024 * 1024
MIN_FREE_BYTES_WARN = 50 * 1024 * 1024


class StorageError(RuntimeError):
    """The message is a complete, human-readable diagnostic."""


@dataclass(frozen=True)
class StorageInfo:
    volume_id: str
    free_bytes: int
    total_bytes: int
    mounted: bool


def _write_probe(directory: Path) -> None:
    probe = directory / f".write_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    payload = uuid.uuid4().hex.encode()
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if probe.read_bytes() != payload:
            raise OSError("read-back mismatch")
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass  # best effort on filesystems without POSIX permissions


def _read_or_create_marker(path: Path) -> str:
    """The marker never gets overwritten; a changed id after a deploy reveals a replaced volume."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip() or "unknown"
    value = uuid.uuid4().hex[:12]
    try:
        os.write(fd, value.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return value


def verify_and_prepare(cfg: Config, env: Optional[Mapping[str, str]] = None) -> StorageInfo:
    env = os.environ if env is None else env
    data = cfg.data_dir

    mounted = os.path.ismount(data) if data.exists() else False

    if cfg.require_mounted_volume:
        railway_mount = (env.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
        if on_railway(env) and not railway_mount:
            raise StorageError(
                "No Railway volume is attached to this service (RAILWAY_VOLUME_MOUNT_PATH is not set).\n"
                f"Without a volume everything under {data} is erased on every deploy.\n"
                "Fix: in the Railway project, add a Volume, attach it to this service and mount it at "
                f"{data}. See README -> 'Persistent Volume'."
            )
        if railway_mount and os.path.realpath(railway_mount) != os.path.realpath(str(data)):
            raise StorageError(
                f"DATA_DIR is {data} but the attached volume is mounted at {railway_mount}.\n"
                f"Fix: set DATA_DIR={railway_mount} (or remove the DATA_DIR variable) or re-mount the volume at {data}."
            )
        if not data.exists():
            raise StorageError(
                f"Data directory {data} does not exist, so no volume is mounted there.\n"
                "Fix: attach a Railway volume with this exact mount path. The application deliberately does not "
                "create the directory itself, because that would write to disposable container storage."
            )
        if not mounted:
            raise StorageError(
                f"{data} exists but is not a mount point, so it is probably disposable container storage.\n"
                "Fix: attach/mount the volume at this path. For local development only, set "
                "REQUIRE_MOUNTED_VOLUME=false."
            )
    else:
        if not data.exists():
            log.warning("DATA_DIR %s does not exist; creating it. Data is NOT guaranteed to persist.", data)
        try:
            data.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot create data directory {data}: {exc}") from exc

    try:
        for sub in (data, cfg.sessions_dir, cfg.pending_dir, cfg.logs_dir, cfg.backups_dir):
            _ensure_dir(sub)
        _write_probe(data)
        _write_probe(cfg.sessions_dir)
    except PermissionError as exc:
        raise StorageError(
            f"Data directory {data} is not writable ({exc}).\n"
            "Fix: on Railway, set the service variable RAILWAY_RUN_UID=0 so the process may write to the volume, "
            "then redeploy."
        ) from exc
    except OSError as exc:
        raise StorageError(f"Write test failed in {data}: {exc}") from exc

    usage = shutil.disk_usage(data)
    if usage.free < MIN_FREE_BYTES_FATAL:
        raise StorageError(f"Volume is almost full ({usage.free // 1024} KiB free). Grow the volume or free space.")
    if usage.free < MIN_FREE_BYTES_WARN:
        log.warning("Volume has only %d MiB free", usage.free // (1024 * 1024))

    volume_id = _read_or_create_marker(cfg.marker_path)
    # Stale half-finished logins from a previous run are never valid; clear only the pending directory.
    for leftover in cfg.pending_dir.iterdir():
        try:
            if leftover.is_file():
                leftover.unlink()
        except OSError:
            log.warning("could not remove stale pending file %s", leftover.name)

    log.info("Storage OK: dir=%s mounted=%s volume_id=%s free=%d MiB", data, mounted, volume_id, usage.free // (1024 * 1024))
    return StorageInfo(volume_id=volume_id, free_bytes=usage.free, total_bytes=usage.total, mounted=mounted)


def health(cfg: Config) -> Dict[str, object]:
    """Cheap runtime check used by System Status."""
    try:
        _write_probe(cfg.data_dir)
        usage = shutil.disk_usage(cfg.data_dir)
        return {"ok": True, "detail": "writable", "free_bytes": usage.free, "total_bytes": usage.total}
    except Exception as exc:  # noqa: BLE001 - report any failure to the admin
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "free_bytes": 0, "total_bytes": 0}


class InstanceLock:
    """Single-instance guard (flock on a file in the volume).

    Railway may briefly run the old and new deployment side by side; the newcomer
    waits for the old process to release the lock instead of starting a second
    set of monitoring tasks and Telegram connections.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: Optional[int] = None

    async def acquire(self, timeout: float = 60.0, poll: float = 1.0) -> None:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX development machine
            log.warning("fcntl unavailable; instance lock disabled")
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise StorageError(
                        "Another instance of this application holds the data-directory lock. "
                        "Run exactly one replica, and stop the other deployment/process."
                    )
                await asyncio.sleep(poll)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.close(self._fd)
        finally:
            self._fd = None
