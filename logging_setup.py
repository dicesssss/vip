"""Logging with secret redaction.

Redaction is applied to the *final formatted record* (message + traceback) so it
also covers exception text raised by third-party libraries.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path
from typing import Iterable, List, Set

_PATTERNS = [
    # Telegram bot token
    # (no leading \b: tokens appear glued to text in URLs, e.g. "/bot123456:AAAA...")
    (re.compile(r"(?<![0-9])\d{5,}:[A-Za-z0-9_-]{30,}"), "[redacted-bot-token]"),
    # Telethon StringSession (starts with '1', long urlsafe-base64 body)
    (re.compile(r"\b1[A-Za-z0-9_\-+/=]{200,}"), "[redacted-session-string]"),
    # 32-hex values (API hash and similar)
    (re.compile(r"\b[0-9a-fA-F]{32}\b"), "[redacted-hex]"),
    # International phone numbers
    (re.compile(r"(?<![\w+])\+\d{7,15}\b"), "+[redacted-phone]"),
]


class Redactor:
    def __init__(self) -> None:
        self._secrets: Set[str] = set()

    def add_secret(self, value: str) -> None:
        if value and len(value) >= 6:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        return text


REDACTOR = Redactor()


def redact(text: str) -> str:
    return REDACTOR.redact(text)


class RedactingFormatter(logging.Formatter):
    converter = time.gmtime  # timestamps in UTC

    def format(self, record: logging.LogRecord) -> str:
        return REDACTOR.redact(super().format(record))


def _formatter() -> RedactingFormatter:
    return RedactingFormatter(
        fmt="%(asctime)sZ %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def setup_logging(level: str, secrets: Iterable[str]) -> None:
    for secret in secrets:
        REDACTOR.add_secret(secret)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(_formatter())
    root.addHandler(stream)
    # Library loggers can include request payloads at DEBUG; keep them quiet.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


def add_file_logging(logs_dir: Path) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "app.log"
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(_formatter())
    logging.getLogger().addHandler(handler)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def tail_log(path: Path, lines: int = 40, max_chars: int = 3400) -> str:
    """Return the last log lines (re-redacted), trimmed to fit a Telegram message."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            chunk = fh.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return "(no log file yet)"
    except OSError as exc:
        return f"(cannot read log file: {type(exc).__name__})"
    selected: List[str] = chunk.splitlines()[-lines:]
    text = REDACTOR.redact("\n".join(selected))
    if len(text) > max_chars:
        text = "…" + text[-max_chars:]
    return text or "(log is empty)"
