# Trace

A local-first collection of movies, TV, anime, books, **albums**, and personal memories. DVD cases, books, vinyl sleeves, and Polaroids form a responsive shelf. Log an item, keep notes and ratings, browse your diary, or create a Tracewall recap and download it as a PNG.

## Run

Requires Python 3.10+ and a modern browser. No package installation, paid API, or API key is required.

```sh
cd /Users/adamw/Documents/ChatGPT/Trace
python3 server.py 5173
```

Open http://localhost:5173. Keep the server running for live catalog search. The server listens only on your computer. The app stores personal entries and compressed uploaded photos in this browser's local storage. Use **Back up my collection** regularly; import the resulting JSON to restore it. Browser storage is specific to the browser and address, so use the same localhost address and port.

The initial collection is explicitly a **sample**, kept separate from personal data. Your first log/import starts an empty personal collection containing only what you add. Sample entries are never included in personal backups.

## Working features

- Six categories, quick catalog/manual logging, editable details, date, notes, optional 1–5 star ratings, favorites, status, image URLs, photo upload.
- Memories: photo, date, context, and notes, with up to 12 attached books, albums, films, or shows from your shelf. No star ratings. Attachments retain a small snapshot so backups remain meaningful even when the original item is absent. Existing food/place entries become memories in memory on load; photos, notes, dates, original source, and old rating/type (as legacy metadata) are preserved. The original storage is not rewritten until a successful save.
- The collection shares one continuous background with wooden ledges: book jackets/spines, DVD cases, records in a wooden rack, and memory Polaroids. Desktop groups books and DVDs side by side when space allows. The iPhone layout uses bottom tabs, safe-area insets, 16px form inputs, and a memory sheet. Selection has a 170 ms lift and a soft modal reveal; reduced-motion preferences disable movement.
- Shelf/list views, category/status/favorite filters, search, sort, diary grouped by month. Half-star imported ratings are retained.
- Tracewall: all-time, yearly, or monthly recap; editable title; three paper colors; select up to 12 completed entries; local PNG download. Images without CORS permission or unavailable images become labeled title cards during export.
- Letterboxd CSV: diary, ratings, reviews, watched, watchlist, and lists. Extract the ZIP yourself and select multiple CSVs. Ratings and diary dates enrich matching existing entries. `Date` is treated as a viewing date only for diary files; activity dates are not invented watch dates.
- Goodreads CSV: books, authors, original publication years, ratings, plain-text reviews, ISBN covers, read dates, and exclusive shelf status. Unrated (zero) stays unrated. Blank read dates remain blank.
- IMDb ratings CSV: title, type, year, directors, and ratings converted from ten to five stars.
- Other services: download the Trace CSV template and map your export columns. Albums only, with no track import.
- Preview imports before applying. Matching entries fill missing metadata, preserve existing ratings/notes, combine known diary dates, and advance want → in progress → logged. Similar titles with different known years/creators stay separate. A shelf has one item per work; rewatch dates are retained in backup history, while the diary shows one primary date per item.
- Trace JSON backup/restore. CSVs are parsed in the browser and are never sent to the local server. Maximum 10 MB per file, 30,000 rows per batch. Very large collections can exceed browser storage; failed writes do not replace in-memory data or report success.

## Real catalogs and import sources

Search is explicit (no background bulk enrichment). Fixed provider adapters in `server.py` cache queries for an hour and pace requests. Catalog availability and coverage vary; failures offer manual entry.

- Movies: [Wikidata](https://www.mediawiki.org/wiki/Wikibase/API) title search and [Wikipedia PageImages](https://www.mediawiki.org/wiki/Extension:PageImages#API). Only results described as films are included; coverage and thumbnails vary. Source links are retained.
- Albums: [Apple Search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/index.html)'s **album** entity; individual tracks and collections labeled Single or EP are excluded. Original Apple Music/store links are retained for opening the album.
- TV: [TVmaze API](https://www.tvmaze.com/api), with source attribution and links. TVmaze data is CC BY-SA; retain attribution and review licensing before production redistribution.
- Anime: [Kitsu public API](https://kitsu.docs.apiary.io/), with original item links.
- Books: [Open Library Search API](https://openlibrary.org/dev/docs/api/search) and cover service.
- [Letterboxd account exports](https://letterboxd.zendesk.com/hc/en-us/articles/15179196880911-Can-I-get-a-copy-of-my-account-data), [Goodreads export page](https://www.goodreads.com/review/import).

This version uses user-provided exports, not scraping or service-account login. It has no accounts, automatic sync, cloud persistence, or social feed. A hosted multi-user version needs authentication, a database, storage, and production provider agreements/limits. No data has been published or committed.

## Verification

```sh
node --test tests/core.test.mjs
python3 -m unittest discover -s tests -p 'test_*.py'
node --input-type=module --check < app.js
python3 -m py_compile server.py
```

## Lessons

2026-10-31 — macOS Python had no trusted CA roots — fall back to the installed system CA bundle when the default context is empty; keep TLS verification enabled.

2026-10-31 — large CSV merges scanned the whole library per row — index by title/provider identity and reuse preview results at confirmation.

2026-11-02 — the first shelf felt like flat cards — keep recognizable physical bindings, compartment depth, and grounded shadows consistent across each media type.

2026-11-02 — backup access disappeared with the mobile sidebar — keep collection export available from the Import page at every viewport.

2026-11-02 — renamed memories could duplicate during backup import — resolve memory IDs before comparing title buckets.

2026-11-02 — the framed wood cabinet felt disconnected from the app — use one continuous room background, restrained ledges, readable labels, and shared native-style controls; avoid decorative slogans.

2026-11-02 — a backdrop-filtered sidebar trapped fixed mobile navigation at the top — clear ancestor filters before anchoring navigation to the viewport.
