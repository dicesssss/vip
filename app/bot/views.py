"""Pure text rendering for the dashboard (HTML parse mode). No aiogram imports."""
from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..timeutil import fmt_time, human_duration

STATE_ICON = {"available": "🟢", "unavailable": "🔴", "unknown": "⚪", "rate_limited": "🟡"}
STATE_TEXT = {
    "available": "AVAILABLE (snapshot, not guaranteed)",
    "unavailable": "UNAVAILABLE (taken / reserved)",
    "unknown": "UNKNOWN (could not be determined)",
    "rate_limited": "RATE-LIMITED (check suspended)",
}
ACCOUNT_ICON = {"connected": "🟢", "connecting": "🟡", "disconnected": "🔴", "auth_failed": "⛔",
                "missing_session": "⛔", "stopped": "⚪"}

e = html.escape


def dashboard(admin_first_name: Optional[str] = None) -> str:
    who = f", {e(admin_first_name)}" if admin_first_name else ""
    return f"🛰 <b>Username Sniper</b>\nWelcome{who}. Choose an action:"


def account_line(view: Dict[str, Any], tz: Any = None) -> str:
    icon = ACCOUNT_ICON.get(view["status"], "⚪")
    parts = [f"{icon} <b>{e(view['safe_id'])}</b> — {e(view['status'])}"]
    if view.get("detail"):
        parts.append(f"   <i>{e(str(view['detail'])[:100])}</i>")
    flood = view.get("flood_until")
    if flood is not None and isinstance(flood, datetime):
        parts.append(f"   ⏳ FloodWait until {fmt_time(flood, tz)}")
    return "\n".join(parts)


def account_list(views: List[Dict[str, Any]], tz: Any = None, monitor_account_id: Optional[int] = None) -> str:
    if not views:
        return "No accounts yet. Use <b>Add Account</b> or <b>Import Session</b>."
    lines = ["👥 <b>Accounts</b>"]
    for v in views:
        mark = " 🎯 active" if monitor_account_id == v["id"] else ""
        lines.append(account_line(v, tz) + mark)
    return "\n\n".join([lines[0], "\n\n".join(lines[1:])])


def account_detail(view: Dict[str, Any], tz: Any = None, is_monitor: bool = False) -> str:
    lines = [
        f"👤 <b>{e(view['safe_id'])}</b>",
        f"Status: {ACCOUNT_ICON.get(view['status'], '⚪')} {e(view['status'])}",
        f"Phone: {e(view.get('phone_hint') or '—')}",
        f"Last connected: {fmt_time(view.get('last_connected_at'), tz)}",
        f"Used for sniping: {'yes' if is_monitor else 'no'}",
    ]
    if view.get("detail"):
        lines.append(f"Detail: {e(str(view['detail'])[:160])}")
    if view.get("permanent"):
        lines.append("⛔ Permanent failure: retries are stopped. Remove and re-add this account.")
    return "\n".join(lines)


def target_card(target: Optional[Dict[str, Any]], *, monitor_status: str, interval: int,
                account_label: Optional[str], tz: Any = None) -> str:
    if target is None:
        return "🎯 <b>No target set.</b>\nSend <code>/snipe @username</code>."
    state = target.get("state") or "unknown"
    lines = [
        f"🎯 <b>@{e(target['username'])}</b>",
        f"Latest check: {STATE_ICON.get(state, '⚪')} {STATE_TEXT.get(state, state)}",
        f"Last confirmed: {e(target.get('confirmed_state') or '—')}",
        f"Sniping Status: <b>{e(monitor_status)}</b> · every {interval}s",
        f"Active Account: {e(account_label or 'none selected')}",
        f"Last check: {fmt_time(target.get('last_check_at'), tz)}",
        f"Last success: {fmt_time(target.get('last_success_at'), tz)}",
        f"Checks run: {target.get('checks_total') or 0}",
    ]
    if target.get("last_error"):
        lines.append(f"Last error: <code>{e(str(target['last_error'])[:160])}</code>")
        lines.append(f"Error time: {fmt_time(target.get('last_error_at'), tz)}")
    lines.append("\n<i>“Available” is a snapshot only; bot will auto-claim immediately when open.</i>")
    return "\n".join(lines)


def history(rows: List[Dict[str, Any]], username: str, tz: Any = None) -> str:
    if not rows:
        return f"No history for @{e(username)} yet."
    lines = [f"📜 <b>History for @{e(username)}</b> (newest first)"]
    for r in rows:
        icon = STATE_ICON.get(r["state"], "⚪")
        detail = f" — {e(str(r['detail'])[:70])}" if r.get("detail") else ""
        lines.append(f"{icon} {fmt_time(r['ts'], tz)} · {e(r['state'])}{detail}")
    return "\n".join(lines)


def settings(interval: int, min_i: int, max_i: int, account_label: Optional[str]) -> str:
    return (
        "⚙️ <b>Sniping settings</b>\n"
        f"Current account rotation gap: <b>{interval} s</b>\n"
        f"Active account: {e(account_label or 'none selected')}\n\n"
        f"Allowed gap: {min_i}–{max_i} s. Telegram enforces its own limits (FloodWait); "
        "bot rotates across your accounts maintaining this gap to check and snipe instantly upon availability."
    )


def flood_settings(notify_flood: bool, notify_errors: bool, events: List[Dict[str, Any]], tz: Any = None) -> str:
    lines = [
        "⏳ <b>FloodWait & error notifications</b>",
        f"FloodWait notices: {'ON' if notify_flood else 'OFF'}",
        f"Other error notices: {'ON' if notify_errors else 'OFF'}",
        "<i>Authorization failures are always reported.</i>",
    ]
    if events:
        lines.append("\n<b>Recent FloodWait events</b>")
        for ev in events:
            wait = human_duration(ev["wait_seconds"]) if ev.get("wait_seconds") is not None else "n/a"
            lines.append(f"• {fmt_time(ev['ts'], tz)} · {e(ev['op'])} · wait {wait}")
    else:
        lines.append("\nNo FloodWait events recorded.")
    return "\n".join(lines)


def stats_dashboard(*, uptime_seconds: float, counts: Dict[str, int], target: Optional[Dict[str, Any]],
                    monitor_status: str, interval: int, active_runners_count: int, tz: Any = None) -> str:
    """Professional performance and sniping statistics dashboard."""
    total_checks = target.get("checks_total") or 0 if target else 0
    target_name = f"@{e(target['username'])}" if target else "None"
    target_state = STATE_TEXT.get(target.get("state"), "Unknown") if target else "Not Set"

    lines = [
        "📊 <b>Performance & Sniping Statistics</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"⏱️ <b>Uptime:</b> <code>{human_duration(uptime_seconds)}</code>",
        f"🎯 <b>Target Username:</b> <b>{target_name}</b>",
        f"📌 <b>Current Status:</b> {e(target_state)}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "📈 <b>Execution Metrics:</b>",
        f"  • Total Tries / Checks Run: <b>{total_checks}</b>",
        f"  • Account Rotation Gap: <b>{interval}s</b>",
        f"  • Sniper Status: <b>{e(monitor_status)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "👥 <b>Account Fleet Status:</b>",
        f"  • Total Accounts Loaded: <b>{counts['total']}</b>",
        f"  • Active & Connected: 🟢 <b>{counts['connected']}</b>",
        f"  • Rotating / Available: 🔄 <b>{active_runners_count}</b>",
        f"  • Disconnected: 🔴 <b>{counts['disconnected']}</b>",
        f"  • Permanently Failed: ⛔ <b>{counts.get('failed', 0)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🕒 <i>Report generated at: {fmt_time(datetime.utcnow(), tz)}</i>",
    ]
    return "\n".join(lines)


def status(*, version: str, uptime_seconds: float, counts: Dict[str, int], target: Optional[Dict[str, Any]],
           monitor_status: str, interval: int, monitor_account: Optional[str], last_check: Optional[str],
           floods: List[Dict[str, Any]], errors: List[Dict[str, Any]], storage: Dict[str, Any],
           db_ok: bool, volume_id: str, tz: Any = None) -> str:
    storage_line = "✅ writable" if storage.get("ok") else f"❌ {e(str(storage.get('detail')))}"
    free = storage.get("free_bytes") or 0
    lines = [
        f"📊 <b>System status</b> · v{e(version)}",
        f"Uptime: {human_duration(uptime_seconds)}",
        "",
        f"Sessions: {counts['total']} total · {counts['connected']} connected · {counts['disconnected']} disconnected"
        + (f" · {counts['failed']} permanently failed" if counts.get("failed") else ""),
        f"Target: {('@' + e(target['username'])) if target else 'none'}",
        f"Sniping: <b>{e(monitor_status)}</b> · every {interval}s · account {e(monitor_account or '—')}",
        f"Last check: {fmt_time(last_check, tz)}",
        "",
        f"Storage: {storage_line} · {free // (1024 * 1024)} MiB free · volume <code>{e(volume_id)}</code>",
        f"Database integrity: {'✅ ok' if db_ok else '❌ problem detected — see logs'}",
    ]
    lines.append("\n<b>Recent FloodWait</b>")
    lines += [f"• {fmt_time(ev['ts'], tz)} · {e(ev['op'])} · {human_duration(ev['wait_seconds'] or 0)}" for ev in floods] or ["• none"]
    lines.append("\n<b>Recent errors</b>")
    lines += [f"• {fmt_time(ev['ts'], tz)} · {e(ev['op'])} · {e(ev['category'])}" for ev in errors] or ["• none"]
    return "\n".join(lines)


def logs(text: str) -> str:
    return f"🧾 <b>Recent log (secrets redacted)</b>\n<pre>{e(text)}</pre>"


REVOCATION_GUIDANCE = (
    "🔐 <b>Revoking the session on Telegram</b>\n"
    "Removing an account here deletes the saved session from this app's storage, but the login stays valid on "
    "Telegram's side until it is terminated. To revoke it: open Telegram with that account → "
    "Settings → Devices → select “Username Sniper” → Terminate Session. "
    "You can also choose <b>Remove + log out</b>, which asks Telegram to end the session for you."
)
