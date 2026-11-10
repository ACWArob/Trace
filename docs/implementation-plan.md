# Trace: durable data, private account libraries, and mobile installation

Status: database/account-sync and home-screen-app foundation implemented; native iOS/Android debug projects now build. Physical-device checks and distribution remain pending. Updated 2026-11-04.

## Outcome

A movie's provider scores become shared SQL records, reused across accounts and server restarts. Each signed-in person gets a private library that synchronizes without publishing their notes. Trace can be installed on iPhone/Android home screens and opens its saved interface offline.

This work ships the database/account-sync foundation, a progressive web app (PWA), and subsequently approved native debug projects. Store distribution remains a subsequent release phase. Separate devices need access to the same hosted HTTPS backend; localhost remains local development.

## Team and ownership

At least five specialists work in batches, with at most three specialists plus the coordinating agent active at once.

| Specialist | Owns | Acceptance evidence |
| --- | --- | --- |
| 1. Persistent ratings | `ratings.py`, rating tests | Scores/backoff survive restart; stale successful records survive outages |
| 2. Account library backend | New `library_store.py`, backend tests | Two-user isolation, revisions, tombstones, validation, additive migration |
| 3. Mobile installation | Manifest, service worker, mobile module, app icons, worker tests | Public app shell offline; API/auth/private data never enter worker caches |
| 4. Browser synchronization | New `library_sync.mjs`, synchronization tests | Durable IndexedDB queue, conflicts, account switches, offline replay |
| 5. Security/diff review | Independent final diff review | Authentication, CSRF, private-data boundaries, caching and conflict findings resolved |
| Coordinator | HTTP integration, existing UI/session integration, documentation, browser QA | End-to-end desktop/phone and two-context checks; preserve the current guest shelf |

Agents edit separate files. Changes to shared integration files belong to the coordinator. No commits, pushes, paid infrastructure, or store submissions are included without a separate request.

## Existing system and design decisions

- Keep Python standard library + browser JavaScript. Keep the existing SQLite database and current account/session system.
- Preserve catalogs, imported IMDb records, reviews, comments, follows, and account credentials.
- Extend the schema additively. Use idempotent initialization; a database backup precedes applying the new tables to the real development database.
- Keep movie consensus unchanged: IMDb, RT critics, RT audience, Letterboxd; equal weights among available feeds; two feeds minimum for a Trace blend.
- Shared metadata and provider scores are public. Library ratings, notes, dates, histories, memory photos, and attachments are private to their account.
- A local guest library remains available. Signing in never automatically uploads the current device's collection or posts reviews.
- Use IndexedDB for private account caches/queues so copying a large guest shelf does not exhaust localStorage. Existing guest localStorage remains untouched until the user edits it.

## Phase 1 — Persistent provider scores

Files: `ratings.py`, `server.py`, focused rating/server tests.

1. Add a provider response table keyed by provider, media kind, and IMDb ID. Store native scores, provenance, original links, fetched time, and next refresh time.
2. Add a provider account-backoff table for throttling/invalid-key cooldown across restarts.
3. Read saved results immediately. Fresh results do not call providers again. Successful scores refresh after seven days; successful missing responses use a shorter deadline.
4. Return stale saved scores while a bounded background worker refreshes. Coalesce refreshes for the same provider/title. Keep successful records if a provider fails.
5. On the first lookup of an uncached title, fetch only the required provider records and store the response. Do not batch-enrich the entire imported library on startup.
6. Show source timestamps/stale status in the breakdown. Never persist or return a provider key in a response or cache payload.

Checks: close/reopen a temporary database; assert subsequent lookup avoids another provider request; verify title/provider separation; simulate time passing, restart, 429/backoff, malformed responses, and outages with stale values retained.

## Phase 2 — Private account library database

Files: new `library_store.py`; `server.py`; new library tests and HTTP tests.

Add per-user library records and a per-user monotonic cursor. A record contains its existing entry ID, normalized private payload or deletion tombstone, and a revision. Keep the client entry ID so imports, histories, memory attachments, and links remain stable.

### HTTP contract

- `GET /api/library?after=0&limit=200` requires an authenticated session. Returns `{changes:[{id,entry,revision}],cursor,hasMore,total}`. `entry:null` is a deletion tombstone.
- `POST /api/library/sync` requires an authenticated session, same-origin request and matching CSRF token. Body: `{changes:[{id,entry,baseRevision}]}`. Maximum 100 records per batch; bounded JSON body and per-entry size.
- The server derives `user_id` from its session, never from a submitted username/account ID.
- Accepted writes/deletions receive the next user cursor as their revision. Stale edits are returned as conflicts instead of overwriting account data.
- Response: `{accepted:[{id,entry,revision}],conflicts:[{id,entry,revision}]}`.
- Pull returns the latest version of each changed record, including tombstones, in increasing revision order. Continue pages until `hasMore:false`.

Limits: 30,000 live entries/account, 1.5 MB per entry, validated IDs/revisions/media types/dates/ratings, bounded notes/history/attachments. Reject malformed input explicitly instead of silently losing private fields. Preserve original unknown dates as blank.

Checks: two users with identical entry IDs; cross-account access; missing/expired session; wrong origin/CSRF; concurrent edits; idempotent retry/conflict; deletion propagation; rollback on invalid batches; all private fields survive reload. Social feeds remain unchanged by private library sync.

## Phase 3 — Browser sync and account UX

Files: new `library_sync.mjs`; `app.js`, `social.js`, `styles.css`; focused sync tests.

1. Use one IndexedDB account cache per authenticated user. Guest collections stay in their existing storage key.
2. Track each entry's server revision, queued local mutations, sync cursor, and unresolved conflicts. Persist locally before reporting a save.
3. Pull server changes, push bounded batches, then pull again. Keep new local edits made during an in-flight request queued against the newly accepted revision.
4. Persist deletions as queued tombstones. Retry transient errors on reconnect. A 401 stops sync and requires sign-in; never replay one account's queued changes into another account.
5. On conflict, preserve the device version and the server version. Provide explicit choices to keep the device version, use the account version, or keep both.
6. Add clear states: saved on device, saving, syncing, synced, offline/pending, conflict, sign-in required. Keep the current visual style and keyboard access.
7. Offer an explicit copy of the device shelf into the signed-in account, with item counts and a merge preview. The original guest shelf remains available when signed out.
8. Scope personal connection/recap preferences to the current account. Public posting remains an explicit separate action.

Checks: sign in/out/account switch; two independent clients; offline add/edit/delete followed by reconnect; change made during a push; conflict resolution; refresh with a pending queue; guest import without lost IDs/ratings/notes/dates/photos; storage failure produces an honest save error.

## Phase 4 — iPhone/Android home-screen app

Files: `manifest.webmanifest`, `sw.js`, `mobile.js`, icon assets; `index.html`, `server.py`, `styles.css`, README; worker tests.

1. Add install metadata, icons, standalone display, safe-area support, and platform-appropriate install guidance.
2. Precache a fixed list of public application-shell files. Use versioned Trace-owned cache names and a controlled update message.
3. Never put `/api`, sessions, POST responses, private library JSON, uploaded memory images, or credentials into service-worker CacheStorage. IndexedDB owns private offline library state.
4. Offline navigation opens the saved shell and account/guest cache. Catalog searches and public feed requests report that a connection is needed.
5. Offer an update action; do not force a reload over an unsaved edit or erase IndexedDB during an app update.
6. Test 320px/390px layouts, safe areas, dialogs, installation metadata, offline launch, and return online. Do not claim a native device/store build was tested when only browser checks were available.

## Phase 5 — Integration, review, and handoff

- Back up the real SQLite database with SQLite's backup API before additive tables are initialized there. Keep the backup ignored and private.
- Test writes use temporary databases and isolated origins. Never create synthetic reviews/accounts in the user's real database.
- Verify a real four-source movie lookup, then restart the server and confirm its persisted response is reused.
- Verify the existing 1,211-entry guest shelf by counts and preservation checks without printing private content or uploading it automatically.
- Run affected focused tests, syntax checks, and `git diff --check`. Broaden tests only where shared changes warrant it.
- Get a fresh inexpensive diff-only security review and fix actionable findings.
- Refresh README with storage locations, migration/backup steps, account sync behavior, recovery/conflict guidance, installation instructions, and deployment limitations.

## Subsequent release phase

Prepare production hosting/HTTPS, account recovery and abuse controls, backups, and operational configuration before inviting external users. Validate the native projects against that hosted API on physical devices and prepare store signing/submission. Provisioning paid services and store submission require explicit approval.

Native packaging was subsequently approved and implemented: local Capacitor dependencies, explicit public-only bundle, iOS/Android projects and branding, native HTTP transport with existing cookie/CSRF checks, per-install server pinning, external source browser, Android Back, and foreground reconnect. Web remains buildless. See [native setup](native-apps.md).

The iOS simulator and Android debug builds passed. A separate synthetic iOS native-HTTP probe passed nine checks covering login/cookie persistence, private reads/writes, missing CSRF rejection, tombstones, logout/cookie clearing, and two-account isolation. iPhone simulator launch was visually checked; UI clicks were blocked by the control tool. Android emulator launch is blocked by its installed Intel/Rosetta tooling. Physical devices, hosted HTTPS, end-to-end native offline queues/attachments, release signing, recovery/deletion, and store submission remain unverified. The final local package defaults to device-only because no production origin is configured. Fresh inexpensive native diff review found no actionable issues.

## Completion checklist

- [x] Persistent ratings and durable provider backoff
- [x] Private library schema and authenticated API
- [x] IndexedDB queue and conflict-safe synchronization
- [x] Explicit guest-to-account copy and account isolation UI
- [x] Installable home-screen app and offline shell
- [x] Temporary-database/two-client/phone-width checks (physical-device cold offline launch remains unconfirmed)
- [x] Real database backup and non-destructive migration verification
- [x] Fresh security review resolved
- [x] README and screenshots updated; changes remain uncommitted/unpushed

## Validation notes

Five specialist agents contributed. Larger-model follow-up sessions hit an account usage limit; the coordinator integrated their saved work and the inexpensive security reviewer completed both reviews. Review findings on HTTPS origins, rapid asynchronous saves, and decimal/ binary batch limits were fixed with regression tests. Browser validation also caught and fixed a profile-label selector and public-cache headers that had prevented shell precaching.

Real database backup/migration preserved original table counts. A real four-source Moonlight lookup kept the same fetched timestamp after a process restart. The real guest UI still shows 1,211 entries and was not uploaded automatically. Isolated Alice/Bob accounts verified private libraries, explicit device-shelf copying, sign-out restoration, offline edit/reconnect, and conflict review with Keep both. Cross-tab sign-out hides the private account view in the other open tab; atomic IndexedDB transactions preserve both tabs’ offline queues.

Cold offline navigation in the embedded browser failed before cache-header correction; its synthesized error-page navigation could not provide a reliable subsequent cold-launch check. Worker/HTTP harness checks now pass, but physical Safari/Chrome cold offline launch and native-device/store builds remain release validation tasks.
