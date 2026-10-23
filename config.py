"""Environment-driven configuration.

Error messages and ``repr`` never include secret values; they only name the
offending variable.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")

MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 3600


def _truthy(value: Optional[str]) -> Optional[bool]:
    if value is None or value.strip() == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    admin_id: int
    api_id: int
    api_hash: str = field(repr=False)
    data_dir: Path
    require_mounted_volume: bool
    display_tz: str = "UTC"
    log_level: str = "INFO"
    min_interval: int = MIN_INTERVAL_SECONDS
    max_interval: int = MAX_INTERVAL_SECONDS

    # ---- derived locations (all absolute, all inside the volume) ----------
    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def pending_dir(self) -> Path:
        return self.data_dir / "pending"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "app.lock"

    @property
    def marker_path(self) -> Path:
        return self.data_dir / ".volume_id"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "app.log"

    def secrets(self) -> List[str]:
        return [self.bot_token, self.api_hash]

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.display_tz)


def on_railway(env: Mapping[str, str]) -> bool:
    return any(env.get(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"))


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = os.environ if env is None else env
    problems: List[str] = []

    def need(name: str) -> str:
        value = (env.get(name) or "").strip()
        if not value:
            problems.append(f"{name} is not set")
        return value

    bot_token = need("BOT_TOKEN")
    if bot_token and not _TOKEN_RE.match(bot_token):
        problems.append("BOT_TOKEN does not look like a BotFather token (expected <digits>:<35 chars>)")

    admin_raw = need("ADMIN_ID")
    admin_id = 0
    if admin_raw:
        if admin_raw.lstrip("-").isdigit() and int(admin_raw) > 0:
            admin_id = int(admin_raw)
        else:
            problems.append("ADMIN_ID must be a positive integer (your numeric Telegram user id)")

    api_raw = need("API_ID")
    api_id = 0
    if api_raw:
        if api_raw.isdigit() and int(api_raw) > 0:
            api_id = int(api_raw)
        else:
            problems.append("API_ID must be a positive integer (from my.telegram.org)")

    api_hash = need("API_HASH")
    if api_hash and not _HASH_RE.match(api_hash):
        problems.append("API_HASH must be 32 hexadecimal characters (from my.telegram.org)")

    data_raw = (env.get("DATA_DIR") or env.get("RAILWAY_VOLUME_MOUNT_PATH") or "/data").strip()
    data_dir = Path(data_raw)
    if not data_dir.is_absolute():
        problems.append("DATA_DIR must be an absolute path (for example /data)")

    explicit = _truthy(env.get("REQUIRE_MOUNTED_VOLUME"))
    require_mount = explicit if explicit is not None else on_railway(env)

    tz_name = (env.get("DISPLAY_TZ") or "UTC").strip()
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"DISPLAY_TZ {tz_name!r} is not a valid IANA time zone (for example Asia/Kolkata)")

    level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
    if level not in _LOG_LEVELS:
        problems.append(f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}")

    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))

    return Config(
        bot_token=bot_token,
        admin_id=admin_id,
        api_id=api_id,
        api_hash=api_hash,
        data_dir=data_dir,
        require_mounted_volume=require_mount,
        display_tz=tz_name,
        log_level=level,
    )
