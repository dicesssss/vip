"""Inline keyboards. Callback data is a single short ``Nav(a=action, v=value)``."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

INTERVAL_CHOICES = (5, 10, 15, 30, 60)


class Nav(CallbackData, prefix="n"):
    a: str
    v: str = ""


Row = List[Tuple[str, Nav]]


def _kb(rows: Sequence[Row]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        for text, data in row:
            builder.button(text=text, callback_data=data)
    builder.adjust(*[len(r) for r in rows])
    return builder.as_markup()


def main_menu() -> InlineKeyboardMarkup:
    return _kb([
        [("➕ Add Account", Nav(a="add")), ("📥 Import Session", Nav(a="import"))],
        [("👥 List Accounts", Nav(a="accts")), ("🗑 Remove Account", Nav(a="rm_list"))],
        [("🎯 Monitor Username", Nav(a="mon")), ("⏹ Stop Monitoring", Nav(a="stop"))],
        [("⚙️ Monitoring Settings", Nav(a="set")), ("⏳ FloodWait Notifications", Nav(a="flood"))],
        [("🧾 View Logs", Nav(a="logs")), ("📊 System Status", Nav(a="status"))],
        [("🔄 Restart Monitoring", Nav(a="restart"))],
    ])


def home_only() -> InlineKeyboardMarkup:
    return _kb([[("⬅️ Dashboard", Nav(a="home"))]])


def cancel() -> InlineKeyboardMarkup:
    return _kb([[("✖️ Cancel", Nav(a="cancel"))]])


def code_step() -> InlineKeyboardMarkup:
    return _kb([[("🔁 Resend code", Nav(a="resend")), ("✖️ Cancel", Nav(a="cancel"))]])


def account_picker(views: List[Dict[str, Any]], action: str, with_home: bool = True) -> InlineKeyboardMarkup:
    rows: List[Row] = [[(f"{v['safe_id']} · {v['status']}", Nav(a=action, v=str(v["id"])))] for v in views]
    if with_home:
        rows.append([("⬅️ Dashboard", Nav(a="home"))])
    return _kb(rows)


def account_detail(account_id: int, *, can_reconnect: bool) -> InlineKeyboardMarkup:
    rows: List[Row] = [[("🎯 Use for monitoring", Nav(a="usemon", v=str(account_id))),
                        ("✏️ Set username…", Nav(a="claim_pick", v=str(account_id)))]]
    second: Row = []
    if can_reconnect:
        second.append(("🔌 Reconnect", Nav(a="reconnect", v=str(account_id))))
    second.append(("🗑 Remove", Nav(a="rm_ask", v=str(account_id))))
    rows.append(second)
    rows.append([("⬅️ Accounts", Nav(a="accts")), ("🏠 Dashboard", Nav(a="home"))])
    return _kb(rows)


def remove_choice(account_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [("🗑 Remove (keep Telegram login)", Nav(a="rm_do", v=str(account_id)))],
        [("🗑 Remove + log out session", Nav(a="rm_logout", v=str(account_id)))],
        [("↩️ Keep account", Nav(a="acct", v=str(account_id)))],
    ])


def target_card(running: bool) -> InlineKeyboardMarkup:
    toggle = ("⏹ Stop", Nav(a="stop")) if running else ("▶️ Start", Nav(a="start"))
    return _kb([
        [toggle, ("🔄 Restart", Nav(a="restart"))],
        [("📜 History", Nav(a="hist")), ("⚙️ Interval", Nav(a="set"))],
        [("✏️ Set username on an account…", Nav(a="claim_pick"))],
        [("🔃 Refresh", Nav(a="target")), ("🏠 Dashboard", Nav(a="home"))],
    ])


def settings(current: int, account_views: List[Dict[str, Any]], selected: Optional[int]) -> InlineKeyboardMarkup:
    rows: List[Row] = [[((f"✅ {s}s" if s == current else f"{s}s"), Nav(a="iv", v=str(s))) for s in INTERVAL_CHOICES],
                       [("✍️ Custom interval", Nav(a="iv_custom"))]]
    for v in account_views:
        tick = "✅ " if selected == v["id"] else ""
        rows.append([(f"{tick}Monitor with {v['safe_id']}", Nav(a="usemon", v=str(v["id"])))])
    rows.append([("🎯 Target card", Nav(a="target")), ("🏠 Dashboard", Nav(a="home"))])
    return _kb(rows)


def flood_settings(notify_flood: bool, notify_errors: bool) -> InlineKeyboardMarkup:
    return _kb([
        [(f"FloodWait notices: {'ON' if notify_flood else 'OFF'}", Nav(a="tog_flood"))],
        [(f"Other error notices: {'ON' if notify_errors else 'OFF'}", Nav(a="tog_err"))],
        [("🏠 Dashboard", Nav(a="home"))],
    ])


def refresh(action: str) -> InlineKeyboardMarkup:
    return _kb([[("🔃 Refresh", Nav(a=action)), ("🏠 Dashboard", Nav(a="home"))]])


def claim_confirm(account_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [("✅ Yes, change this account's username", Nav(a="claim_do", v=str(account_id)))],
        [("✖️ Cancel", Nav(a="home"))],
    ])


def availability_alert(username: str) -> InlineKeyboardMarkup:
    return _kb([
        [("✏️ Set it on one of my accounts…", Nav(a="claim_pick"))],
        [("⏹ Stop monitoring", Nav(a="stop")), ("🎯 Status", Nav(a="target"))],
    ])
