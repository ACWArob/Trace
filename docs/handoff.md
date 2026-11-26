# Trace IDE handoff

Updated November 24, 2026. Trace is a local-first shelf and diary for movies, TV and anime, books, albums, and photo memories. The immediate goal is an invite-only friends beta that works with the laptop off. The public Android and iOS release follows, with capacity to be tested for up to 1,000 daily active users.

## Open the current checkout

Open `/Users/adamw/Documents/ChatGPT/Trace` in the other IDE. Read [AGENTS.md](../AGENTS.md) and run `git status --short`. The branch is `main`, the remote is [ACWArob/Trace](https://github.com/ACWArob/Trace), and the last committed revision at handoff is `41e9676`. The current checkout contains substantial modified **and untracked** release work. A fresh clone alone lacks that work. Preserve it and get approval before committing, pushing, or moving it to another machine.

The Codex schedule “Continue building Trace” runs daily at 10 AM Central on this workspace. Coordinate active editing between IDEs and check Git status before starting a change.

## Run and orient

From the repo root on this Mac, start an isolated preview. Use the exact preview URL below so its browser storage is separate from the normal shelf on port 5173.

```sh
trace_preview_data=$(mktemp -d)
TRACE_DATA_DIR="$trace_preview_data" python3.11 server.py 5187
```

Open [the isolated preview](http://127.0.0.1:5187/). The web app uses browser JavaScript and CSS with a Python server and SQLite. Native apps use Capacitor. Start with the [README](../README.md) for product behavior and [native development setup](native-apps.md) for Android Studio, Xcode, and local debug builds.

| Area | Start here |
| --- | --- |
| Shelf, Cinema/TV rooms, Diary, import, and Tracewall | `app.js`, `styles.css`, `core.mjs`, `views.mjs`, `letterboxd_zip.mjs` |
| Catalog, cached public scores, and providers | `server.py`, `catalog_store.py`, `screen_catalog.py`, `book_catalog.py`, `music_catalog.py`, `ratings.py`, `score_service.py` |
| Private library and shared social features | `library_store.py`, `library_sync.mjs`, `social.py`, `social.js` |
| Native config, API transport, and release builds | `native/`, `native_transport.mjs`, `scripts/build-native.mjs`, `scripts/prepare-release.mjs` |
| Hosted runtime, invites, and backup | `production.py`, `release_policy.py`, `deploy/`, `scripts/bootstrap_server.py`, `scripts/manage_beta.py`, `scripts/backup_database.py` |

## Pick up the beta

1. **Hosting is the first dependency.** The owner needs to finish DigitalOcean sign-in and approve the host and domain. Follow the [deployment runbook](deployment.md) for the fresh Ubuntu VPS, HTTPS, and invite-only configuration. The app has yet to be deployed.
2. **Use the reviewed release artifacts.** This Mac’s ignored `.trace/release-status.json` records the archive path, SHA-256, public catalog, and checks. The archive is `.trace/releases/trace-beta-20261113-v2.tar.gz`. The companion catalog export contains 2,011 public records. Confirm the package still matches the intended source before deploying. Transfer the public export into a new server database. Keep the full local database and personal shelf on the owner’s device unless they explicitly choose to move the shelf.
3. **Validate the hosted service.** Check HTTPS and invite signup, test with a synthetic account, and configure and restore a real encrypted offsite backup. Follow the [backup runbook](backup-restore.md) and configure provider credentials privately on the server.
4. **Build both phones against the live HTTPS origin.** Follow the [friends beta guide](friends-beta.md) for `scripts/prepare-release.mjs`, Android signing, and TestFlight. Check sign-in, photo permissions, offline edits, and updates on physical phones over mobile data. Current builds have passed local debug/simulator checks. Signed, hosted builds and physical-device tests are pending.
5. **Use the public launch plan after the friends beta.** It covers PostgreSQL, private photo storage, durable provider jobs, moderation, store submissions, and sustained and burst traffic tests. [Public launch plan](public-launch-plan.md)

## Verification to carry forward

The packaged server already passed a local HTTPS run with Caddy and Waitress. Two synthetic accounts could use the public catalog, and a private rating survived restart without appearing in the other account. A four-second run at 30 requests per second passed all 120 requests and verified 20 saves. The separate shared-IP run returned 30 rate-limit responses out of 270 reads. Measure shared-network traffic in hosted staging before claiming capacity. The iPhone camera purpose string is present in the built simulator app. Camera permission and capture still need a physical device.

Run the focused check for the area you change. For example:

```sh
node --test tests/release_build.test.mjs
python3.11 -m unittest discover -s tests -p 'test_production.py'
python3.11 scripts/package_server.py --verify .trace/releases/trace-beta-20261113-v2.tar.gz
git diff --check
```

## Paste into the next IDE

> Read `AGENTS.md` and `docs/handoff.md` in the current Trace checkout. Inspect Git status and `.trace/release-status.json`. Preserve all existing work, personal data, ratings, notes, photos, dates, and IDs. Continue with the invite-only hosted beta, then prepare connected Android and iOS builds against the same HTTPS server. If cloud access is blocked, work on an independent release check or an already approved improvement. Verify using synthetic data and a temporary database. Read `AGENTS.md` for approval and review rules before changing files, infrastructure, or data. Keep credentials and private exports out of prompts, logs, packages, and Git.
