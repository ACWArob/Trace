"""Persistent, attributed catalog results; private collection data never enters this store."""
from pathlib import Path
from contextlib import contextmanager
import json
import os
import sqlite3
import time
import unicodedata
from book_catalog import normalize_isbn, book_genres
from music_catalog import ALBUM_GENRES, clean_album_genres

KINDS = {'movie', 'tv', 'anime', 'book', 'album'}
VISIBLE_CATALOG = "coalesce(json_extract(catalog.payload,'$.catalogExcludedReason'),'')=''"


def catalog_genres(kind, values, *, facets=False):
    if kind == 'album':
        return clean_album_genres(values, limit=200 if facets else 20)
    if kind == 'book':
        return book_genres(values, limit=200 if facets else 8)
    if kind == 'all':
        return book_genres(clean_album_genres(values, limit=200), limit=200)
    return values


def catalog_search_text(value):
    text = unicodedata.normalize('NFKD', value).casefold()
    words = (char if char.isalnum() else ' ' for char in text
             if not unicodedata.category(char).startswith('M'))
    return ' '.join(''.join(words).split())


def catalog_search_rank(title, creator, query):
    title = catalog_search_text(title)
    creator = catalog_search_text(creator)
    if title == query:
        return 0
    if query in (f'{title} {creator}', f'{creator} {title}'):
        return 1
    if creator == query:
        return 2
    combined = f'{title} {creator}'
    if all(word in combined for word in query.split()):
        return 3 if title.startswith(query) else 4
    return None


def query_cache_ttl(result):
    statuses = result.get('providerStatus') or {}
    unavailable = (isinstance(statuses, dict) and 'unavailable' in statuses.values()) or statuses == 'unavailable'
    return 60 if result.get('warnings') or unavailable else 86400


def database_path() -> Path:
    directory = Path(os.environ.get('TRACE_DATA_DIR', Path(__file__).resolve().parent / '.trace'))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / 'trace.sqlite3'


class CatalogStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS catalog (
                    item_key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    creator TEXT NOT NULL, genres TEXT NOT NULL, payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS catalog_kind_title ON catalog(kind,title COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS catalog_queries (
                    kind TEXT NOT NULL, query TEXT NOT NULL, payload TEXT NOT NULL,
                    updated_at REAL NOT NULL, PRIMARY KEY(kind,query)
                );
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def cached(self, kind, query, stale=False):
        with self.connect() as db:
            row = db.execute('SELECT payload,updated_at FROM catalog_queries WHERE kind=? AND query=?',
                             (kind, query.casefold().strip())).fetchone()
        if row:
            result = json.loads(row['payload'])
            for item in result.get('items', []):
                item['genres'] = catalog_genres(item.get('type'), item.get('genres') or [])
            expired = time.time() - row['updated_at'] >= query_cache_ttl(result)
            if expired and not stale:
                return None
            result['cached'] = True
            result['stale'] = expired
            return result
        return None

    def save(self, kind, query, result, *, metadata_only=False):
        if kind not in KINDS:
            raise ValueError('Unknown catalog')
        now = time.time()
        # Only provider results, never a user's rating, diary date, notes, or attachments.
        fields = ('type', 'title', 'creator', 'year', 'image', 'genres', 'description', 'source', 'sourceUrl', 'externalId', 'imdbId', 'isbn', 'publicScores')
        items = [{key: row.get(key, [] if key in ('genres', 'publicScores') else '') for key in fields}
                 for row in result.get('items', [])[:250 if query is None else 50]]
        source_fields = ('source', 'sourceUrl', 'externalId')
        for item, row in zip(items, result.get('items', [])):
            item['genres'] = catalog_genres(item['type'], item['genres'])
            sources = row.get('catalogSources')
            item['catalogSources'] = [{key: source[key] for key in source_fields}
                                      for source in (sources[:8] if isinstance(sources, list) else [])
                                      if isinstance(source, dict) and all(isinstance(source.get(key), str) for key in source_fields)
                                      and source['source'] and source['externalId']]
        payload = {**result, 'items': items}
        retained_fields = ('image', 'description', 'creator', 'year', 'imdbId', 'isbn', 'genres', 'publicScores', 'catalogSources')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for item in items:
                if item['type'] not in KINDS or not item['externalId'] or not item['title']:
                    continue
                key = f"{item['type']}:{item['source']}:{item['externalId']}"
                stored = db.execute('SELECT payload FROM catalog WHERE item_key=?', (key,)).fetchone()
                previous = json.loads(stored['payload']) if stored else {}
                if previous:
                    previous['genres'] = catalog_genres(item['type'], previous.get('genres'))
                if metadata_only and previous:
                    genres = list(dict.fromkeys(list(previous.get('genres') or []) + list(item['genres'] or [])))
                    score_rows = list(previous.get('publicScores') or []) + list(item['publicScores'] or [])
                    scores = {row['source'].casefold(): row for row in score_rows
                              if isinstance(row, dict) and isinstance(row.get('source'), str)}
                    sources = list(previous.get('catalogSources') or [])
                    sources.extend(source for source in item['catalogSources'] if source not in sources)
                    item.update(previous)
                    item['genres'], item['publicScores'] = genres, list(scores.values())
                    item['catalogSources'] = sources[:8]
                if previous.get('verifiedYear') and previous.get('verifiedYear') == previous.get('year'):
                    item['year'] = item['verifiedYear'] = previous['verifiedYear']
                incoming_image = item['image']
                for field in retained_fields:
                    value = item[field]
                    if (not value or isinstance(value, str) and not value.strip()) and previous.get(field):
                        item[field] = previous[field]
                # These audit fields are server-written, never provider input.
                missing = previous.get('unavailableImages')
                missing = [url for url in missing[:8] if isinstance(url, str)] if isinstance(missing, list) else []
                if missing:
                    item['unavailableImages'] = missing
                # Reject only an already-confirmed missing thumbnail. A fresh,
                # different provider image may replace the previously audited one.
                if (previous.get('verifiedImage') and previous.get('verifiedImage') == previous.get('image')
                        and (not incoming_image or incoming_image == previous['image'] or incoming_image in missing)):
                    item['image'] = item['verifiedImage'] = previous['verifiedImage']
                    for source in previous.get('catalogSources', []):
                        if (isinstance(source, dict) and all(isinstance(source.get(name), str) for name in source_fields)
                                and source not in item['catalogSources'] and len(item['catalogSources']) < 8):
                            item['catalogSources'].append({name: source[name] for name in source_fields})
                db.execute('''INSERT INTO catalog VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(item_key) DO UPDATE SET title=excluded.title,creator=excluded.creator,
                    genres=excluded.genres,payload=excluded.payload,updated_at=excluded.updated_at''',
                           (key, item['type'], item['title'], item['creator'], json.dumps(item['genres']), json.dumps(item), now))
            if query is not None:
                db.execute('''INSERT INTO catalog_queries VALUES(?,?,?,?) ON CONFLICT(kind,query)
                    DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at''',
                           (kind, query.casefold().strip(), json.dumps(payload), now))
        return payload

    def browse(self, kind='all', query='', genre='', page=0):
        if kind not in KINDS | {'all'}:
            raise ValueError('Unknown catalog')
        clauses, args = [VISIBLE_CATALOG], []
        if kind != 'all':
            clauses.append('kind=?'); args.append(kind)
        isbn = normalize_isbn(query) if kind in ('book', 'all') else ''
        search = '' if isbn else catalog_search_text(query)
        order = 'title COLLATE NOCASE,item_key'
        order_args = []
        if isbn:
            clauses.append("json_extract(payload,'$.isbn')=?")
            args.append(isbn)
        elif search:
            clauses.append('catalog_rank(title,creator,?) IS NOT NULL')
            args.append(search)
            order = 'catalog_rank(title,creator,?),'+order
            order_args.append(search)
        elif query:
            # Preserve literal searches for titles containing only punctuation.
            term = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            clauses.append("(title LIKE ? ESCAPE '\\' OR creator LIKE ? ESCAPE '\\')"); args.extend([term, term])
        # Keep alternatives visible while a genre is selected, within this catalog/search.
        facet_where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        facet_args = list(args)
        if genre:
            clauses.append('EXISTS (SELECT 1 FROM json_each(catalog.genres) WHERE lower(value)=lower(?))'); args.append(genre)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        page = max(0, min(10000, int(page)))
        with self.connect() as db:
            if search:
                db.create_function('catalog_rank', 3, catalog_search_rank, deterministic=True)
            count = db.execute('SELECT count(*) FROM catalog' + where, args).fetchone()[0]
            rows = db.execute('SELECT payload FROM catalog' + where + ' ORDER BY ' + order + ' LIMIT 40 OFFSET ?',
                              args + order_args + [page * 40]).fetchall()
            genres = [r[0] for r in db.execute('SELECT DISTINCT value FROM catalog,json_each(catalog.genres)' + facet_where + ' ORDER BY value COLLATE NOCASE LIMIT 200', facet_args)]
            totals = {r[0]: r[1] for r in db.execute('SELECT kind,count(*) FROM catalog WHERE ' + VISIBLE_CATALOG + ' GROUP BY kind')}
        items = [json.loads(row[0]) for row in rows]
        for item in items:
            item['genres'] = catalog_genres(item['type'], item.get('genres') or [])
        return {'items': items, 'total': count, 'page': page,
                'hasMore': (page + 1) * 40 < count, 'genres': catalog_genres(kind, genres, facets=True),
                'suggestedGenres': list(ALBUM_GENRES) if kind == 'album' else [], 'counts': totals}

    def identified(self, kind, source, external_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM catalog WHERE item_key=? AND ' + VISIBLE_CATALOG,
                             (f'{kind}:{source}:{external_id}',)).fetchone()
        if not row:
            return None
        item = json.loads(row[0])
        item['genres'] = catalog_genres(kind, item.get('genres') or [])
        return item

    def matching(self, kind, title, year='', creator=''):
        with self.connect() as db:
            rows = db.execute('SELECT payload FROM catalog WHERE kind=? AND title=? COLLATE NOCASE AND ' + VISIBLE_CATALOG + ' LIMIT 50', (kind, title)).fetchall()
        matches = [json.loads(row[0]) for row in rows]
        if creator and kind in ('book', 'album'):
            matches = [row for row in matches if catalog_search_text(row.get('creator') or '') == catalog_search_text(creator)]
        exact = [row for row in matches if str(row.get('year') or '') == str(year or '')]
        return exact[0] if len(exact) == 1 else None
