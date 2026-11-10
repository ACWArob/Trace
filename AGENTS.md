# AGENTS.md — Trace (software)

Trace is a local-first shelf and diary for films, TV, books, albums, and memories.
Stack: browser JavaScript/CSS, Python standard library, SQLite for shared features.
Default branch: `main`. New branches use `codex/`.
Adapted from ACWArob/claude-md-templates, using Codex's AGENTS.md convention.

## Commands
```sh
python3 server.py 5173
node --test tests/core.test.mjs tests/views.test.mjs
python3 -m unittest discover -s tests -p 'test_*.py'
node --input-type=module --check < app.js
node --input-type=module --check < data.js
python3 -m py_compile server.py
git diff --check
```
Web remains buildless. Optional native apps: `npm install`, `npm run native:sync`.
Prefer the affected test file over the whole suite.

## Layout
- `app.js`, `index.html`, `styles.css` — shelf, diary, imports, and recap UI.
- `core.mjs`, `views.mjs` — pure import/merge and presentation helpers.
- `data.js`, `assets/` — clearly marked examples and attributed cover art.
- `server.py`, `profile_feeds.py` — fixed public catalog/feed adapters and local HTTP.
- `catalog_store.py`, `social.py`, `social.js` — shared catalog and social features.
- `tests/` — Node built-in tests and Python unittest; no package dependencies.
- `native-entry.mjs`, `native_transport.mjs`, `scripts/`, `native/` — optional Capacitor packaging; see `docs/native-apps.md`.

## Workflow
1. For multiple files, data, infrastructure, or hard-to-undo work, list files and
   verification first and wait for OK. An approved plan stays approved across turns.
2. Keep one concern per change. No unrelated refactors, renames, or formatting.
3. Verify with the narrowest meaningful test before calling work done.
4. Review nontrivial changes with a fresh inexpensive subagent, diff only.
5. Record corrections and non-obvious causes under Lessons: date — issue — rule.

## Credit discipline
- Use `rg` and globs before reading; read sections of files over 300 lines.
- Skip lockfiles, dependencies, build output, datasets, weights, and long logs.
- Read user import data only when necessary for the explicitly requested import;
  report counts and validation results instead of private notes or raw exports.
- Do not reread just-edited files or repeat passing tests without a reason.
- Repository-wide location questions go to an Explore subagent.
- Use terse progress updates. If blocked, ask one specific question.

## Conventions and safety
- Match the existing buildless stack; ask before adding dependencies.
- Preserve personal ratings, notes, dates, IDs, and attachments during enrichment.
- Unknown viewing dates remain blank. Public feeds are limited; complete Letterboxd
  ratings come from ratings.csv, including titles without diary entries.
- Keep Movies and TV separate. Albums are releases, never individual tracks.
- Use escaped text, fixed provider hosts, bounded requests, and provider attribution.
- Shared features never publish an existing local collection automatically.
- Test writes use isolated origins and temporary databases, never the user's library.
- Never print or commit credentials, cookies, private exports, or database contents.
- Never commit or push unless asked; never force-push.
- Ask before deletion, global installs, CI, schema changes, or paid operations.
- User approval for a described schema change authorizes that change; do not re-ask.

## Lessons
- Historical lessons remain in README.md under Lessons.
- 2026-11-03 — large movie collections made horizontal browsing tedious — use
  vertical columns with bounded rendering and retain separate DVD/poster choices.
- 2026-11-03 — metadata selection looked available for unsaved samples — show it only for personal entries; an explicit cover choice may replace the previous cover.
- 2026-11-03 — movie columns displaced the loved cinema view — retain the screen above columns and keep the TV room and mixed All shelf.
- 2026-11-03 — Discover seeded a today date for unexperienced titles — want entries start undated; metadata updates retain available public scores and canceled composers clear their intent.
- 2026-11-03 — malformed review types raised a handler error — validate JSON value types before set membership and return a bounded 400 response.
- 2026-11-03 — standalone logo marks missed the requested identity — explore feature-inspired Trace letterforms; show options before changing the app logo.
- 2026-11-03 — abstract monochrome letters were too subtle — logo concepts should use colorful, recognizable books, vinyl, film, photos, and TV objects as the actual letters.
- 2026-11-03 — logo and app felt like separate styles — use the approved warm cartoon-vintage palette and illustrated frames consistently while retaining real cover art.
- 2026-11-03 — heavy spine type, ghosted covers, and overlay previews made the bookcase awkward — use clean bindings and reserve a full-cover slot in every row before selection.

- 2026-11-04 — Discover showed a raw fetch error while the local server was stopped — explain how to start the server; display the Trace score on the requested /10 scale.

- 2026-11-04 — a lone IMDb value looked like a multi-site Trace score — require two feeds for a movie blend, show all four source slots, and keep RT critics/audience distinct.
- 2026-11-04 — a global cache lock serialized independent rating requests — keep network I/O outside the lock and coalesce only matching title/provider lookups.
- 2026-11-04 — provider setup needed to survive local server restarts — load an ignored owner-only key file at startup, validate its permissions, and retain environment overrides.
- 2026-11-04 — asynchronous UI snapshots could undo rapid edits — serialize saves and rebase changed fields onto the latest durable account state.
- 2026-11-04 — HTTP-only origin checks rejected hosted sync — validate an explicit HTTPS deployment origin and use Secure session cookies there.
- 2026-11-04 — static no-store headers prevented public shell precaching — keep APIs private/no-store and give only allowlisted app assets revalidatable public headers.
- 2026-11-04 — separate tabs could replace a shared offline snapshot — read and update account queues in one IndexedDB transaction and propagate sign-out across tabs.
- 2026-11-04 — native WebViews retain local storage when a build changes servers — pin the API origin and pause network access on changes so queued private data cannot cross servers.
- 2026-11-04 — generated native tests retained Capacitor's template app ID — verify generated package assertions against Trace's actual application ID before shipping projects.
- 2026-11-05 — starter queries overrepresented Western work — default to international discovery and verify Algerian, Arabic, French, and other original-language searches.
- 2026-11-05 — descriptions mentioning films admitted games, soundtracks, and people — validate work types and reversibly exclude incorrect public catalog records.
- 2026-11-05 — failed catalog covers borrowed the last opened entry's fallback — bind artwork failures to their own record and try only sizes of the same artwork ID.
- 2026-11-05 — import order made recaps mostly films — balance automatic Tracewall picks across available media, preserve manual order, and share preview/export geometry.
- 2026-11-08 — the full recap was too tall for social posts — compose fixed-size cards with room at the story edges and export the preview canvas directly.
- 2026-11-09 — a flat list of text buttons made recap picks hard to scan — group cover tiles by category and show the pick order in a separate tray.
- 2026-11-09 — the 3 MB upload limit rejected ordinary phone photos — bound the input and resize locally before saving.
