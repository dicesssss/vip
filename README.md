# Telegram Username Monitor & Session Manager

Admin-only Telegram control bot (aiogram 3) that manages several authorized Telethon accounts and monitors
whether a username becomes available. Built for Railway with a Persistent Volume.

> **Verification status (read this).** The core logic (config, storage checks, SQLite + migrations, validators,
> error classification, FloodWait gating, availability checker, monitor loop, notifier grouping, log redaction)
> has **offline unit tests that were executed and pass** (`python -m unittest discover -s tests`).
> The **Telethon and aiogram integration was never run**: no real Telegram login, no bot polling, no Railway
> deployment, and the pinned versions in `requirements.txt` were not installed in the authoring sandbox.
> Follow `docs/TEST_PLAN.md` on your own machine/Railway before relying on it.

## What it does

* Dashboard with inline buttons: Add Account, Import Session, List/Remove Account, Monitor Username,
  Stop/Restart Monitoring, Monitoring Settings, FloodWait Notifications, View Logs, System Status.
* `/snipe @username` registers a target and starts monitoring.
* Intervals 5/10/15/30/60 s or custom (5–3600 s).
* States are kept distinct: **available / unavailable / unknown / rate-limited**. `available` is reported only
  when `account.checkUsername` says free **and** `contacts.resolveUsername` confirms no current holder.
  Anything unexpected is `unknown`. Even "available" is only a snapshot, never a guarantee.
* FloodWait is honoured exactly: no request is sent for that account until Telegram's wait (+1 s) has passed,
  the wait is persisted across restarts, and the admin is notified. The monitor never switches to another
  account to avoid a limit.
* Username change (`/Set username…` button) is manual, confirmed, per-account, and never creates a channel.

## Project layout

```
app/
  main.py            entrypoint & graceful shutdown
  config.py          env parsing (secrets never echoed)
  storage.py         volume verification, write probe, instance lock
  db.py repo.py      SQLite (WAL, transactions, migrations, integrity check)
  accounts.py        supervised Telethon connections, FloodWait gate, backoff
  login.py imports.py  interactive login / session import
  checker.py monitor.py claim.py  availability, monitor loop, username change
  notifier.py errors.py backoff.py logging_setup.py validators.py timeutil.py
  bot/               aiogram handlers, keyboards, admin-only middleware
tests/test_core.py   offline tests (stdlib only)
docs/                backup/restore, test plan, troubleshooting
```

## Environment variables (Railway → Service → Variables)

| Variable | Required | Meaning |
|---|---|---|
| `BOT_TOKEN` | yes | BotFather token |
| `ADMIN_ID` | yes | Your numeric Telegram user id; the only user the bot answers |
| `API_ID`, `API_HASH` | yes | From https://my.telegram.org |
| `DATA_DIR` | no | Volume mount path, default `/data` (or `RAILWAY_VOLUME_MOUNT_PATH`) |
| `REQUIRE_MOUNTED_VOLUME` | no | Default `true` on Railway, `false` elsewhere. Keep `true` in production |
| `DISPLAY_TZ` | no | IANA zone for displayed times, e.g. `Asia/Kolkata` (stored times are UTC) |
| `LOG_LEVEL` | no | `INFO` default |
| `RAILWAY_RUN_UID` | maybe | Set to `0` if the app cannot write to the volume |

Secrets live only in Railway variables. Never put them in Git.

## Persistent Volume (do this before first deploy)

1. Railway project → your service → **Volumes → New Volume** (or Ctrl/Cmd+K → "Volume").
2. Attach to this service, **mount path `/data`**.
3. Keep **one replica** (volumes are single-attach; the app also holds a lock file).
4. If startup says `not writable`, add variable `RAILWAY_RUN_UID=0` and redeploy.

Contents of `/data`: `sessions/` (authorized sessions, chmod 600), `monitor.db` (+WAL), `logs/app.log`
(redacted), `backups/` (automatic pre-migration copies), `.volume_id`, `app.lock`.

The app **fails with a diagnostic and exits** if the volume is missing, not a mount point, not writable, or
nearly full. It never silently writes sessions to disposable container storage, and it never erases data on start.

**Consequences of deleting/replacing the volume:** all saved sessions, monitor config, history and preferences are
gone. Telegram-side logins stay active until terminated (Settings → Devices), so re-add or restore a backup,
and terminate stale devices. A replaced volume gets a new `volume_id`, shown in System Status and the startup
message, so you can notice it.

## Deploy (existing GitHub-connected Railway project)

1. Push this folder to GitHub. Check `git status` shows no `.env`, `*.session`, `*.db` (the `.gitignore` excludes them).
2. Railway → service → Settings → connect the repo/branch. Builder: Nixpacks (config in `railway.json`; start command
   `python -m app.main`).
3. Add the variables above. Add the Volume at `/data`.
4. Deploy. In the Railway logs expect `Storage OK`, `Database ready`, `Control bot connected as @…`.
5. In Telegram, open your bot, send `/start`. Add an account, then `/snipe @name`.
6. Redeploy once and confirm the account reconnects and monitoring resumes (see test plan).

If you already pasted secrets anywhere public, rotate them: BotFather `/revoke`, and create new API credentials.

## Running locally

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in, then export the variables
export REQUIRE_MOUNTED_VOLUME=false DATA_DIR=$PWD/data
python -m app.main
```

## Security notes

* Updates from anyone but `ADMIN_ID` (and from non-private chats) are silently ignored.
* Login codes, 2FA passwords and imported sessions are deleted from the chat immediately, kept only in memory,
  never logged or stored. Logs are redacted (bot token, 32-hex values, phone numbers, long session strings).
* Send login codes with spaces (`1 2 3 4 5`); Telegram may invalidate a code sent as a plain number.
* Bot messages are not end-to-end encrypted. Importing a session through the bot exposes it to Telegram's servers
  in transit; prefer the phone-login flow when possible.
* Automated checks run on **your own accounts** at the interval you choose, within Telegram's limits. Using
  Telegram accounts for automation can still lead to restrictions; the app reduces risk but cannot remove it.

See `docs/BACKUP_RESTORE.md`, `docs/TEST_PLAN.md`, `docs/TROUBLESHOOTING.md`.
