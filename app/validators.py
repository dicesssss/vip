"""Input validation and safe identifiers. Pure functions, no I/O."""
from __future__ import annotations

import re
from typing import Optional


class ValidationError(ValueError):
    """Message is safe to show to the administrator."""


_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]$")
_LINK_PREFIX = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/", re.IGNORECASE)


def normalize_username(raw: str) -> str:
    """Return the lowercase username without '@'. Telegram remains the final authority."""
    text = (raw or "").strip()
    text = _LINK_PREFIX.sub("", text).lstrip("@").strip()
    if not text:
        raise ValidationError("The username is empty. Example: /snipe @example_name")
    if len(text) < 5 or len(text) > 32:
        raise ValidationError("A Telegram username must be 5–32 characters long.")
    if not re.fullmatch(r"[A-Za-z0-9_]+", text):
        raise ValidationError("Only Latin letters, digits and underscores are allowed.")
    if not text[0].isalpha():
        raise ValidationError("A username must start with a letter.")
    if text.endswith("_"):
        raise ValidationError("A username cannot end with an underscore.")
    if "__" in text:
        raise ValidationError("A username cannot contain two underscores in a row.")
    if not _USERNAME_RE.match(text):  # defensive: should be unreachable
        raise ValidationError("That is not a valid Telegram username.")
    return text.lower()


def parse_interval(raw: str, lo: int, hi: int) -> int:
    text = (raw or "").strip().lower().removesuffix("s").strip()
    if not text.isdigit():
        raise ValidationError("Send a whole number of seconds, for example 45.")
    value = int(text)
    if value < lo or value > hi:
        raise ValidationError(f"The interval must be between {lo} and {hi} seconds.")
    return value


def parse_phone(raw: str) -> str:
    text = re.sub(r"[\s\-().]", "", raw or "")
    if text.startswith("00"):
        text = "+" + text[2:]
    if text and not text.startswith("+"):
        text = "+" + text
    if not re.fullmatch(r"\+\d{7,15}", text):
        raise ValidationError("Send the phone number in international format, for example +919876543210.")
    return text


def parse_login_code(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not 4 <= len(digits) <= 8:
        raise ValidationError("The login code is 5 digits. Send it like: 1 2 3 4 5")
    return digits


def mask_phone(phone: Optional[str]) -> str:
    if not phone:
        return "—"
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 4:
        return "••••"
    return "+" + "•" * (len(digits) - 2) + digits[-2:]


def safe_account_id(user_id: int, username: Optional[str]) -> str:
    """Identifier safe to show to the admin: handle plus the last 4 digits of the user id."""
    tail = str(user_id)[-4:]
    return f"@{username} (id …{tail})" if username else f"id …{tail}"
