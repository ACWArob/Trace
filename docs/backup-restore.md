# Trace database backups and recovery

The shared `trace.sqlite3` contains accounts, private libraries/photos, public
catalog records, provider caches, and published social content. A backup is
private account data. Keep it outside Git and the web root. Provider API keys and
signing keys are separate and are not included in this database snapshot.

The script uses SQLite's [online backup API](https://www.sqlite.org/backup.html)
so committed WAL changes are included while Trace runs. It checks the resulting
database, writes owner-only files, and publishes each local snapshot atomically
without replacing an existing destination. It does not print database rows,
passwords, recovery codes, or encryption keys.

## Local safety snapshot

Use Python 3.11 or newer. From the repository root:

```sh
python3.11 scripts/backup_database.py backup --output-dir .trace/backups
```

`TRACE_DATA_DIR` selects the source directory. `--source /path/trace.sqlite3`
selects an explicit existing source. Each output has a UTC timestamp and random
suffix. This command creates an **unencrypted local safety copy** with mode
`0600`; it is not protection against losing the server or its disk.

## Encrypted off-server backups

The deployment backup job requires both encryption and offsite upload; it fails
instead of silently falling back to local-only storage. Before enabling it:

1. Confirm `age` and OpenSSH's `sftp` are installed. `scripts/bootstrap_server.py`
   installs `age` and `openssh-client`; a manual install must add them from the
   operating system's trusted package repository. See the
   [age manual](https://github.com/FiloSottile/age/blob/main/doc/age.1.ronn).
2. Generate an age identity **on a separate trusted recovery device**. Retain a
   secure second copy of that private identity. The server needs only its public
   `age1…` recipient; a lost private identity makes the backups unrecoverable.
3. Provision an independent SSH/SFTP backup destination. The remote account should
   have access only to its backup directory. Keep it on a separate server or
   provider, with sufficient quota. Restrict deletion through destination policy
   or snapshots where available; the app host's upload credential alone does not
   provide immutable backup storage.
4. Provision the `trace` service user's SSH authentication under
   `/var/lib/trace/.ssh`. Verify the remote host's fingerprint through a trusted
   channel and add that verified key to `known_hosts`. The job uses batch mode and
   strict host-key verification; it cannot answer an interactive prompt.
5. Create `/etc/trace/backup.env` owned by root with mode `0600`, containing the
   public recipient and remote destination. Enter actual values privately:

```ini
TRACE_BACKUP_AGE_RECIPIENT=age1YOUR_PUBLIC_RECIPIENT
TRACE_BACKUP_REMOTE=tracebackup@backup.example:/trace/backups
```

Create `/var/backups/trace` owned by `trace:trace` with mode `0700`. The service
expects the application at `/opt/trace/current` and its environment at
`/opt/trace/venv`; adjust both unit paths together if your deployment differs.

The script snapshots first, encrypts using age, removes its own plaintext
staging file, and then uploads only the encrypted file. SFTP uploads to a unique
`.partial` name and renames it after completion. An upload failure leaves the
local encrypted copy for recovery; incomplete remote transfers do not have the
final `.age` filename.

After configuration, run a real test job before enabling the timer:

```sh
sudo systemctl start trace-backup.service
sudo systemctl status trace-backup.service
sudo systemctl enable --now trace-backup.timer
sudo systemctl list-timers trace-backup.timer
```

The supplied unit runs at 03:30 UTC with up to 15 minutes of randomized delay and
catches up after a missed run. Install both files from `deploy/` into the system
unit directory and run `systemctl daemon-reload` first. The service passes
`--timeout 900` to each stage (snapshot, encryption, upload), so
`TimeoutStartSec=50min` covers all three. A timer's presence does not establish
that encryption, upload, or restore worked.

### Failure and missed-run alerts

`trace-backup.service` declares `OnFailure=trace-backup-alert.service`, so a failed
or timed-out run starts that unit. It does not exist until you create it. Create a
check at a dead-man's-switch service such as healthchecks.io with a one-day period
and a few hours of grace, and put its private ping URL in
`/etc/trace/backup.env` (root-owned, mode `0600`):

```ini
TRACE_BACKUP_PING_URL=https://hc-ping.com/YOUR_CHECK_UUID
```

Then create `/etc/systemd/system/trace-backup-alert.service`:

```ini
[Unit]
Description=Alert that the Trace backup failed

[Service]
Type=oneshot
User=trace
Group=trace
EnvironmentFile=/etc/trace/backup.env
ExecStart=/usr/bin/curl --fail --silent --show-error --max-time 20 --retry 3 --data-raw 'Trace backup failed' ${TRACE_BACKUP_PING_URL}/fail
NoNewPrivileges=true
PrivateTmp=true
```

For the missed-run alert, add a success ping with `sudo systemctl edit trace-backup.service`:

```ini
[Service]
ExecStartPost=-/usr/bin/curl --fail --silent --max-time 20 --retry 3 ${TRACE_BACKUP_PING_URL}
```

The leading `-` keeps a ping outage from failing a good backup; the check then
alerts when pings stop arriving. Run `sudo systemctl daemon-reload`, then test the
alert path with `sudo systemctl start trace-backup-alert.service` before inviting users.

### Retention

After a successful run the unit deletes local `trace-*.age` files older than seven
days from `/var/backups/trace`. A failed run does not prune, so monitor local disk
space; the script refuses to start with less than 128 MiB free, which is a minimum
safety check, not a storage-quota guarantee.

**Remote retention is a required step before enabling the timer.** Nothing prunes
the offsite directory, so it grows without limit until you configure it. A
reasonable beta policy is seven daily and four weekly recovery points, plus a
pre-upgrade copy. Enforce it with the provider's snapshot or expiry feature (for a
Hetzner Storage Box, snapshots and a snapshot plan in the provider console), or
with a cleanup job that runs from the operator's own device using a credential the
app host does not hold. Record the policy you chose and test that old files are
actually removed or retained as intended.

## Restore rehearsal

Download an encrypted backup to a trusted recovery device. Restore it into a
**new path**, never the live database. Keep the age identity owner-only:

```sh
chmod 600 /secure/trace-backup-identity.txt
python3.11 scripts/backup_database.py restore \
  --source /secure/trace-20261113T033000Z-example.sqlite3.age \
  --identity /secure/trace-backup-identity.txt \
  --destination /secure/rehearsal/trace.sqlite3
```

For an unencrypted local snapshot omit `--identity`. Existing files, symlinks,
corrupt SQLite data, and decryption failures are rejected. SQLite integrity is
verified before publishing the restored file. An intact database establishes
storage consistency; additionally exercise sign-in and library reads against an
isolated local server. Do not post, send mail, or call paid providers during the
rehearsal.

Perform and record a real offsite download/decrypt/restore rehearsal before the
friends beta and after backup configuration changes. The automated tests exercise
live WAL snapshots, no-overwrite restoration, and simulated offsite transport.
A real age 1.3.2 encryption/decryption and SQLite restore drill also passed on
2026-11-13 using synthetic data and a test-only identity. External storage delivery
still needs a configured destination and a real restore drill.

## Recovering the live service

A live replacement requires the owner's approval. The service can write only under
`/var/lib/trace` (`ProtectSystem=strict`), reads the database as `trace.sqlite3`
inside `TRACE_DATA_DIR`, and must own that file, so restore **as the `trace` user
into a new directory under `/var/lib/trace`**. A `sudo` restore would leave the
database root-owned and unusable.

The age private identity stays off the application server. Decrypt and verify the
backup on the recovery device with the rehearsal command above, then copy only the
resulting `trace.sqlite3` to the administrator's home on the server as
`restore-input.sqlite3`. Then on the server:

```sh
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESTORE=/var/lib/trace/restore-$STAMP
sudo systemctl stop trace
# Keep the current database and its WAL/SHM files together for investigation.
sudo install -d -o trace -g trace -m 0700 "$RESTORE-previous"
sudo sh -c 'cp -p /var/lib/trace/trace.sqlite3* "$1"' sh "$RESTORE-previous"
# Restore as trace into a new 0700 directory; the file must be named trace.sqlite3.
sudo install -d -o trace -g trace -m 0700 "$RESTORE"
sudo install -o trace -g trace -m 0600 "$HOME/restore-input.sqlite3" "$RESTORE/input.sqlite3"
sudo -u trace /opt/trace/venv/bin/python /opt/trace/current/scripts/backup_database.py restore \
  --source "$RESTORE/input.sqlite3" --destination "$RESTORE/trace.sqlite3"
sudo rm "$HOME/restore-input.sqlite3" "$RESTORE/input.sqlite3"
# The provider key lives beside the database; copy it with its owner and mode.
if sudo test -f /var/lib/trace/mdblist-api-key; then
  sudo cp -p /var/lib/trace/mdblist-api-key "$RESTORE/mdblist-api-key"
fi
sudo sh -c 'ls -ld "$1" "$1"/*' sh "$RESTORE"
```

Confirm the directory is `trace:trace` mode `0700` and `trace.sqlite3` is owned by
`trace`. Point both services at it. `EnvironmentFile=` overrides the units'
`Environment=TRACE_DATA_DIR=/var/lib/trace`, so add this line to
`/etc/trace/trace.env` **and** `/etc/trace/backup.env` (replace any existing
`TRACE_DATA_DIR`), using the actual `$RESTORE` value. Otherwise the next backup
snapshots the old database:

```sh
echo "$RESTORE"
sudoedit /etc/trace/trace.env     # TRACE_DATA_DIR=/var/lib/trace/restore-<STAMP>
sudoedit /etc/trace/backup.env    # the same line
sudo systemctl restart trace
curl --fail --silent --show-error https://trace.example.com/api/health
```

Use your real domain. Then revoke every restored session. The running service
creates the `sessions` table if the snapshot predates it, so run this after the
health check passes; deletion takes effect on the next request:

```sh
sudo -u trace /opt/trace/venv/bin/python -c "import sqlite3,sys; db=sqlite3.connect(sys.argv[1], timeout=30); db.execute('DELETE FROM sessions'); db.commit()" "$RESTORE/trace.sqlite3"
```

Check sign-in and a test library read before reopening access. Never overwrite a
live SQLite file or mix a restored database with old WAL/SHM sidecars. To roll back
the restore, stop `trace`, remove the `TRACE_DATA_DIR` override from both
environment files so the units' `/var/lib/trace` default applies (those original
files are untouched; `$RESTORE-previous` is a separate copy), and restart.

Restoring an older snapshot rolls back account changes and revives credentials,
sessions, and recovery codes present at that snapshot. It can also revive records
subsequently deleted by users. Revoke restored sessions (above), reconcile deletions, and
review queued device edits before restoring public access. Keep the old database
for a controlled rollback; do not merge whole databases by copying files.
