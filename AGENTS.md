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
No installation or build step. Prefer the affected test file over the whole suite.

## Layout
- `app.js`, `index.html`, `styles.css` — shelf, diary, imports, and recap UI.
- `core.mjs`, `views.mjs` — pure import/merge and presentation helpers.
- `data.js`, `assets/` — clearly marked examples and attributed cover art.
- `server.py`, `profile_feeds.py` — fixed public catalog/feed adapters and local HTTP.
- `catalog_store.py`, `social.py`, `social.js` — shared catalog and social features.
- `tests/` — Node built-in tests and Python unittest; no package dependencies.

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
