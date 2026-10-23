"""Shared application context handed to every handler module."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ..accounts import AccountManager
from ..config import Config
from ..db import Database
from ..login import LoginFlow
from ..monitor import Monitor
from ..notifier import Notifier
from ..repo import Repo
from ..timeutil import utcnow


@dataclass
class AppContext:
    cfg: Config
    db: Database
    repo: Repo
    manager: AccountManager
    monitor: Monitor
    notifier: Notifier
    bot: Any
    volume_id: str
    started_at: datetime = field(default_factory=utcnow)
    login: Optional[LoginFlow] = None

    async def cancel_login(self) -> None:
        flow, self.login = self.login, None
        if flow is not None:
            await flow.cancel()
