"""Public score adapters. IMDb's optional dataset is never bundled or imported by default."""
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote, urlparse
import csv
import gzip
import io
import json
import math
import os
import re
import stat
import time


FILM_SOURCES = ('IMDb', 'Rotten Tomatoes critics', 'Rotten Tomatoes audience', 'Letterboxd')
SCORE_CACHE_SECONDS = 7 * 24 * 60 * 60
MISSING_CACHE_SECONDS = 60 * 60
SOURCE_NAMES = {
    'imdb': FILM_SOURCES[0], 'internet movie database': FILM_SOURCES[0],
    'tomatoes': FILM_SOURCES[1], 'rotten tomatoes': FILM_SOURCES[1],
    'rotten tomatoes critics': FILM_SOURCES[1], 'tomatoes-critics': FILM_SOURCES[1],
    'popcorn': FILM_SOURCES[2], 'tomatoesaudience': FILM_SOURCES[2],
    'tomatoes-audience': FILM_SOURCES[2], 'rotten tomatoes audience': FILM_SOURCES[2],
    'letterboxd': FILM_SOURCES[3],
}


def merge_scores(*groups):
    """Canonical source identities prevent counting two providers as two scores."""
    merged = {}
    for group in groups:
        for row in group:
            if not isinstance(row, dict) or not isinstance(row.get('source'), str):
                continue
            source = SOURCE_NAMES.get(row['source'].strip().lower(), row['source'].strip())
            if source:
                merged[source.lower()] = {**row, 'source': source}
    return list(merged.values())


def rating_count(value):
    try:
        number = float(str(value).replace(',', ''))
        return int(number) if math.isfinite(number) and number > 0 and number.is_integer() else None
    except (ValueError, TypeError):
        return None


def valid_score_identity(kind, external_id):
    if not isinstance(external_id, str):
        return False
    if kind in ('movie', 'tv', 'anime'):
        return bool(re.fullmatch(r'tt\d{5,12}', external_id))
    if kind == 'book':
        return bool(re.fullmatch(r'/works/OL\d+W|isbn:97[89]\d{10}', external_id))
    if kind == 'album':
        return bool(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|apple:[1-9]\d{0,19}', external_id))
    return False


def source_url(source, imdb_id, title, supplied=''):
    if source == 'IMDb':
        return f'https://www.imdb.com/title/{imdb_id}/ratings/'
    # Provider links are display links, never targets of server-side requests.
    parsed = urlparse(supplied if isinstance(supplied, str) else '')
    if source == 'Letterboxd':
        if parsed.scheme == 'https' and parsed.netloc == 'letterboxd.com' and re.fullmatch(r'/film/[a-z0-9-]+/?', parsed.path):
            return 'https://letterboxd.com' + parsed.path
        return 'https://letterboxd.com/search/films/' + quote(title, safe='') + '/'
    if parsed.scheme == 'https' and parsed.netloc in ('rottentomatoes.com', 'www.rottentomatoes.com') and re.fullmatch(r'/(?:m|tv)/[a-zA-Z0-9_/-]+', parsed.path):
        return 'https://www.rottentomatoes.com' + parsed.path
    return 'https://www.rottentomatoes.com/search?' + urlencode({'search': title})


def mdblist_scores(imdb_id, title, kind, fetch):
    """Read native scores, not MDBList's own aggregate or normalized `score` field."""
    key = os.environ.get('MDBLIST_API_KEY')
    if not key or kind not in ('movie', 'tv', 'anime') or not re.fullmatch(r'tt\d{5,12}', imdb_id or ''):
        return []
    media = 'movie' if kind == 'movie' else 'show'
    result = fetch(f'https://api.mdblist.com/imdb/{media}/{imdb_id}/?' + urlencode({'apikey': key}), 'mdblist')
    if not isinstance(result, dict) or not isinstance(result.get('ratings'), list):
        raise ValueError('Invalid ratings response.')
    if result.get('imdb_id') and result['imdb_id'] != imdb_id:
        raise ValueError('Ratings belong to another title.')
    scores = []
    for row in result['ratings'][:32]:
        if not isinstance(row, dict) or not isinstance(row.get('source'), str):
            continue
        source = SOURCE_NAMES.get(row['source'].strip().lower())
        value = row.get('value')
        allowed = FILM_SOURCES if kind == 'movie' else FILM_SOURCES[:3]
        if source not in allowed or isinstance(value, bool) or value is None:
            continue
        try:
            number = float(value)
        except (ValueError, TypeError):
            continue
        maximum = 10 if source == 'IMDb' else 5 if source == 'Letterboxd' else 100
        if not math.isfinite(number) or not 0 <= number <= maximum:
            continue
        scores.append({'source': source, 'value': number, 'max': maximum,
                       'count': rating_count(row.get('votes')),
                       'url': source_url(source, imdb_id, title, row.get('url')),
                       'via': 'MDBList'})
    return merge_scores(scores)


def load_local_rating_key(directory=None):
    """Load the optional private local key at server startup; environment wins."""
    if os.environ.get('MDBLIST_API_KEY'):
        return True
    directory = Path(directory) if directory is not None else Path(os.environ.get('TRACE_DATA_DIR', Path(__file__).resolve().parent / '.trace'))
    path = directory / 'mdblist-api-key'
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 512:
            return False
        if os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            return False
        key = path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return False
    if not key or any(character.isspace() for character in key):
        return False
    os.environ['MDBLIST_API_KEY'] = key
    return True


class RatingsStore:
    """Public scores and provider cache over the shared Postgres database.

    Tables come from migrations/004_ratings.sql; no DDL runs here.
    """

    def __init__(self, database):
        self.database = database

    def lookup(self, imdb_id):
        if not re.fullmatch(r'tt\d{5,12}', imdb_id or ''):
            return []
        with self.database.connect() as db:
            rows = db.execute('SELECT source,value,maximum,votes,url FROM public_scores WHERE external_id=?', (imdb_id,)).fetchall()
        return [{'source': r[0], 'value': r[1], 'max': r[2], 'count': r[3], 'url': r[4]} for r in rows]

    @staticmethod
    def _provider_key(provider, kind, imdb_id):
        if (not isinstance(provider, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', provider)
                or not valid_score_identity(kind, imdb_id)):
            raise ValueError('Invalid provider cache key.')

    def read_provider_cache(self, provider, kind, imdb_id, now=None):
        """Read shared parsed scores, including expired scores for error fallback."""
        self._provider_key(provider, kind, imdb_id)
        now = time.time() if now is None else now
        with self.database.connect() as db:
            row = db.execute('''SELECT sources_json,fetched_at,refresh_after
                FROM provider_score_cache WHERE provider=? AND kind=? AND external_id=?''',
                (provider, kind, imdb_id)).fetchone()
        if row is None:
            return None
        sources = json.loads(row[0])
        return {'sources': sources, 'status': 'ok' if sources else 'missing',
                'fetchedAt': row[1], 'stale': now >= row[2], 'refreshAt': row[2]}

    def save_provider_cache(self, provider, kind, imdb_id, sources, *, now=None, refresh_after=None):
        """Save only successful responses; never call this for a failed request.

        An empty successful response is cached for one hour. Scores last seven
        days before refresh. An explicit refresh_after is an epoch deadline.
        Failure/backoff writes are separate so successful stale data survives.
        """
        self._provider_key(provider, kind, imdb_id)
        if not isinstance(sources, list) or len(sources) > 32 or any(not isinstance(row, dict) for row in sources):
            raise ValueError('Invalid provider scores.')
        payload = json.dumps(sources, allow_nan=False, separators=(',', ':'))
        if len(payload.encode('utf-8')) > 65536:
            raise ValueError('Provider scores are too large.')
        now = time.time() if now is None else now
        refresh_after = now + (SCORE_CACHE_SECONDS if sources else MISSING_CACHE_SECONDS) if refresh_after is None else refresh_after
        if not math.isfinite(now) or not math.isfinite(refresh_after) or now < 0 or refresh_after < now:
            raise ValueError('Invalid provider cache time.')
        with self.database.connect() as db:
            db.execute('''INSERT INTO provider_score_cache VALUES(?,?,?,?,?,?)
                ON CONFLICT(provider,kind,external_id) DO UPDATE SET
                sources_json=excluded.sources_json,fetched_at=excluded.fetched_at,
                refresh_after=excluded.refresh_after''',
                (provider, kind, imdb_id, payload, now, refresh_after))

    def get_provider_backoff(self, provider, now=None):
        """Return an active account-wide provider cooldown across server restarts."""
        self._provider_key(provider, 'movie', 'tt0000000')
        now = time.time() if now is None else now
        with self.database.connect() as db:
            row = db.execute('SELECT status,refresh_after FROM provider_score_backoff WHERE provider=?', (provider,)).fetchone()
        return {'status': row[0], 'refreshAt': row[1]} if row and row[1] > now else None

    def set_provider_backoff(self, provider, status, *, retry_after=60, now=None):
        """Keep the longest pending retry deadline without changing cached scores."""
        self._provider_key(provider, 'movie', 'tt0000000')
        now = time.time() if now is None else now
        if (not isinstance(status, str) or not status or len(status) > 64
                or not math.isfinite(now) or now < 0
                or not math.isfinite(retry_after) or retry_after < 0
                or not math.isfinite(now + retry_after)):
            raise ValueError('Invalid provider backoff.')
        with self.database.connect() as db:
            db.execute('''INSERT INTO provider_score_backoff VALUES(?,?,?)
                ON CONFLICT(provider) DO UPDATE SET
                status=excluded.status,refresh_after=excluded.refresh_after
                WHERE excluded.refresh_after >= provider_score_backoff.refresh_after''',
                (provider, status, now + retry_after))

    def import_imdb(self, context=None, *, enabled=False):
        """Bulk-load IMDb's dataset (~1.4 M rows). Off unless ``enabled=True``.

        The dataset is non-commercial and would use most of a small hosted
        database, so only the explicit ``--import-imdb`` command enables it.
        Rows are upserted in committed batches; use COPY instead if this ever
        runs routinely.
        """
        if enabled is not True:
            raise RuntimeError('The IMDb dataset import is disabled; run python3 ratings.py --import-imdb to enable it.')
        request = Request('https://datasets.imdbws.com/title.ratings.tsv.gz', headers={'User-Agent': 'TraceLocal/0.1 (personal non-commercial catalog)'})
        now, count = time.time(), 0
        upsert = '''INSERT INTO public_scores VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(source,external_id) DO UPDATE SET value=excluded.value,
            maximum=excluded.maximum,votes=excluded.votes,url=excluded.url,updated_at=excluded.updated_at'''
        with urlopen(request, timeout=60, context=context) as response:
            with gzip.GzipFile(fileobj=response) as zipped:
                reader = csv.DictReader(io.TextIOWrapper(zipped, encoding='utf-8'), delimiter='\t')
                batch = []
                with self.database.connect() as db:
                    for row in reader:
                        imdb_id = row.get('tconst', '')
                        if not re.fullmatch(r'tt\d{5,12}', imdb_id):
                            continue
                        value, votes = float(row['averageRating']), int(row['numVotes'])
                        if not 0 <= value <= 10 or votes < 1:
                            continue
                        batch.append(('IMDb', imdb_id, value, 10, votes, f'https://www.imdb.com/title/{imdb_id}/ratings/', now))
                        count += 1
                        if len(batch) >= 10000:
                            db.executemany(upsert, batch); db.commit(); batch.clear()
                    if batch:
                        db.executemany(upsert, batch)
        return count


def omdb_scores(imdb_id, title, fetch):
    key = os.environ.get('OMDB_API_KEY')
    if not key or not re.fullmatch(r'tt\d{5,12}', imdb_id or ''):
        return []
    result = fetch('https://www.omdbapi.com/?' + urlencode({'apikey': key, 'i': imdb_id}), 'omdb')
    if result.get('Response') == 'False':
        return []
    scores = []
    for row in result.get('Ratings', []):
        source, value = row.get('Source'), row.get('Value', '')
        match = re.fullmatch(r'(\d+(?:\.\d+)?)/(10|100)', value)
        if match:
            number, maximum = float(match[1]), float(match[2])
        elif re.fullmatch(r'\d+(?:\.\d+)?%', value):
            number, maximum = float(value[:-1]), 100
        else:
            continue
        if source == 'Internet Movie Database':
            source = 'IMDb'; raw_count = str(result.get('imdbVotes', '')).replace(',', ''); count = int(raw_count) if raw_count.isdigit() and int(raw_count) else None
            url = f'https://www.imdb.com/title/{imdb_id}/ratings/'
        elif source == 'Rotten Tomatoes':
            source = 'Rotten Tomatoes critics'
            count = None; url = source_url(source, imdb_id, title)
        elif source == 'Metacritic':
            count = None; url = 'https://www.metacritic.com/search/' + quote(title) + '/'
        else:
            continue
        scores.append({'source': source, 'value': number, 'max': maximum, 'count': count, 'url': url, 'via': 'OMDb'})
    return scores


if __name__ == '__main__':
    import sys
    if sys.argv[1:] != ['--import-imdb']:
        raise SystemExit('Usage: python3 ratings.py --import-imdb (personal/non-commercial data only; needs TRACE_DATABASE_URL or DATABASE_URL)')
    from database import Database
    # Use the same verified system TLS context as catalog requests.
    from server import TLS_CONTEXT
    print('Importing the official IMDb ratings dataset for non-commercial use…', flush=True)
    print(f'Imported {RatingsStore(Database.from_env()).import_imdb(TLS_CONTEXT, enabled=True):,} IMDb rating records.', flush=True)
