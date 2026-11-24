# Trace → Vercel + Neon + Blob migration plan

Prepared 2026-11-24 for branch `codex/friends-beta-release` (f68f7aa). This document proposes work only: no code, services, spend, or deletions have been changed. The owner's decision (not re-argued here): hosted backend moves from one VPS (Python stdlib handler + Waitress + SQLite + Caddy) to Vercel Functions (Python) + Neon Postgres (Marketplace, Free) + Vercel Blob (private) before any users exist. Static shell comes from Vercel's CDN. No hosted data is migrated; the owner's personal shelf stays in their browser/device.

> **For agentic workers:** execute step by step (superpowers:subagent-driven-development or executing-plans). Each step ends green on its own test command before the next dependent step starts. Approving this plan approves the DDL described in Step 1 (AGENTS.md: an approved described schema change needs no re-ask).

**Architecture:** `api/index.py` exports the existing WSGI adapter (`production.application`) as `app`. Stores swap `sqlite3` for a thin `database.py` over psycopg 3 with one short-lived connection per request to Neon's pooled URL. Process-local state moves to Postgres or is demoted to best-effort. Photos move from base64-in-JSON to private Blob objects referenced by `trace-photo:<sha256>` keys.

**Tech stack:** Python 3.12 on Vercel (3.11 locally), `psycopg[binary]`, Vercel Python Blob SDK, Vercel Cron, GitHub Actions (backups). Frontend stays buildless (a copy-only script stages the public files).

**Current facts used** (read from the repo): ~91 SQLite touch points in 8 modules; no `AUTOINCREMENT`, FTS, `strftime`, `lastrowid`, or `executescript` outside DDL; one registered SQL function (`catalog_rank`); `BEGIN IMMEDIATE` at 8 sites; `sqlite3.Error` caught at 8 sites in `server.py`; `python3` on this Mac is 3.8, use `python3.11` (already required by `docs/backup-restore.md`).

---

## 1. Findings (answers to the investigation questions)

### 1.1 Entry point, routing, headers, host/IP checks
- **WSGI works as-is.** `production.application(environ, start_response)` is a plain WSGI callable; `WsgiHandler` reads only `REQUEST_METHOD`, `PATH_INFO`, `QUERY_STRING`, `REMOTE_ADDR`, `HTTP_*`, `CONTENT_*`, `wsgi.input`. Vercel's Python runtime accepts a top-level `app` (WSGI/ASGI) in `api/*.py`, where `api/index.py` serves `/api` ([Python runtime](https://vercel.com/docs/functions/runtimes/python)). Vercel docs do **not** document `environ` keys, `wsgi.input` reads, or whether a rewrite preserves the original `PATH_INFO`; Step 0 verifies these before anything is built on them.
- **Routing:** `vercel.json` rewrite `/api/:path*` → `/api/index`; no framework preset (“Other”), `outputDirectory: "public"`. Static files must NOT be served from the repo root (it would publish `server.py`, `docs/`, `tests/`). `scripts/build_public.py` (copy-only, no bundling) stages `server.PUBLIC_FILES` + `assets/*` into git-ignored `public/` via `buildCommand`. Function bundle trimmed with `functions["api/index.py"].excludeFiles` (assets, html, js/mjs, docs, tests, native, node_modules, deploy). Pin `maxDuration` (30 s is plenty; platform default is 300 s, Pro max 800 s, [limits](https://vercel.com/docs/functions/limitations)) and `regions` to one region matching the Neon project.
- **Headers moving from `deploy/Caddyfile` / `server.py` into `vercel.json` `headers`:**
  | Source | Becomes |
  |---|---|
  | Caddy `X-Content-Type-Options nosniff`, `Referrer-Policy strict-origin-when-cross-origin`, HSTS `max-age=31536000` | global `/(.*)` header rule (function already sets the first two; static needs them) |
  | `server.py end_headers`: shell files `public, max-age=0, must-revalidate` | explicit rule for the shell paths (sw.js `isPublicFile` rejects `no-store`/`private`, so static must never be `no-store`) |
  | `server.py send_json`: `no-store` | stays in code; add belt-and-braces `/api/(.*)` rule |
  | `.mjs` / `.webmanifest` content types (sw.js requires JS and manifest/json types) | explicit `Content-Type` rules if the Step 0 spike shows Vercel's defaults differ |
  | Caddy `-Server`, `request_body max_size`, `header_up *` | dropped: Vercel sets `Server`; body cap is the platform's 4.5 MB ([limits](https://vercel.com/docs/functions/limitations)); proxy header plumbing is replaced below |
- **Host/Origin/CSRF** (`valid_host`, `request_origin`, `library_post`, `social_post`): logic is sound on Vercel because `Host` is the domain as the client accessed it ([request headers](https://vercel.com/docs/headers/request-headers)). Keep `TRACE_PUBLIC_ORIGIN` = the stable production origin (production only accepts that exact host; the other automatic `*.vercel.app` aliases get 403 “Unknown host”). Add `allowed_origins()`: in `VERCEL_ENV=preview` also accept `https://$VERCEL_URL` and `https://$VERCEL_BRANCH_URL`, so staging can run. Cookies stay `HttpOnly; SameSite=Lax; Secure` (derived from the https origin).
- **Client IP:** replace the loopback-proxy trust (`X-Trace-Client-IP`) with a new `TRACE_TRUST_PROXY=vercel` mode that reads `x-vercel-forwarded-for` (fallback `x-real-ip`), validated with `ipaddress` and IPv6 /64-collapsed exactly as today. Vercel documents these as the client's public IP and says it overwrites `x-forwarded-for` rather than forwarding external values; it does not say a client cannot send `x-real-ip` itself, so prefer `x-vercel-forwarded-for` and confirm in Step 0. Loopback mode stays for local tests.
- **`MAX_BODY_BYTES`** 10 MB → 4,000,000 (platform cap 4.5 MB; a 20 MiB beta exists but needs Fluid + 4 GB memory + an env flag — not relied on).

### 1.2 SQLite → Postgres inventory and translation
| SQLite usage (where) | Postgres equivalent |
|---|---|
| `sqlite3.connect`, `row_factory=sqlite3.Row`, `PRAGMA journal_mode/foreign_keys` (catalog_store, library_store, social, ratings) | `Database.connect()`; `Row` class supporting `row[0]` and `row['x']` (code uses both); FKs always on; WAL pragma dropped |
| `with db:` commit/rollback; `db.execute('BEGIN')` (library pull) | wrapper commits/rolls back/closes; pull uses `isolation='repeatable read'` for a consistent cursor+rows snapshot |
| `BEGIN IMMEDIATE` ×8 (catalog save, movie reconcile, register, issue_recovery_codes, recover, delete_account, library sync) | see locking table below |
| `?` placeholders everywhere | wrapper translates `?`→`%s` (and escapes literal `%`); keeps SQL strings diff-small; unit-tested |
| `INSERT OR REPLACE` (ratings.import_imdb), `INSERT OR IGNORE` (follows, likes) | `ON CONFLICT (...) DO UPDATE` / `DO NOTHING`; `ON CONFLICT ... DO UPDATE ... WHERE` upserts already valid |
| `json_extract(x,'$.k')`, `json_each`, `json_array_length`, `json_valid` | JSONB columns: `x->>'k'`, `jsonb_array_elements_text`, `jsonb_array_length`; `json_valid` guard removed |
| `catalog_rank` Python UDF (`create_function`) | stored `title_norm`/`creator_norm` columns (written by `catalog_search_text` at save) + SQL `CASE` using `position(word in …)`; `catalog_search_rank` stays as the test oracle |
| `COLLATE NOCASE` (catalog titles; `users.username`) | `lower(title)` + functional index; `citext` for `users.username` (unique, case-insensitive, no query changes); `display_name LIKE` → `ILIKE` (SQLite LIKE is ASCII case-insensitive, Postgres is not) |
| `REAL` timestamps/scores | `DOUBLE PRECISION` (Postgres `REAL` is 4-byte and would corrupt epoch seconds) |
| `INTEGER` revision/cursor (up to 2^53−1) | `BIGINT` (Postgres `INTEGER` would overflow) |
| `spoiler INTEGER` (bool inserted) | `BOOLEAN` (psycopg sends bool; integer column rejects it) |
| `BLOB` salt/hash | `BYTEA` (psycopg 3 returns `bytes`, so `hmac.compare_digest` is unchanged) |
| `length(cast(entry AS BLOB))`, `sum()` | `octet_length(entry)`, cast sums to `bigint` |
| `sqlite_master` probe in `delete_account` | removed; library tables always exist (single migration set) |
| `executescript` DDL in constructors | numbered `migrations/*.sql` + `scripts/migrate_db.py` (no DDL on cold start) |
| `sqlite3.IntegrityError` (duplicate username) | `database.IntegrityError` (psycopg `UniqueViolation`) |
| backup API (`scripts/backup_database.py`) | `pg_dump` (Step 11) |

**Locking equivalents** (all inside the one transaction the wrapper opens):
| Site | Replacement |
|---|---|
| `register` invite claim | one atomic `UPDATE beta_invites SET claimed_at=… WHERE invite_hash=… AND claimed_at IS NULL AND expires_at>… RETURNING 1`; no row → 403 (replaces read-then-write under global lock); user insert + `claimed_by` update follow in the same transaction |
| `recover`, `issue_recovery_codes`, `delete_account` | `SELECT … FROM users WHERE … FOR UPDATE` on the user row |
| library `sync` (per-user cursor) | after the `users` existence check: `INSERT INTO private_library_state … ON CONFLICT DO NOTHING`, then `SELECT cursor … FOR UPDATE` (serializes per user, not globally) |
| catalog `save`, `reconcile_movie_catalog` | `pg_advisory_xact_lock(hashtextextended('catalog-write',0))` (transaction-scoped, safe on Neon's transaction pooler, which does not support session advisory locks, [pooling](https://neon.com/docs/connect/connection-pooling)) |

**Connection policy:** `DATABASE_URL` (Neon pooled) at runtime; `DATABASE_URL_UNPOOLED` for migrations, `pg_dump`, tests ([integration vars](https://neon.com/docs/guides/vercel-managed-integration)). `prepare_threshold=None`, `connect_timeout=5`, `SET LOCAL statement_timeout='8s'` per transaction (startup `options` is not confirmed for the pooler, so avoid it). Register a psycopg loader so `jsonb` returns text, leaving every `json.loads(row['payload'])` call unchanged.

**Test strategy — decision: tests run on real Postgres; no SQLite adapter.** A dual-dialect adapter would need branches for `json_each`, rank, collation, locks, and bool/bigint types, and a green SQLite suite would not prove the production SQL. Local dev and tests use `scripts/dev_postgres.sh` (`docker run postgres:17` on a fixed local port; Docker 29 is installed) or any Neon dev branch via `TRACE_TEST_DATABASE_URL`. `tests/pg_support.py` creates a throwaway schema per test (`search_path` set on the direct connection), applies `migrations/`, drops it afterward. Existing tests change mechanically from `Store(tmp_path)` to `Store(database)`. Pure-function tests need no database. The legacy-file reader in `public_catalog_transfer.export` keeps using `sqlite3` (read-only), the one place SQLite remains.

### 1.3 Stateful pieces
| Piece | Decision |
|---|---|
| `CACHE` (provider results), `LOCK/LAST` (1.1 s provider throttle) | Keep as per-instance best-effort. Durable cache already exists (`catalog_queries`, `provider_score_cache`). Accept that several instances may exceed a provider's polite rate; mitigated by login-gated search + `catalog` budget (risk R6) |
| `RequestBudget` (API_BUDGET, 120–240/min) | Keep in-process as a cheap burst limiter (per-instance, documented as such). Primary global limit = Vercel WAF rate-limit rule on `/api/*` and `/api/auth/*` (blocks before the function and before Neon wakes; check Pro rule allowances in the dashboard, not verified here) |
| `AUTH_BUDGET` (login/register/recover, 15/min/IP) | New `DurableBudget` in `durable_budget.py` over table `auth_budget(key, window_start, hits)`; one atomic upsert per attempt; key is `sha256(ip)` (no raw IP stored) |
| `score_service` threads | `ThreadPoolExecutor` fire-and-forget refresh dies when a function freezes. Cache miss → bounded synchronous load (10 s timeout); stale → serve stale and `INSERT INTO score_refresh_queue … ON CONFLICT DO NOTHING`; drained by the daily cron (25/run) |
| Cron | One route `GET /api/cron/maintenance`, daily `17 4 * * *`: purge expired sessions/invites/old budget rows, drain `score_refresh_queue`, photo GC (Step 8). Auth with `Authorization: Bearer $CRON_SECRET` (constant-time compare); Step 0 confirms the mechanism, since the cron pages I read do not state it ([cron](https://vercel.com/docs/cron-jobs/usage-and-pricing)). **Do not schedule more often than daily** (R2) |
| `TRACE_DATA_DIR` / `database_path()` | Removed in production. `production.run()` Waitress path and its env checks become dead code (Step 12) |
| `mdblist-api-key` file (`load_local_rating_key`) | No change: environment variable already wins; set `MDBLIST_API_KEY`, `OMDB_API_KEY`, `GOOGLE_BOOKS_API_KEY` as Sensitive Vercel env vars. File loading is only a local-dev path |
| `scripts/manage_beta.py` (invite file) | Same `O_EXCL`, mode 0600 file semantics. Runs on the owner's laptop against `TRACE_DATABASE_URL` set from the Neon console (direct URL); no secrets pulled to disk by the tool |
| `/api/health` | Default becomes DB-free (`{"status":"ok"}`); `?deep=1` checks the DB. Uptime monitors and `prepare-release.mjs checkServer` must not keep Neon awake (R2) |
| IMDb bulk import (`ratings.import_imdb`) | Not run on Neon: ~1.4 M rows would consume most of the 1 GB free cap, and the launch plan already flags the dataset's non-commercial terms. Keep the method behind an explicit `--import-imdb` flag and use `COPY` if ever enabled |

### 1.4 Photos (design)
Today `app.js photoData()` resizes any picked file (UI allows 25 MB) to ≤1280 px JPEG at quality .82 and stores a base64 data URL inside the library entry (`library_sync.mjs` batches ≤9.5 MB; entries ≤1.5 MB). On Vercel two things break: the sync batch cap (9.5 MB > 4.5 MB platform limit) and Neon's 1 GB free storage.

**Decision: resize-then-proxy upload, direct-to-Blob deferred.** The raw file (which may exceed 4.5 MB) never leaves the device; only the locally resized JPEG (typically 0.2–0.6 MB, hard cap 3 MB) is POSTed to `/api/photos`, comfortably under the 4.5 MB function limit. This keeps authorization, quotas, and magic-byte checks in our code and needs no Node function, bundler, or client SDK. True browser→Blob direct upload is documented only through the `@vercel/blob` JS SDK (`handleUpload` + `@vercel/blob/client`) and every example assumes a bundler ([client uploads](https://vercel.com/docs/vercel-blob/client-upload)); it is the fallback only if the owner wants full-resolution originals stored (open question 2).
- **Storage:** private Blob store ([private storage](https://vercel.com/docs/vercel-blob/private-storage), minimum Python SDK `vercel-blob` ≥0.5.0). Object path `photos/<user_id>/<sha256>.jpg`. Table `photos(user_id, id, pathname, content_type, bytes, created_at, deleted_at, PRIMARY KEY(user_id,id))`; `id` = SHA-256 of the bytes, which makes retries idempotent and dedupes. Per-user photo budget `TRACE_PHOTO_BYTES_TOTAL` (default 100 MiB, existing `TRACE_LIBRARY_BYTES` stays for JSON).
- **Writes:** `POST /api/photos`, body is raw `image/jpeg|png|webp`, header `X-Trace-Photo-Id: <sha256>`, same Origin + `X-Trace-CSRF` + session checks as `library_post`. Server verifies magic bytes, size ≤ `TRACE_PHOTO_BYTES` (3,000,000), recomputed hash == header, quota; then Blob `put` (private) and row insert. `photo_store.py` has two implementations behind one interface (`put/get/delete`): `BlobPhotoStore` (production) and `LocalPhotoStore` (files under a temp dir, used by tests and local dev) — tests need no Blob token.
- **Reads:** `GET /api/photos/<id>` authenticates the session, looks up `(user_id, id)` (another user's id is a 404 and never reveals existence), fetches the private blob with the store token, returns `Cache-Control: private, max-age=86400` bytes. Private blobs have no public URL; Vercel recommends exactly this authenticated-route pattern and warns against CDN-caching private responses. Native WebViews cannot send the native cookie jar with `<img>` requests and `native_transport.mjs` is text-only, so `GET /api/photos/<id>?encoding=base64` returns JSON `{contentType,data}` (≤3 MB → ≤4 MB base64, under the response cap) and a native-only hydrator in `mobile.js` swaps `img[src^="/api/photos/"]` for a data URL (small LRU). This avoids touching every `imageUrl()` template.
- **Reference format:** `image: "trace-photo:<64 hex>"`. `library_store._url(image=True)` accepts it (pattern `^trace-photo:[0-9a-f]{64}$`) and still accepts `data:` URLs ≤ entry cap for queued offline entries. **`social.image_url` must reject `trace-photo:`** so a private memory photo can never be published into a public review snapshot.
- **Offline queue semantics preserved:** pending entries keep their `data:` image while offline. `library_sync.mjs` gains an injected `uploadPhoto(dataUrl)` hook; during `push`, before `batch()`, each pending entry's `data:image/*` fields (image and `attachments[].image`) are uploaded, then replaced by the ref via `mutate` **without** bumping `pending.version`/`baseRevision` (so existing `versions[id]` acknowledgment logic is untouched). Network failure keeps the existing `offline` status and retry; 4xx surfaces as `error`. Client also caches uploaded/viewed bytes in IndexedDB (`trace-photo-cache`, keyed by id) so photos still show offline. `MAX_BYTES` 9,500,000 → 4,000,000 (needed regardless of photos).
- **Deletion:** nothing deletes a photo inline. Cron GC deletes `photos` rows older than 7 days whose id is not referenced by any of that user's non-null library entries (`entry LIKE '%trace-photo:<id>%'`; fine at beta scale, replace by a reference table if entries grow), calling `store.delete` first and the row second. The 7-day grace covers queued offline edits. `delete_account` sets `deleted_at` on all the user's photos, and the cron removes those blobs immediately on its next run (and `delete_account` also attempts inline best-effort deletion).
- **Other readers of images:** the in-app JSON backup/export and the Tracewall canvas loader (`app.js` ~700–790) must resolve refs through the same resolver, otherwise exported backups and shared cards would lose memory photos (R7).

### 1.5 Native apps
`native_transport.mjs` pins one exact HTTPS origin; `scripts/prepare-release.mjs` health-checks `<origin>/api/health` for `status: ok` and bakes the origin into `native-config.json`. Use the stable **production alias** (`https://<project>.vercel.app`, or a custom domain if decided now), never a per-deployment URL. Moving the origin later pauses network access for installed apps by design, so decide the domain before any store build (open question 1). Production deployments must have Deployment Protection off (previews are protected by default and cannot serve phones). No code change expected beyond one added test case.

### 1.6 Backups
Neon Free gives only a **6-hour** restore window (capped at 1 GB of changes) and, at the storage cap, blocks writes but keeps reads and `pg_dump` ([plans](https://neon.com/docs/introduction/plans)). That is not a backup. Add a scheduled GitHub Actions job: `pg_dump -Fc "$DATABASE_URL_UNPOOLED"` → `age -r $AGE_RECIPIENT` → `rclone` to Google Drive, daily, keep 14 daily + 8 weekly; plus a per-table row-count manifest inside the encrypted bundle. The `age` private identity stays on a recovery device, never in GitHub (same model as today's `docs/backup-restore.md`). Weekly job also copies the Blob objects listed in `photos` into a second encrypted archive. Monthly restore drill: restore into a new Neon branch/project and run `scripts/verify_restore.py` against the manifest.

### 1.7 Dependencies (new)
| Package | Why | Notes |
|---|---|---|
| `psycopg[binary]==3.3.9` | Postgres driver; binary wheel avoids building libpq on Vercel | PyPI latest at planning time; requires Python ≥3.10 |
| `vercel-blob==0.6.2` | private-blob put/get/delete | Official docs call it `vercel-blob` ≥0.5.0 but show `from vercel import blob`, while PyPI `vercel` 0.13.3 is a different SDK; Step 0 confirms the import path and that delete works in private mode before pinning. Plain HTTPS is viable for GET only (documented `Authorization: Bearer`), not for put/delete (no documented REST) |
| `requirements.txt` (root) | Vercel installs it; `requirements-runtime.txt` (waitress) becomes obsolete | Python pin via `pyproject.toml` `requires-python = ">=3.11"` / Vercel Python 3.12 |
| none for Node | frontend stays buildless; no `@vercel/blob` unless Option B | `scripts/build_public.py` only copies files |
CI-only tools (`postgresql-client-17`, `age`, `rclone`) are installed in the workflow, not in the repo.

### 1.8 Cost check (official docs read 2026-11-24)
| Service | Limit that matters | Source |
|---|---|---|
| Neon Free | 1 GB storage/project (older docs said 0.5 GiB); 100 CU-hours/project/month, autoscale ≤2 CU; **suspends after 5 min idle, not configurable**; 5 GB egress; 6 h restore; 10 branches; at storage cap writes blocked, reads/`pg_dump` work; none of the limits delete data | [plans](https://neon.com/docs/introduction/plans) |
| Neon pooling | pooled host `-pooler`; transaction mode forbids session advisory locks, `SET`, `LISTEN`; protocol-level prepared statements supported on PgBouncer ≥1.22 | [pooling](https://neon.com/docs/connect/connection-pooling) |
| Vercel Functions (Pro) | 4.5 MB request and response body; duration default 300 s, max 800 s (1,800 s beta); 2 GB/1 vCPU default memory; Python bundle 500 MB | [limits](https://vercel.com/docs/functions/limitations), [Python](https://vercel.com/docs/functions/runtimes/python) |
| Vercel Cron (Pro) | 100 jobs/project, minimum interval 1 min; each run is billed as a function invocation; documented as a legacy feature moving to “Schedules” | [cron](https://vercel.com/docs/cron-jobs/usage-and-pricing) |
| Vercel Blob | Pro: storage/ops/transfer are usage-based against the plan's monthly credit, then on-demand (Hobby is 1 GB, 10k simple ops, 2k advanced ops, then access is blocked for 30 days). Example overage rates on the pricing page: storage $0.023/GB-month, simple ops $0.40/M, advanced ops $5/M, data transfer $0.05/GB. Private delivery through a function adds Fast Data Transfer + Fast Origin Transfer. Uploads via client upload carry no transfer charge | [pricing](https://vercel.com/docs/vercel-blob/usage-and-pricing), [private](https://vercel.com/docs/vercel-blob/private-storage) |
Not verified here: the Pro plan's included-credit amount and function/Fast Data Transfer allowances; read the Usage page and enable **Spend Management with pause** before launch.

**Where charges (or outages) could start:**
1. Neon compute: 100 CU-h is about 55% uptime at an assumed 0.25 CU minimum (min size not confirmed). Anything that pings the DB more often than every 5 min keeps it awake; at the cap compute is suspended for the rest of the month (an outage, not a bill) — R2.
2. Neon storage 1 GB: public catalog + library JSON; measure the catalog before loading it.
3. Blob on a Pro team spends the shared monthly credit; photo reads through functions add transfer costs; Hobby-style hard blocks do not apply on Pro but overage bills do.
4. Function invocations/duration from scraping or abuse (WAF rule + spend cap), and the provider search path (`fetch` can take 18 s per upstream call).
5. Preview deployments with Neon preview branches add concurrently active computes against the Free cap.

---

## 2. Ordered steps
Effort: S ≤ 2 h, M ≈ 3–5 h, L ≈ 6–9 h. Commands run with `python3.11` and `TRACE_TEST_DATABASE_URL=postgresql://postgres:trace@127.0.0.1:54329/postgres` unless noted. One commit per step; never push or deploy without the owner.

### Step 0 — Provision and spike (owner + 1 engineer, throwaway branch, no merge) — M
- **Owner provisions:** Vercel project (Pro), Neon via Marketplace (Free; choose the region matching the function region), private Blob store, `CRON_SECRET`, GitHub secrets later. Opt in to Neon preview branches.
- **Throwaway:** a 10-line `api/index.py` returning only the **names** of `environ` keys, `PATH_INFO`, the Host header, and the response headers set by `vercel.json`; one rewrite; one static file per type (`.mjs`, `.webmanifest`, `sw.js`); a cron hit; a private Blob put/get/delete via the candidate SDK.
- **Confirm and write back into this plan:** `PATH_INFO` preserved under rewrite; `wsgi.input` readable for POST; `SCRIPT_NAME` empty; `x-vercel-forwarded-for` vs spoofed `x-real-ip`; static content types and HSTS present; cron `Authorization` format; SDK import path/pin; Python 3.12 selected; cold-start time with psycopg.
- **Test:** `curl -si https://<preview>/api/probe` (with the protection-bypass header for previews). **Parallel:** nothing else should start before the answers on `PATH_INFO` and `wsgi.input`; Step 1 can start immediately.

### Step 1 — Database foundation and schema — L
- **Create:** `database.py` (`Database(dsn, search_path=None)`, `connect(isolation=None)` context manager, `lock(db,name)`, `Row`, `Error`, `IntegrityError`, `?` translation, jsonb text loader, `prepare_threshold=None`), `migrations/001_catalog.sql` … `006_photos.sql` (catalog + `title_norm/creator_norm` + indexes; users/sessions/follows/reviews/likes/comments/invites/recovery codes with the type changes in 1.2; library; ratings/provider caches/backoff; `auth_budget`, `score_refresh_queue`; `photos`), `schema_migrations`, `scripts/migrate_db.py` (direct URL, advisory-locked, idempotent), `scripts/dev_postgres.sh`, `requirements.txt`, `pyproject.toml` (requires-python), `tests/pg_support.py`, `tests/test_database.py`.
- **Test:** `python3.11 -m unittest tests.test_database -v` — covers `?`→`%s` with literal `%`, Row int/name access, commit/rollback, `IntegrityError` mapping, isolation param, double-migration idempotence, citext uniqueness (`Ada` vs `ada`), BIGINT at 2^53−1, DOUBLE epoch round-trip.
- **Parallel:** none (everything below needs it). Steps 10 and 11 may start now (they touch no shared files).

### Step 2 — Catalog store — L  *(parallel with 3, 4, 5, 9, 10, 11)*
- **Touch:** `catalog_store.py` (DB swap; `browse` rank CASE; `genres` via `jsonb_array_elements_text`; `lower(title)`; advisory lock in `save`; `VISIBLE_CATALOG` as `payload->>'catalogExcludedReason'`), `movie_catalog.py` (`reconcile_movie_catalog`), `catalog_seed.py` (audit query), `scripts/public_catalog_transfer.py` (import writes via `Database`; export keeps read-only sqlite3), tests `test_catalog_store.py`, `test_movie_catalog.py`, `test_catalog_seed.py`, `test_public_catalog_transfer.py`.
- **New test:** rank parity — for a fixture of titles/creators incl. accents and punctuation-only queries, SQL ordering == ordering by `catalog_search_rank`.
- **Test:** `python3.11 -m unittest tests.test_catalog_store tests.test_movie_catalog tests.test_catalog_seed tests.test_public_catalog_transfer`

### Step 3 — Social/accounts — L  *(parallel)*
- **Touch:** `social.py` (all `BEGIN IMMEDIATE` replaced per 1.2; `register` atomic invite claim; `ILIKE`; `payload->>` community rating via `item->>'type'`), `scripts/manage_beta.py` (store construction only), tests `test_social.py`, `test_beta_accounts.py`.
- **New tests:** two threads claim one invite → exactly one succeeds; duplicate username differing in case → 409; `recover` with an uppercase username (previous NOCASE behavior); `delete_account` removes library rows and photo rows.
- **Test:** `python3.11 -m unittest tests.test_social tests.test_beta_accounts`

### Step 4 — Library store — M  *(parallel)*
- **Touch:** `library_store.py` (`pull` at repeatable read; `sync` per-user `FOR UPDATE`; `octet_length`), test `test_library_store.py`.
- **New tests:** concurrent syncs for one user never reuse a revision and produce a gap-free cursor; revision 2^53−1 accepted/rejected at the existing limit; user A cannot pull user B's rows.
- **Test:** `python3.11 -m unittest tests.test_library_store`

### Step 5 — Ratings, score service, durable budget — M  *(parallel)*
- **Touch:** `ratings.py` (`ON CONFLICT` upserts, double-precision types, `import_imdb` behind explicit flag), `score_service.py` (no background threads; synchronous bounded load; enqueue stale), new `durable_budget.py` (`DurableBudget(database).allow(key, limit, seconds=60)` with the same signature as `RequestBudget.allow`), new `score_queue.py` (`enqueue`, `drain(limit, loader)`), tests `test_ratings.py`, `test_score_service.py`, `test_media_scores.py`, new `tests/test_durable_budget.py`.
- **Test:** `python3.11 -m unittest tests.test_ratings tests.test_score_service tests.test_media_scores tests.test_durable_budget` (budget: 16th attempt in one window denied; next window allowed; keys are hashed).

### Step 6 — Vercel hosting adapter — L  *(after 1; its server-level tests go fully green only after 2–5)*
- **Touch:** `server.py` (`sqlite3` import and 8 `except sqlite3.Error` → `database.Error`; `allowed_origins()` in `valid_host`/`request_origin`; shallow `/api/health` + `?deep=1`; swap `AUTH_BUDGET` to `DurableBudget` — hunk at the budgets block and the three `.allow(client_ip(...),15)` calls), `production.py` (`MAX_BODY_BYTES=4_000_000`; `run()` no longer demands `TRACE_DATA_DIR`/Waitress), `release_policy.py` (`client_ip` vercel mode; accept `TRACE_TRUST_PROXY=vercel`), `app.js`/`library_sync.mjs` untouched. **Create:** `api/index.py` (insert repo root into `sys.path`; `from production import application as app`), `vercel.json`, `.vercelignore`, `scripts/build_public.py`, `tests/test_vercel_config.py`.
- **Tests:** `tests/test_vercel_config.py` parses `vercel.json` and asserts: headers from 1.1, rewrite `/api/:path*`, cron schedule is daily, and every path in `sw.js SHELL_FILES` ∈ staged public set; `test_release_policy.py`/`test_production.py` gain cases for vercel client-IP mode (spoofed `x-real-ip` ignored when `x-vercel-forwarded-for` present), preview-origin acceptance, production rejection of other `*.vercel.app` hosts, 413 above 4 MB.
- **Test:** `python3.11 -m unittest tests.test_vercel_config tests.test_release_policy tests.test_production`; after 2–5 land: `python3.11 -m unittest discover -s tests -p 'test_*.py'` and `node --test tests/core.test.mjs tests/views.test.mjs`.

### Step 7 — Cron maintenance — S–M  *(after 5 and 6)*
- **Touch:** `server.py` (route `GET /api/cron/maintenance`, secret check, never listed in `PUBLIC_FILES`), `maintenance.py` (new: purge sessions/invites/budgets, drain queue; photo GC is added by Step 8), `vercel.json` (`crons`), `tests/test_maintenance.py`.
- **Test:** `python3.11 -m unittest tests.test_maintenance` — missing/wrong bearer → 401; correct bearer purges expired sessions only; concurrent runs are safe (`FOR UPDATE SKIP LOCKED` on the queue).

### Step 8 — Photo backend — L  *(after 3, 4, 6)*
- **Create:** `photo_store.py` (`BlobPhotoStore`, `LocalPhotoStore`, shared interface), tests `tests/test_photo_store.py`. **Touch:** `server.py` (`POST /api/photos`, `GET /api/photos/<id>[?encoding=base64]`), `library_store.py` (`_url` accepts refs), `social.py` (`image_url` rejects refs; `delete_account` marks photos), `maintenance.py` (photo GC), `requirements.txt` (Blob SDK pin), `tests/test_library_http.py`.
- **Tests:** raw 5 MB body → 413; wrong magic bytes → 400; hash mismatch → 400; idempotent re-upload returns 200 with one row; user B reads user A's id → 404; unauthenticated → 401; missing CSRF/Origin → 403; over-quota → 400 with the existing “storage is full” wording; `social.publish` with a `trace-photo:` image stores an empty image; GC keeps referenced photos and photos younger than 7 days, removes the rest; account deletion marks and removes.
- **Test:** `python3.11 -m unittest tests.test_photo_store tests.test_library_http tests.test_social`

### Step 9 — Photo client — L  *(parallel with 8 against the contract above; merge after 8)*
- **Touch:** `library_sync.mjs` (`uploadPhoto` hook, pre-batch externalization, `MAX_BYTES` 4,000,000, error text), `app.js` (supply `uploadPhoto` using SHA-256 + `apiFetch`; ref-aware `imageUrl`; export/backup and Tracewall loader resolve refs; IndexedDB photo cache), `mobile.js` (native hydrator), `sw.js` (bump `CACHE_NAME` v16→v17), tests `library_sync.test.mjs`, `mobile.test.mjs`, `core.test.mjs` if export helpers move.
- **Tests:** offline entry with a `data:` photo stays pending offline, uploads and rewrites to a ref on reconnect, and a second sync does not re-upload; an upload failure leaves the entry pending with unchanged `baseRevision`; a batch never exceeds 4,000,000 bytes; hydrator replaces only `/api/photos/` images; exporting an entry with a ref inlines the photo.
- **Test:** `node --test tests/library_sync.test.mjs tests/mobile.test.mjs && node --input-type=module --check < app.js`

### Step 10 — Native origin — S  *(parallel)*
- **Touch:** `tests/release_build.test.mjs` (a `https://trace-example.vercel.app` origin passes `validateNativeConfig`; health `ok` required), `docs/native-apps.md`. No change to `native_transport.mjs` or `prepare-release.mjs`.
- **Test:** `node --test tests/native_*.test.mjs tests/release_build.test.mjs`

### Step 11 — Backups and restore tooling — M  *(parallel after 1)*
- **Create:** `.github/workflows/backup.yml` (changing CI needs the owner's explicit go-ahead per AGENTS.md; it is new, so request it when Step 11 starts), `scripts/verify_restore.py` (row counts per table vs manifest; prints counts only, never row data), `tests/test_verify_restore.py`. **Rewrite:** `docs/backup-restore.md` for Neon + age + Drive + monthly drill.
- **Workflow shape:** schedule daily 03:17 UTC and manual dispatch; `set +x`; secrets `DATABASE_URL_UNPOOLED`, `AGE_RECIPIENT`, rclone Drive config; `pg_dump --no-owner -Fc` → `age` → `rclone copy` → prune; counts manifest included; job fails loudly (GitHub emails the owner). Pin `postgresql-client` to Neon's Postgres major.
- **Test:** `python3.11 -m unittest tests.test_verify_restore`; plus a manual dispatch followed by a restore into a scratch Neon branch (also the Step 13 drill).

### Step 12 — Documentation and obsolete VPS files — S  *(after 6; deletion is the owner's separate decision)*
- **Edit:** `AGENTS.md` (stack line: Postgres; commands: add `TRACE_TEST_DATABASE_URL`, `dev_postgres.sh`, `migrate_db.py`; Lessons), `README.md`, `docs/deployment.md` (replace VPS runbook with Vercel/Neon/Blob setup and env table), `docs/friends-beta.md`, `docs/public-launch-plan.md` (mark storage/queue items done or redirected), `docs/handoff.md`.
- **Obsolete after cutover (keep until the owner approves deletion):** `deploy/Caddyfile`, `deploy/trace.service`, `deploy/trace-backup.service`, `deploy/trace-backup.timer`, `deploy/trace.env.example`, `scripts/bootstrap_server.py`, `scripts/package_server.py`, `scripts/backup_database.py`, `requirements-runtime.txt`, the Waitress `run()` in `production.py`, and their tests (`test_bootstrap_server.py`, `test_package_server.py`, `test_backup_database.py`). `catalog_tls` tests stay (TLS context for provider calls).
- **Test:** `git diff --check` and the full suites listed under Step 6.

### Step 13 — Staging verification on a Vercel preview — M + owner time
Preview deployment, Neon preview branch (migrated with `scripts/migrate_db.py`), Blob store, preview env vars; protection-bypass header for automation. New `scripts/staging_check.py` (stdlib only, counts and status codes in output, no content) drives the API; Playwright (webapp-testing skill) drives the browser.
1. **Two synthetic accounts, A and B,** each created from a separate owner-issued invite (`manage_beta.py`).
2. **Invite-only signup:** register without an invite → 403; reuse a claimed invite → 403; expired invite → 403; wrong host header → 403 “Unknown host”.
3. **Privacy isolation:** A writes entries and a photo; B's `GET /api/library` returns none of A's rows; B's `GET /api/photos/<A id>` → 404; B cannot publish or read A's photo through reviews; A's session cookie never works for B's CSRF token.
4. **Photo over 4.5 MB raw:** in the browser pick a ~6–8 MB JPEG as a memory photo; confirm the upload request is under 1 MB, the entry holds a `trace-photo:` ref, and the image renders after reload and on a second device profile. Separately POST a 5 MB raw body to `/api/photos` → platform/app 413.
5. **Offline edit sync:** Playwright offline mode: edit a title and add a photo memory offline, go online, confirm sync status `synced`, revisions advance, and the same data appears in a fresh browser context; repeat with a deliberate conflict from the other context and confirm the conflict UI.
6. **Rate limiting:** 16 rapid wrong logins → 429 with `Retry-After` on the 16th (Postgres budget), plus the WAF rule if enabled.
7. **Cron:** call the maintenance route with and without the bearer secret; confirm a stale-score row is drained; check Neon went idle again afterward.
8. **Restore drill:** run the backup workflow, download and `age -d` the dump, restore into a new Neon branch, run `scripts/verify_restore.py`; record counts and elapsed time in `docs/backup-restore.md`.
9. **Headers:** `curl -I` on `/`, `/app.js`, `/sw.js`, `/manifest.webmanifest`, `/api/health`; check the Step 1.1 table; install the service worker and confirm `isPublicFile` accepts every shell file.
10. **Native smoke:** `TRACE_NATIVE_API_ORIGIN=<production alias> node scripts/prepare-release.mjs` passes its health check.

### Parallelism map
- After Step 1: {2, 3, 4, 5} are disjoint files and can run at once; 9, 10, 11 are independent of them. 6 can start after 1 but its full-suite run waits for 2–5. 7 after 5 and 6. 8 after 3, 4, 6. 9 merges after 8. 12 after 6. 13 last.
- Shared files to serialize: `server.py` (6 → 7 → 8), `library_store.py` (4 → 8), `social.py` (3 → 8), `maintenance.py` (7 → 8), `vercel.json` (6 → 7), `requirements.txt` (1 → 8).

---

## 3. Risks
- **R1 `PATH_INFO` / `wsgi.input` / `SCRIPT_NAME` differ on Vercel** and the adapter's “host at origin root” check rejects requests. Mitigation: Step 0 before building; fallback is a tiny shim in `api/index.py` restoring the original path from `x-vercel-*`/query.
- **R2 Neon compute exhaustion** (100 CU-h; 5 min idle suspend). Cron daily only, DB-free health, WAF rate limit in front, alert on usage in the Neon console.
- **R3 Cold starts + pooler round trips** make every request slower (TLS+auth per connection). Same-region Neon/function, no pool, no DDL on start; measure p95 in Step 13 and revisit (psycopg pool is not safe across frozen invocations).
- **R4 SQL semantic drift** (case rules, `LIKE`, ordering, rank). Parity tests in Steps 2–3; real-Postgres tests only.
- **R5 Type traps** (`REAL`, `INTEGER`, bool) silently truncating or erroring; covered by Step 1 type tests.
- **R6 Provider throttle is per instance**, so concurrent instances may exceed polite request rates to Wikidata/TVmaze/Kitsu. Acceptable at beta size; add a Postgres throttle row if a provider complains.
- **R7 Photo refs break secondary readers** (JSON export, Tracewall, offline viewing, native). Covered in Step 9; manual check in Step 13.
- **R8 Private photo leakage via publishing** a snapshot. `social.image_url` rejects refs (tested).
- **R9 Backups miss Blob contents or depend on a GitHub-held database credential.** Weekly photo archive; credential is least-privilege (create a read-only Neon role for dumps); `age` private key stays offline.
- **R10 Neon Free storage cap (1 GB)** blocks writes (app returns 503 on writes). Measure catalog before loading, keep IMDb off, monitor.
- **R11 Preview protection and aliases:** previews are SSO-protected by default; production must be public (native apps). Misconfiguration looks like a health-check failure in `prepare-release.mjs`.
- **R12 Vercel “Cron → Schedules” migration and Blob SDK naming** may change underneath us; pin versions and re-read docs at Step 0 and before launch.
- **R13 Docs not fully verified:** Pro plan included credits, WAF rate-limit allowances, cron auth, Neon minimum CU, and inactivity handling were not confirmed from official pages.

## 4. Open questions for the owner
1. **Production origin now:** keep `https://<project>.vercel.app` for the beta, or buy/attach a custom domain before any native build? Installed apps pin one exact origin, so changing it later forces a new app release (recommended: decide now; either works technically).
2. **Photo fidelity:** is the existing ≤1280 px JPEG enough for memory photos (recommended; no new client SDK, uploads are small), or must full-resolution originals be stored (adds a Node token function, a vendored `@vercel/blob/client` bundle, and a dependency)?
3. **Backup credentials:** OK to store the Neon direct (read-only role) URL, an `age` public recipient, and an rclone Google Drive token as GitHub Actions secrets for the daily encrypted backup, with the `age` private key kept only on your recovery device?
