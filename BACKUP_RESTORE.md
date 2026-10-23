# Backup & restore of the Railway volume

Session files are credentials. Treat every backup like a password: encrypt it, never put it in Git.

## Back up

Railway has built-in **volume backups** (Volume → Backups, available on supported plans): enable a schedule
and use "Backup now" before risky changes. Prefer this.

Manual copy using the Railway CLI (run from your computer, project linked with `railway link`):

```
railway ssh                       # opens a shell in the running service
cd /data
python - <<'EOF'
import sqlite3
src = sqlite3.connect("/data/monitor.db"); dst = sqlite3.connect("/data/backups/manual.db")
src.backup(dst); dst.close(); src.close()
EOF
tar czf /tmp/data-backup.tgz -C /data sessions backups
```

Then copy `/tmp/data-backup.tgz` out (for example `railway ssh` + `base64`, or Railway's file tools) and encrypt it:
`gpg -c data-backup.tgz`. Use the SQLite `.backup` API (above), not a plain `cp` of a live database.

The app also writes `backups/pre-migration-*.db` automatically before any schema migration.

## Restore

1. Stop the service (scale to 0 / remove the deployment) so nothing writes to the volume.
2. Restore the Railway backup, **or** extract your archive into `/data` so you have
   `/data/sessions/*.session` and `/data/monitor.db`. Remove stale `monitor.db-wal` / `-shm` files.
3. Make sure the sessions are not in use elsewhere (the same session on two machines can get revoked).
4. Start the service. Check System Status: sessions connected, monitoring resumed, integrity ok.

## If the volume is lost or replaced

Everything in it is gone (see README). Re-add accounts, or restore a backup. Terminate unknown devices on
Telegram (Settings → Devices). If startup reports a database integrity failure, the file is left untouched;
restore a backup instead of deleting it.
