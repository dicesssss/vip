# Troubleshooting

Read the Railway deploy logs first; every fatal start-up problem prints a full explanation.
Exit codes: 2 config · 3 storage/lock · 4 database · 5 bot token/network · 1 runtime failure.

## Missing or wrong volume (exit 3)
* "No Railway volume is attached" → add a Volume to **this service**, mount path `/data`, redeploy.
* "DATA_DIR is X but the volume is mounted at Y" → set `DATA_DIR=Y` or remove `DATA_DIR`.
* "exists but is not a mount point" → the path is container storage; mount the volume at exactly that path.
* "not writable" → set `RAILWAY_RUN_UID=0`, redeploy.
* "Another instance … holds the lock" → run one replica only; wait for the old deployment to stop.
* Sessions vanish after each deploy → the volume is not mounted where `DATA_DIR` points. Check System Status `volume_id`.

## Invalid credentials
* Exit 2 lists which variable is wrong (values are never printed). `API_HASH` must be 32 hex chars, `ADMIN_ID` numeric.
* Exit 5: BotFather token rejected → regenerate with `/token`, update `BOT_TOKEN`.
* "Telegram rejected API_ID/API_HASH" during login → re-copy from my.telegram.org. Imported sessions should come from
  the same API_ID/API_HASH.
* Bot silent → your `ADMIN_ID` is wrong (get it from @userinfobot), or you are using a group chat.

## Disconnected sessions
* `disconnected`/`connecting` → temporary; the app retries with backoff up to 5 minutes. Use **Reconnect** on the account.
* `auth_failed` → session revoked/logged out/account deactivated; retries are stopped on purpose. Remove and re-add.
* `missing_session` → file absent from the volume (volume replaced?). Restore a backup or re-add.
* Frequent disconnects or FloodWait → raise the interval; each account has its own limits and none are bypassed.
* Monitoring "waiting_for_account" → the chosen monitoring account is down; fix it or pick another in Settings
  (the app never switches by itself).

## Database errors (exit 4)
* Integrity failure → the file is untouched. Restore a backup (`docs/BACKUP_RESTORE.md`), or inspect with
  `sqlite3 /data/monitor.db "PRAGMA integrity_check"`.
* "schema newer than this application" → you deployed an older version over a newer database; redeploy the newer code.
* "database is locked" → another process uses the file; ensure a single replica.
* After a bad migration, use `/data/backups/pre-migration-*.db`.

## Other
* Login code "expired" immediately → send the code with spaces between digits and quickly.
* `pip` conflicts → loosen the ranges in `requirements.txt` and re-test.
