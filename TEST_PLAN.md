# Test plan

Status key: **[AUTO]** covered by `tests/test_core.py` and executed offline; **[MANUAL]** must be done by you
against real Telegram/Railway (not executed by the author).

## 0. Setup
- [AUTO] `python -m unittest discover -s tests -v` → all pass (one permission test skips when run as root).
- [MANUAL] `pip install -r requirements.txt` succeeds; adjust version ranges if pip reports a conflict.

## 1. Login
| Case | Expected | |
|---|---|---|
| Valid phone, correct code | Account added, session in `/data/sessions` | MANUAL |
| Wrong code | "That code is wrong", retry allowed, cancelled after 5 tries | MANUAL |
| Expired code | Message offers "Resend code" | MANUAL |
| Wrong 2FA password | Retry allowed, cancelled after 5 tries | MANUAL |
| Invalid / banned number, bad API_ID | Clear terminal message, pending file removed | MANUAL |
| Cancel mid-flow | No session file left in `/data/pending` | MANUAL |
| Code/password messages | Deleted from chat, absent from logs | MANUAL |
| Same account added twice | "already added" | MANUAL |
| Import StringSession / .session (valid, malformed, revoked) | Valid imports; others rejected with a safe message | MANUAL |

## 2. Persistence & redeploy
1. Add an account, `/snipe @name`, pick 15 s. Note `volume_id` in System Status.
2. Redeploy. [MANUAL] Account reconnects without login, same `volume_id`, monitoring resumes, no duplicate
   notifications, exactly one monitor task (status shows one target/one state).
3. [AUTO] Startup does not erase data; stale `pending/` cleared; migrations idempotent; future schema refused.
4. [AUTO] Missing / non-mounted / unwritable volume → clear diagnostic and exit code 3.
5. [MANUAL] Detach volume and deploy: service exits with the "No Railway volume" message.
6. [MANUAL] Terminate the session in Telegram → account becomes `auth_failed`, one notification, no retry loop.

## 3. Monitoring
- [AUTO] available requires both signals; unexpected response/None → unknown; taken → unavailable;
  invalid username stops monitoring; only the transition to available notifies.
- [MANUAL] Monitor a clearly taken name → `unavailable`. Monitor a name you know is free → `available` alert with
  the "snapshot, not a reservation" warning. Interval buttons/custom interval validation (4 and 3601 rejected).
- [MANUAL] Stop → stays stopped after redeploy. Restart → single task.

## 4. FloodWait
- [AUTO] A FloodWait blocks further requests locally until wait + 1 s; no call reaches Telegram in the window;
  one notification with account, operation, category, wait, time, status, next retry; wait persisted in DB.
- [AUTO] Identical errors are grouped; authorization failures always notify.
- [MANUAL] Natural FloodWait with a very low interval: status shows `rate_limited`, resumes by itself afterwards.
  Do not provoke FloodWait on purpose on an account you care about.

## 5. Security
- [MANUAL] Message the bot from another Telegram account → no reply at all. Use the bot in a group → ignored.
- [AUTO] Redaction of bot token (incl. inside URLs), 32-hex values, phone numbers, configured secrets.
- [MANUAL] `git ls-files | grep -E "session|\.env|\.db"` prints nothing.
