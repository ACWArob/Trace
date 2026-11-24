# Hosted friends beta

This runbook prepares one always-on server for a small invited beta. The hosted
server, domain, offsite backup destination, store accounts, and real phone tests
are still required. A local debug build does not establish a hosted release.

## Shape and ownership

Use a 2 GiB Ubuntu LTS DigitalOcean Droplet, with a domain pointing to its public
address. One Waitress process serves the existing Python routes through six
bounded threads on `127.0.0.1:5173`. Caddy terminates HTTPS. SQLite, cached scores,
accounts, and private libraries live outside releases at `/var/lib/trace`.

The administrator owns code, environment files, signing keys, and recovery
material. The unprivileged `trace` service can write its database and private
temporary files. Its code directory is read-only to that user. Keep SSH restricted
to the administrator; expose ports 80/443 for Caddy and never expose port 5173.
Follow DigitalOcean's [server setup](https://docs.digitalocean.com/products/droplets/getting-started/recommended-droplet-setup/)
and [firewall guidance](https://docs.digitalocean.com/products/networking/firewalls/getting-started/quickstart/).

## Prepare the release on the development machine

The working tree can be packaged without committing or pushing it. The package
script copies an explicit reviewed list of backend modules, public app modules,
sample assets, and operations files. It excludes `.trace`, databases, credentials,
private exports, screenshots, native signing files, dependencies, and Git metadata.
Symlinks and existing output files are rejected. The owner-only archive contains
a SHA-256 manifest; hashes detect corruption, not the trustworthiness of an
archive obtained from somebody else.

```sh
python3.11 scripts/package_server.py --output trace-beta-20261124.tar.gz
python3.11 scripts/package_server.py --verify trace-beta-20261124.tar.gz
```

Use a new versioned filename for every build. Upload it through authenticated SSH
to the administrator's server home. Keep the printed archive digest with the
release notes. If the packager reports a missing file, finish that reviewed
release input before uploading; do not replace the allowlist with a directory
copy. Publish the package or native build only after its release review passes.

## Server preparation

For a **fresh Ubuntu 24.04+ VPS**, `scripts/bootstrap_server.py` automates the
first installation below. Transfer the verified archive and both
`scripts/bootstrap_server.py` and `scripts/package_server.py` over SSH into the
same private administrator directory. Run the default dry-run first:

```sh
python3 bootstrap_server.py --archive /root/trace-beta-20261124.tar.gz --sha256 YOUR_VERIFIED_SHA256 --domain YOUR_REAL_DOMAIN --release 20261124-beta1
```

Review the plan, then run the same command as root with `--apply`. It installs
official signed-repository Caddy, Python/Waitress, and age; creates the unprivileged
service and private persistent directories; configures invite-only HTTPS; and
checks both local and public health. It refuses existing Trace/Caddy installations
before installing packages, preserves Caddy's newly installed default, and does
not modify SSH or firewall rules. DNS and ports 80/443 must already reach the VPS.
Provider keys, public catalog transfer, offsite backups, native signing, and phone
checks remain explicit follow-up steps. If it fails partway through, inspect the
server; do not rerun it as an upgrade or delete the partial installation blindly.

The manual steps below are an alternative to the bootstrap, not additional steps
to run after it.

Install Python 3.11 or newer, its virtual-environment package, and Caddy from the
[official Caddy Ubuntu instructions](https://caddyserver.com/docs/install#debian-ubuntu-raspbian).
The following server commands assume an administrator with sudo, a verified
archive at `$HOME/trace-beta-20261124.tar.gz`, and the name `20261124-beta1` for
this release. Review the domain and paths before running them.

```sh
sudo useradd --system --user-group --home-dir /var/lib/trace --shell /usr/sbin/nologin trace
sudo install -d -o root -g root -m 0755 /opt/trace /opt/trace/releases /opt/trace/releases/20261124-beta1
sudo install -d -o trace -g trace -m 0700 /var/lib/trace /var/backups/trace
sudo install -d -o root -g root -m 0700 /etc/trace
sudo tar --extract --gzip --file "$HOME/trace-beta-20261124.tar.gz" --directory /opt/trace/releases/20261124-beta1 --strip-components=1 --no-same-owner
sudo sh -c 'umask 022; python3 -m venv /opt/trace/venv && /opt/trace/venv/bin/python -m pip install -r /opt/trace/releases/20261124-beta1/requirements-runtime.txt'
sudo ln -s /opt/trace/releases/20261124-beta1 /opt/trace/current
sudo install -o root -g root -m 0600 /opt/trace/current/deploy/trace.env.example /etc/trace/trace.env
```

The explicit modes and the `umask 022` wrapper keep `/opt/trace` and the virtual
environment readable by the `trace` service when the administrator's default umask
is `077`. The release extraction commands assume the owner-created archive was
verified before upload. On later releases, choose a new directory and preserve the previous
one. Reuse the `trace` account and virtual environment; do not rerun `useradd`.

Edit `/etc/trace/trace.env` privately. Set the real HTTPS origin and keep
`TRACE_SIGNUP_MODE=invite`. Put provider API keys in this root-readable environment
file, using the provider's server settings; never place them in JavaScript or a
native package. The app identifier and API origin are public configuration.
`TRACE_LIBRARY_BYTES` defaults to 100 MiB per account for this beta. Available disk
space and total account count still require operator monitoring.

```sh
sudoedit /etc/trace/trace.env
sudo install -o root -g root -m 0644 /opt/trace/current/deploy/trace.service /etc/systemd/system/trace.service
sudo systemctl daemon-reload
sudo systemctl enable --now trace
sudo systemctl status trace --no-pager
```

For a configuration-only check, load the root-owned environment in a protected
administrator shell and run `production.py --check`. It checks hosted origin,
production mode, proxy mode, signup mode, and storage configuration without
opening a socket or database. The service itself performs the same validation.
Inspect bounded logs with `journalctl -u trace -n 50 --no-pager`; do not copy
credentials, cookies, private records, or full request payloads into support logs.

## HTTPS proxy

Copy `deploy/Caddyfile` into `/etc/caddy/Caddyfile`. Set `TRACE_DOMAIN` to the same
hostname used in `TRACE_PUBLIC_ORIGIN`, without `https://`, through Caddy's systemd
environment override. The setting is public, for example
`Environment=TRACE_DOMAIN=trace.example.com` under `[Service]` in
`sudo systemctl edit caddy`. Use your actual domain, then reload systemd and Caddy.

```sh
sudo install -o root -g root -m 0644 /opt/trace/current/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo env TRACE_DOMAIN=trace.example.com caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl restart caddy
curl --fail https://trace.example.com/api/health
```

Replace the example hostname in both the override and the validation command.
Caddy gets its certificate only when DNS and ports 80/443 are reachable. It
overwrites `X-Trace-Client-IP` using the network peer and preserves the public
Host. Trace trusts that custom header only from the loopback proxy in production;
incoming forwarding headers are never a general trust boundary. Keep the service
loopback-bound, even when changing firewall rules.

## Catalog, accounts, and existing device shelves

The new server starts with a new private database. Transfer only reviewed public
catalog metadata using `scripts/public_catalog_transfer.py`. Export on the laptop,
transfer the new owner-only JSON file over SSH, then import it as the `trace` user
on the server. Use explicit paths, for example:

```sh
python3.11 scripts/public_catalog_transfer.py export --source .trace/trace.sqlite3 --output public-catalog.json
```

On the server, after a destination backup:

```sh
sudo -u trace /opt/trace/venv/bin/python /opt/trace/current/scripts/public_catalog_transfer.py import --source /var/lib/trace/public-catalog.json --destination /var/lib/trace/trace.sqlite3
```

Stage the transfer at that private path with the correct ownership first. The
transfer excludes accounts, sessions,
private libraries, provider credentials, and the bulk IMDb dataset. Back up the
destination before importing. Do not upload the laptop's entire SQLite database
as a shortcut.

Create individual expiring invitations with `scripts/manage_beta.py --output`.
Run it as `trace` with `TRACE_DATA_DIR=/var/lib/trace` and a new owner-only output
path. Its output file contains a credential; deliver it privately and never put
it in a public download link, screenshot, shell history, or Git. Invites are
single-use. Recovery codes are shown once at registration and should be saved
by the account owner. Account deletion requires reauthentication and explicit
confirmation in Trace. Deletion and old encrypted-backup retention are separate
operator responsibilities; see the backup policy.

Your current laptop shelf remains device-local until you export it or explicitly
copy it into your signed-in hosted account. Sign into the hosted app, preview an
import of your Trace JSON backup, and verify counts, dates, notes, and photos.
Never publish the import as reviews automatically. Friends start with their own
empty private shelves or explicitly imported libraries.

## Backups and restore drill

Follow [backup and restore operations](backup-restore.md). Configure an `age`
public recipient and an off-server SFTP destination in `/etc/trace/backup.env`.
Keep the matching age **private identity off the application server** in an
operator-controlled recovery location. Install `age` and OpenSSH using the
server's package manager. Do not disable SSH host-key checks.

The `trace` user's home is `/var/lib/trace`. Place its restricted backup SSH
identity/config and verified `known_hosts` there with owner-only permissions, or
use an equivalent operator-managed OpenSSH configuration. The remote backup user
should have access only to its backup destination. The timer refuses to run
without encryption and an offsite target.

Fetch and verify the Storage Box host key before anything connects. Hetzner
Storage Boxes serve SFTP on port 22 and also on port 23; use the port your
`TRACE_BACKUP_REMOTE` host and SSH config select. Replace the host below:

```sh
sudo install -d -o trace -g trace -m 0700 /var/lib/trace/.ssh
ssh-keyscan -t ed25519 -p 23 uXXXXXX.your-storagebox.de > "$HOME/storagebox.hostkey"
ssh-keygen -lf "$HOME/storagebox.hostkey"
```

Compare that fingerprint against the one in the provider console, from a trusted
channel, before trusting it. Only then install it:

```sh
sudo install -o trace -g trace -m 0600 "$HOME/storagebox.hostkey" /var/lib/trace/.ssh/known_hosts
rm "$HOME/storagebox.hostkey"
```

The backup script passes no `-P` option, so select port 23 and the key in
`/var/lib/trace/.ssh/config`. Create the key and config as `trace` so OpenSSH
accepts their owner and mode, then add `id_ed25519.pub` to the Storage Box:

```sh
sudo -u trace ssh-keygen -t ed25519 -N '' -f /var/lib/trace/.ssh/id_ed25519
sudo -u trace sh -c 'umask 077; cat > /var/lib/trace/.ssh/config' <<'EOF'
Host uXXXXXX.your-storagebox.de
    Port 23
    IdentityFile /var/lib/trace/.ssh/id_ed25519
EOF
sudo cat /var/lib/trace/.ssh/id_ed25519.pub
```

Confirm access with one manual session before enabling the timer, then `ls` and
`exit`: `sudo -u trace sftp -oBatchMode=yes tracebackup@uXXXXXX.your-storagebox.de`.
Do not use `StrictHostKeyChecking=no` or `accept-new`.

After that succeeds and remote retention is configured (see backup-restore.md),
install the units and enable the timer, then run one real job:

```sh
sudo install -o root -g root -m 0644 /opt/trace/current/deploy/trace-backup.service /opt/trace/current/deploy/trace-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trace-backup.timer
sudo systemctl start trace-backup.service
```

Create the failure-alert unit described in backup-restore.md first, because the
service declares `OnFailure=trace-backup-alert.service`.

Restore one encrypted backup to a new temporary database and run its integrity
check before accepting testers. Confirm the offsite copy exists and can be read
using the separately stored recovery identity. Define retention and disk alerts;
the script deliberately does not automatically delete backups or remote files.
A backup on the same VM is insufficient protection against loss of that VM.

## Phone builds and release gate

Build both mobile packages with the final hosted origin, following
[native setup](native-apps.md). Keep `TRACE_NATIVE_API_ORIGIN` set during the
entire native sync/build command; an ordinary unconfigured sync makes an
offline-only build. Do not reuse a test installation whose pinned API origin
belongs to a different server. Export pending offline edits before switching
installations.

Before inviting friends, record a passing result for each check:

- HTTPS health endpoint works over mobile data with the laptop shut down.
- Host/origin checks, secure session cookies, invites, login, recovery, and logout
  work through Caddy. Two users cannot read each other's library, notes, or photos.
- Signed Android install/update works with the same signing identity. A real
  iPhone can use the hosted home-screen app, and its native TestFlight build has
  been tested when Apple signing is available.
- Add a photo memory, reopen it after an app restart, edit offline, and reconnect.
  Pending edits survive; private entries do not appear in the public feed.
- Search/import a book, album, movie, and TV show. Verify source links and artwork
  identity, rating persistence, Tracewall image export, keyboard, and Android Back.
- Restart the cloud service and verify data persists. Restore the offsite backup
  into an isolated database. Confirm free disk space and current backup status.
- Replace placeholder operator/support details in the public privacy/support
  pages. Record provider-data permissions, current beta limits, and known gaps.
- Fresh security/diff review passes. Runtime checks and affected tests pass for
  the exact packaged release.

Account recovery cannot be supported by the operator without the owner's saved
recovery code. Google sign-in, email recovery, store enrollment/review, public
store approval, and unavailable physical-device checks are separate work. Keep
the beta invite-only while any release gate remains unresolved.

## Rollback

Keep the previous code directory and the verified pre-deploy database snapshot.
For a code-only rollback, stop `trace`, switch `/opt/trace/current` to the verified
previous release, then restart and run health/login/library checks. Preserve the
live `/var/lib/trace` database when its schema is backward-compatible.

For incompatible schema changes or data corruption, stop writes first. Restore
as the `trace` user into a new `0700` directory under `/var/lib/trace` (the only
path the service can write), with the file named `trace.sqlite3`; retain the
current database and its WAL/SHM files for investigation rather than overwriting
or deleting them. Follow the exact commands in
[Recovering the live service](backup-restore.md#recovering-the-live-service):
restore with `sudo -u trace ... backup_database.py restore`, copy
`mdblist-api-key` across with `cp -p`, set `TRACE_DATA_DIR` in
`/etc/trace/trace.env` (it overrides the unit's `Environment=`), restart, check
`/api/health`, and revoke sessions. Switch only after the operator approves the
loss of writes newer than that snapshot. Never restore over a live database or
assume reverting code also reverts data.
