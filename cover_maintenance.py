"""Hourly server-side cover pass (GET /api/cron/covers), for every account.

Finds private library entries with no image or an automatic Wikimedia pick
(the indexed SQL function ``trace_cover_candidate``, migration 007) and asks
each type's dedicated source, in cover_backfill.mjs order: the Letterboxd film
page for linked films/TV/anime, then TVmaze for TV, Kitsu for anime and Open
Library for books. Unlinked films and albums are left to the client backfill.

Only ``image`` changes, through LibraryStore.sync with the revision read, so
every device pulls it and a concurrent user edit wins (counted as a conflict).
Lookups are injected; the server passes its cached, throttled
letterboxd_poster/cover functions. Misses are recorded in ``cover_checks`` and
retried after RETRY_SECONDS or when the entry changes; provider outages are
not recorded and pause that provider for the rest of the run. With nothing to
do the run is one indexed query.
"""
from collections import defaultdict
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from book_catalog import normalize_isbn
from database import IntegrityError
from cover_sources import cover_params, KITSU_IMAGE_HOSTS, OPENLIBRARY_COVER_HOST, TVMAZE_IMAGE_HOSTS
from library_store import LibraryStore
from profile_feeds import letterboxd_film_url, letterboxd_poster_image
from social import SocialError

BUDGET_SECONDS = 18  # one more lookup (<= ~10 s) may still finish inside the 30 s function limit
LIMIT = 60
RETRY_SECONDS = 3 * 24 * 3600
SCREEN_TYPES = ('movie', 'tv', 'anime')
PRIMARY_HOSTS = {'tv': TVMAZE_IMAGE_HOSTS, 'anime': KITSU_IMAGE_HOSTS, 'book': (OPENLIBRARY_COVER_HOST,)}
WIKIMEDIA_HOSTS = ('upload.wikimedia.org', 'commons.wikimedia.org', 'thumb.wikimedia.org')
PROVIDER_ERRORS = (HTTPError, URLError, TimeoutError, OSError, ValueError)

# Round-robin across accounts (each account's oldest revision first), with the
# account order within a round rotated per run so no account always goes last.
# WHERE trace_cover_candidate(e.entry) matches the partial index exactly.
CANDIDATES_SQL = '''
WITH ranked AS (
    SELECT e.user_id, e.id, e.revision,
           row_number() OVER (PARTITION BY e.user_id ORDER BY e.revision) AS turn
    FROM private_library_entries e
    WHERE trace_cover_candidate(e.entry)
      AND NOT EXISTS (SELECT 1 FROM cover_checks c WHERE c.user_id=e.user_id AND c.entry_id=e.id
                      AND c.revision=e.revision AND c.checked_at>?)
), picked AS (
    SELECT user_id, id, revision, turn, md5(user_id || ?) AS spin FROM ranked
    ORDER BY turn, spin, user_id LIMIT ?
)
SELECT p.user_id, p.id, p.revision, e.entry
FROM picked p JOIN private_library_entries e ON e.user_id=p.user_id AND e.id=p.id AND e.revision=p.revision
ORDER BY p.turn, p.spin, p.user_id
'''


class _Outage(Exception):
    """A provider failed; the entry is retried next run, not marked."""


def _host(value):
    try:
        url = urlparse(value)
        return url.hostname if url.scheme == 'https' and not url.username and not url.password and not url.port else ''
    except ValueError:
        return ''


def _primary_image(kind, value):
    return value if isinstance(value, str) and _host(value) in PRIMARY_HOSTS.get(kind, ()) else ''


def _upgradeable(entry):
    """Python twin of the SQL predicate, re-checked on the row actually read."""
    if not isinstance(entry, dict) or entry.get('coverChosen') is True or entry.get('type') not in ('tv', 'anime', 'book', 'movie'):
        return False
    if not isinstance(entry.get('title'), str) or not entry['title'].strip():
        return False
    image = entry.get('image', '')
    return image == '' or (isinstance(image, str) and _host(image) in WIKIMEDIA_HOSTS)


def _letterboxd_link(entry):
    if entry.get('type') not in SCREEN_TYPES:
        return ''
    try:
        return letterboxd_film_url(entry.get('sourceUrl'))
    except ValueError:
        return ''


def _cover_args(entry):
    """The parameters app.js coverLookup sends to /api/cover."""
    kind = entry['type']
    year = str(entry.get('year') or '')[:4]
    year = year if re.fullmatch(r'[12][0-9]{3}', year) else ''
    creator = isbn = ''
    if kind == 'book':
        creator = str(entry.get('creator') or '').strip()[:300]
        isbn = normalize_isbn(entry['isbn']) if isinstance(entry.get('isbn'), str) else ''
    return kind, entry['title'].strip()[:300], year, creator, isbn or ''


def _find(entry, lookups, down, out_of_time):
    """A non-Wikimedia poster URL, '' for a definite miss, or raise _Outage."""
    link = _letterboxd_link(entry)
    if link:
        if 'letterboxd' in down:
            raise _Outage()
        try:
            image = letterboxd_poster_image(lookups.letterboxd(link))
        except PROVIDER_ERRORS:
            down.add('letterboxd')
            raise _Outage()
        if image:
            return image
    kind = entry['type']
    if kind not in PRIMARY_HOSTS:
        return ''
    try:
        args = cover_params(*_cover_args(entry))
    except ValueError:  # e.g. control characters in the title: no lookup can match it
        return ''
    if kind in down or (link and out_of_time()):
        raise _Outage()
    try:
        return _primary_image(kind, lookups.cover(*args))
    except PROVIDER_ERRORS:
        down.add(kind)
        raise _Outage()


def run_cover_pass(database, *, lookups, budget_seconds=BUDGET_SECONDS, limit=LIMIT, clock=time.monotonic,
                   now=None, library=None):
    """Return ``{'checked', 'updated', 'skipped', 'conflicts'}``.

    ``lookups`` has ``letterboxd(film_url)`` and ``cover(kind, title, year,
    creator, isbn)``, each returning an image URL or '' and raising on outage.
    """
    deadline = clock() + budget_seconds
    out_of_time = lambda: clock() >= deadline
    now = time.time() if now is None else now
    result = {'checked': 0, 'updated': 0, 'skipped': 0, 'conflicts': 0}
    with database.connect() as db:
        rows = db.execute(CANDIDATES_SQL, (now - RETRY_SECONDS, str(int(now // 3600)), limit)).fetchall()
    if not rows:
        return result
    library = library or LibraryStore(database)
    updates, misses, down = defaultdict(list), [], set()
    for row in rows:
        if out_of_time():
            break
        entry = json.loads(row['entry'])
        result['checked'] += 1
        if not _upgradeable(entry):
            misses.append((row['user_id'], row['id'], row['revision']))
            continue
        try:
            image = _find(entry, lookups, down, out_of_time)
        except _Outage:
            continue
        if image and _host(image) not in WIKIMEDIA_HOSTS and image != entry.get('image'):
            updates[row['user_id']].append({'id': row['id'], 'entry': {**entry, 'image': image}, 'baseRevision': row['revision']})
        else:
            misses.append((row['user_id'], row['id'], row['revision']))
    for user_id, changes in updates.items():
        try:
            saved = library.sync(user_id, changes)
        except SocialError:  # account deleted meanwhile, or storage full: try again after the retry window
            misses.extend((user_id, change['id'], change['baseRevision']) for change in changes)
            continue
        result['updated'] += len(saved['accepted'])
        result['conflicts'] += len(saved['conflicts'])
    result['skipped'] = result['checked'] - result['updated'] - result['conflicts']
    _mark(database, misses, now)
    return result


def _mark(database, misses, now):
    try:
        with database.connect() as db:
            db.execute('DELETE FROM cover_checks WHERE checked_at<=?', (now - RETRY_SECONDS,))
            if misses:
                db.executemany('INSERT INTO cover_checks(user_id,entry_id,revision,checked_at) '
                               'SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM users WHERE id=?) '
                               'ON CONFLICT(user_id,entry_id) DO UPDATE SET revision=excluded.revision,checked_at=excluded.checked_at',
                               [(user_id, entry_id, revision, now, user_id) for user_id, entry_id, revision in misses])
    except IntegrityError:
        pass  # an account deleted mid-run; markers are best-effort
