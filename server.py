"""Trace's local server: static UI + small, cached public-catalog adapters."""
from collections import OrderedDict
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path
import json
import os
import re
import sys
import time
import threading
import ssl
import hmac
from http.cookies import SimpleCookie
import base64
import database
import maintenance
import photo_store
from durable_budget import DurableBudget
from catalog_store import CatalogStore, query_cache_ttl, catalog_search_text
from book_catalog import search_books, normalize_isbn, openlibrary_scores, google_books_scores, CATALOG_VERSION as BOOK_CATALOG_VERSION
from music_catalog import search_albums, album_details, match_album, ALBUM_GENRES, CATALOG_VERSION as MUSIC_CATALOG_VERSION
from screen_catalog import search_wikidata, tvmaze_item, CATALOG_VERSION as SCREEN_CATALOG_VERSION
from movie_catalog import is_movie_metadata, is_non_work_description, WIKIDATA_SOURCE
from social import SocialStore, SocialError
from library_store import LibraryStore
from score_service import ScoreService
from ratings import RatingsStore, omdb_scores, mdblist_scores, merge_scores, load_local_rating_key
from profile_feeds import read_profile_feed, FeedUnavailable, letterboxd_film_url, fetch_letterboxd_poster
from release_policy import production_mode, signup_mode, client_ip, RequestBudget, library_byte_limit

ROOT = Path(__file__).resolve().parent
CACHE = OrderedDict()
CACHE_LIMIT = 2000
CACHE_LOCK = threading.Lock()
LOCK = threading.Lock()
LAST = {}
def create_tls_context():
    context = ssl.create_default_context()
    # Some macOS Python installations have a partial CA directory but no CA file.
    # Use an existing OS trust bundle; certificate and hostname checks stay enabled.
    explicit_trust = os.environ.get('SSL_CERT_FILE') or os.environ.get('SSL_CERT_DIR')
    if not explicit_trust and (not ssl.get_default_verify_paths().cafile or not context.get_ca_certs()):
        for bundle in ('/etc/ssl/cert.pem', '/etc/ssl/certs/ca-certificates.crt'):
            if Path(bundle).is_file():
                context.load_verify_locations(bundle)
                break
    return context


TLS_CONTEXT = create_tls_context()
PROVIDERS = {'movie': 'Wikidata / Wikipedia', 'tv': 'TVmaze / Wikidata', 'anime': 'Kitsu',
             'book': 'Open Library', 'album': 'Apple / MusicBrainz'}

DATABASE = None
CATALOG_STORE = None
SOCIAL_STORE = None
RATINGS_STORE = None
LIBRARY_STORE = None
STORE_LOCK = threading.RLock()
AUTH_BUDGET = None  # DurableBudget shared by every instance; see auth_budget().
API_BUDGET = RequestBudget()  # Per-instance burst limiter only.
PUBLIC_FILES = frozenset(('/', '/index.html', '/privacy.html', '/support.html', '/styles.css', '/app.js', '/core.mjs', '/data.js', '/views.mjs', '/social.js', '/library_sync.mjs', '/api_client.mjs', '/artwork.mjs', '/letterboxd_zip.mjs', '/tv_starters.mjs', '/collection_starters.mjs', '/cover_backfill.mjs', '/mobile.js', '/sw.js', '/manifest.webmanifest'))
SCORE_SERVICE = None


def public_origin():
    value = os.environ.get('TRACE_PUBLIC_ORIGIN', '').strip().rstrip('/')
    if not value:
        return ''
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('TRACE_PUBLIC_ORIGIN must be an HTTPS origin without a path.')
    port = parsed.port
    return 'https://' + parsed.hostname.lower() + (':' + str(port) if port and port != 443 else '')


def _vercel_origin(host):
    host = (host or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host):
        return ''
    return 'https://' + host


def allowed_origins():
    """HTTPS origins this deployment answers on: TRACE_PUBLIC_ORIGIN, plus a
    Vercel preview's own deployment and branch URLs. Production never accepts
    other *.vercel.app aliases."""
    origins = [public_origin()]
    if os.environ.get('VERCEL_ENV') == 'preview':
        origins += [_vercel_origin(os.environ.get('VERCEL_URL')), _vercel_origin(os.environ.get('VERCEL_BRANCH_URL'))]
    return tuple(dict.fromkeys(origin for origin in origins if origin))


def get_database():
    """The one Database (connection settings only) shared by every store."""
    global DATABASE
    with STORE_LOCK:
        if DATABASE is None:
            DATABASE = database.Database.from_env()
        return DATABASE


def get_catalog():
    global CATALOG_STORE
    with STORE_LOCK:
        if CATALOG_STORE is None:
            CATALOG_STORE = CatalogStore(get_database())
    return CATALOG_STORE


def get_social():
    global SOCIAL_STORE
    with STORE_LOCK:
        if SOCIAL_STORE is None:
            SOCIAL_STORE = SocialStore(get_database())
    return SOCIAL_STORE


def get_ratings():
    global RATINGS_STORE
    with STORE_LOCK:
        if RATINGS_STORE is None:
            RATINGS_STORE = RatingsStore(get_database())
    return RATINGS_STORE


def get_library():
    global LIBRARY_STORE
    with STORE_LOCK:
        if LIBRARY_STORE is None:
            LIBRARY_STORE = LibraryStore(get_database(), byte_limit=library_byte_limit() if production_mode() else None)
    return LIBRARY_STORE


def auth_budget():
    """Login/signup/recovery budget shared by every instance (table auth_budget)."""
    global AUTH_BUDGET
    with STORE_LOCK:
        if AUTH_BUDGET is None:
            AUTH_BUDGET = DurableBudget(get_database())
        return AUTH_BUDGET


def attach_scores(result):
    for item in result.get('items', []):
        sources = list(item.get('publicScores') or [])
        saved = get_catalog().identified(item.get('type'), item.get('source', ''), item.get('externalId', ''))
        if saved:
            sources.extend(saved.get('publicScores') or [])
            item['genres'] = list(dict.fromkeys(list(item.get('genres') or []) + list(saved.get('genres') or [])))
            if saved.get('verifiedYear') and saved['verifiedYear'] == saved.get('year'):
                item['year'] = saved['verifiedYear']
        sources.extend(get_ratings().lookup(item.get('imdbId', '')))
        item['publicScores'] = merge_scores(sources)
    return result


def score_service():
    global SCORE_SERVICE
    store = get_ratings()
    with STORE_LOCK:
        if SCORE_SERVICE is None or SCORE_SERVICE.store is not store:
            SCORE_SERVICE = ScoreService(store)
        service = SCORE_SERVICE
    return service


def remote_scores(kind, title, imdb_id):
    return score_service().lookup(kind, imdb_id, {
        'omdb': {'enabled': bool(os.environ.get('OMDB_API_KEY')), 'load': lambda: omdb_scores(imdb_id, title, fetch)},
        'mdblist': {'enabled': bool(os.environ.get('MDBLIST_API_KEY')), 'load': lambda: mdblist_scores(imdb_id, title, kind, fetch)},
    }, title=title)


def score_loader(provider, kind, external_id, title):
    """Rebuild a queued key's provider loader for the cron, or None to drop it
    (provider not configured, or one whose loader needs the full record)."""
    title = title or ''
    if provider == 'omdb' and os.environ.get('OMDB_API_KEY'):
        return lambda: omdb_scores(external_id, title, fetch)
    if provider == 'mdblist' and os.environ.get('MDBLIST_API_KEY'):
        return lambda: mdblist_scores(external_id, title, kind, fetch)
    if provider == 'openlibrary' and kind == 'book':
        if re.fullmatch(r'/works/OL\d+W', external_id):
            return lambda: openlibrary_scores({'externalId': external_id, 'title': title}, fetch)
        if external_id.startswith('isbn:'):
            return lambda: openlibrary_scores({'isbn': external_id[5:], 'title': title}, fetch)
    return None


def title_scores(kind, title, year, imdb_id='', *, creator='', source='', external_id='', isbn=''):
    item = get_catalog().identified(kind, source, external_id) if source and external_id else None
    if not item and kind == 'anime' and source == 'TVmaze' and external_id:
        item = get_catalog().identified('tv', source, external_id)
    item = item or get_catalog().matching(kind, title, year, creator) or {
        'type': kind, 'title': title, 'year': year, 'creator': creator,
        'source': source, 'externalId': external_id, 'isbn': normalize_isbn(isbn)}
    if kind in ('book', 'album'):
        return media_scores(item, isbn)
    imdb_id = item.get('imdbId') or imdb_id
    if not re.fullmatch(r'tt\d{5,12}', imdb_id or ''):
        imdb_id = ''
    scores = list(item.get('publicScores') or []) + get_ratings().lookup(imdb_id)
    extra = remote_scores(kind, title, imdb_id)
    scores.extend(extra['sources'])
    community = get_social().community_rating({**item, 'type': kind})
    if community:
        scores.append(community)
    scores = merge_scores(scores)
    if kind in ('tv', 'anime'):
        scores = [row for row in scores if row['source'] != 'Letterboxd']
    return {'sources': scores, 'imdbId': imdb_id, 'providerStatus': extra['providerStatus'], 'providerMeta': extra['providerMeta']}


def media_scores(item, isbn=''):
    kind = item['type']
    providers = {}
    identifier = ''
    if kind == 'book':
        isbn = normalize_isbn(item.get('isbn') or isbn)
        if item.get('source') == 'Open Library' and re.fullmatch(r'/works/OL\d+W', item.get('externalId') or ''):
            identifier = item['externalId']
        elif isbn:
            identifier = 'isbn:' + isbn
        providers['openlibrary'] = {'enabled': True, 'load': lambda: openlibrary_scores({**item, 'isbn': isbn}, fetch)}
        providers['googlebooks'] = {'enabled': bool(os.environ.get('GOOGLE_BOOKS_API_KEY')),
                                   'load': lambda: google_books_scores({**item, 'isbn': isbn}, fetch)}
    else:
        sources = [{'source': item.get('source'), 'externalId': item.get('externalId')}] + list(item.get('catalogSources') or [])
        identifier = next((s['externalId'] for s in sources if isinstance(s, dict) and s.get('source') == 'MusicBrainz'
                           and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(s.get('externalId') or ''))), '')
        musicbrainz_id = identifier
        if not identifier and item.get('source') == 'Apple Music' and re.fullmatch(r'[1-9]\d{0,19}', str(item.get('externalId') or '')):
            identifier = 'apple:' + item['externalId']
        def load_album():
            target = musicbrainz_id
            if not target:
                matched = match_album(item, fetch)
                if not matched:
                    return []
                target = matched['externalId']
            details = album_details(target, fetch)
            if (catalog_search_text(details['title']) != catalog_search_text(item['title'])
                    or catalog_search_text(details['creator']) != catalog_search_text(item.get('creator') or '')):
                raise ValueError('Album title or artist did not match the selected record.')
            # The original provider identity and artwork remain attached to this record.
            enriched = {**item, 'genres': list(dict.fromkeys(list(item.get('genres') or []) + details['genres'])),
                        'publicScores': merge_scores(item.get('publicScores') or [], details['publicScores']),
                        'catalogSources': list(item.get('catalogSources') or []) + [{
                            'source': 'MusicBrainz', 'externalId': target, 'sourceUrl': details['sourceUrl']}]}
            get_catalog().save(kind, None, {'items': [details]}, metadata_only=True)
            if item.get('source') != 'MusicBrainz' or item.get('externalId') != target:
                get_catalog().save(kind, None, {'items': [enriched]}, metadata_only=True)
            if identifier != target:
                get_ratings().save_provider_cache('musicbrainz', kind, target, details['publicScores'])
            return details['publicScores']
        providers['musicbrainz'] = {'enabled': True, 'load': load_album}
    extra = score_service().lookup(kind, identifier, providers, title=item.get('title'))
    scores = merge_scores(item.get('publicScores') or [], extra['sources'])
    community = get_social().community_rating(item)
    if community:
        scores = merge_scores(scores, [community])
    # Metadata may have been enriched during the cached provider lookup.
    saved = get_catalog().identified(kind, item.get('source', ''), item.get('externalId', ''))
    genres = list((saved or item).get('genres') or [])
    if kind == 'album' and identifier:
        canonical = get_catalog().identified(kind, 'MusicBrainz', identifier)
        if canonical:
            genres = list(dict.fromkeys(genres + list(canonical.get('genres') or [])))
    return {'sources': scores, 'providerStatus': extra['providerStatus'], 'providerMeta': extra['providerMeta'],
            'genres': genres}


def fetch(url, provider):
    # Coalesce public-provider requests and respect a modest per-provider request rate.
    with LOCK:
        wait = max(0, LAST.get(provider, 0) + 1.1 - time.monotonic())
        if wait:
            time.sleep(wait)
        LAST[provider] = time.monotonic()
    req = Request(url, headers={'User-Agent': 'Trace/0.1 (https://github.com/ACWArob/Trace)', 'Accept': 'application/vnd.api+json' if provider == 'kitsu' else 'application/json'})
    with urlopen(req, timeout=9, context=TLS_CONTEXT) as response:
        return json.load(response)


def cache_lifetime(key, result):
    if key[0] == 'profile':
        return 300
    if key[0] == 'book-description':
        return 3600
    return min(3600, query_cache_ttl(result))


def cache_put(key, result):
    with CACHE_LOCK:
        now = time.time()
        CACHE[key] = (now, result)
        CACHE.move_to_end(key)
        if len(CACHE) > CACHE_LIMIT:
            expired = [entry for entry, (cached_at, value) in CACHE.items()
                       if now - cached_at >= cache_lifetime(entry, value)]
            for entry in expired:
                del CACHE[entry]
            while len(CACHE) > CACHE_LIMIT:
                CACHE.popitem(last=False)


def catalog_cache_current(kind, result):
    versions = {'album': MUSIC_CATALOG_VERSION, 'movie': SCREEN_CATALOG_VERSION,
                'tv': SCREEN_CATALOG_VERSION, 'book': BOOK_CATALOG_VERSION}
    return kind not in versions or result.get('catalogVersion') == versions[kind]


def safe_cached_catalog(result):
    def eligible(item):
        if not is_movie_metadata(item):
            return False
        return not (item.get('type') == 'tv' and item.get('source') == WIKIDATA_SOURCE
                    and is_non_work_description(item.get('description'), item.get('title', '')))
    return {**result, 'items': [item for item in result.get('items', []) if isinstance(item, dict) and eligible(item)]}


def search(kind, query, *, persist=True, genre=''):
    if kind == 'book':
        query = normalize_isbn(query) or query
    cache_query = query if not (kind == 'album' and genre) else f'trace-genre:{genre}:{query}'
    cache_key = (kind, cache_query.casefold())
    with CACHE_LOCK:
        cached = CACHE.get(cache_key)
    if cached is not None:
        cached_at, cached_result = cached
        lifetime = cache_lifetime(cache_key, cached_result)
        if time.time() - cached_at < lifetime and catalog_cache_current(kind, cached_result):
            return attach_scores(cached_result)
    saved = get_catalog().cached(kind, cache_query)
    if saved and catalog_cache_current(kind, saved):
        return attach_scores(saved)
    provider = PROVIDERS[kind]
    items = []
    warnings = []
    if kind == 'movie':
        items = search_wikidata(query, 'movie', fetch)
    elif kind == 'album':
        result = attach_scores(search_albums(query, fetch, genre=genre))
        if persist:
            result = get_catalog().save(kind, cache_query, result)
            cache_put(cache_key, result)
        return result
    elif kind == 'tv':
        try:
            data = fetch('https://api.tvmaze.com/search/shows?' + urlencode({'q': query}), 'tvmaze')
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            data = []
            warnings.append('TVmaze is unavailable. Other television results are shown when available.')
        if not isinstance(data, list):
            data = []
            warnings.append('TVmaze returned an invalid response. Other television results are shown when available.')
        for result in data[:16]:
            item = tvmaze_item(result.get('show')) if isinstance(result, dict) else None
            if item:
                items.append(item)
        if not any(row['title'].casefold() == query.casefold() for row in items):
            try:
                items.extend(search_wikidata(query, 'tv', fetch))
            except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                if not items and warnings:
                    raise URLError('Television catalogs are unavailable.')
                warnings.append('Wikidata is unavailable. Other television results are shown when available.')
    elif kind == 'anime':
        data = fetch('https://kitsu.io/api/edge/anime?' + urlencode({'filter[text]': query, 'page[limit]': 16, 'include': 'categories'}), 'kitsu')
        category_names = {r['id']: r.get('attributes', {}).get('title', '') for r in data.get('included', []) if r.get('type') == 'categories'}
        for row in data.get('data', []):
            attr = row.get('attributes', {})
            items.append({'title': attr.get('titles', {}).get('en') or attr.get('canonicalTitle', ''),
                          'creator': '', 'year': (attr.get('startDate') or '')[:4], 'type': 'movie' if attr.get('subtype') == 'movie' else kind,
                          'image': (attr.get('posterImage') or {}).get('medium', ''),
                          'source': provider, 'sourceUrl': 'https://kitsu.app/anime/' + str(row['id']),
                          'externalId': str(row['id']), 'publicScores': [{'source':'Kitsu','value':float(attr['averageRating']),'max':100,'count':None,'url':'https://kitsu.app/anime/'+str(row['id'])}] if attr.get('averageRating') else [], 'genres': [category_names.get(r['id'], '') for r in row.get('relationships', {}).get('categories', {}).get('data', []) if category_names.get(r['id'])][:8], 'description': attr.get('synopsis') or ''})
    elif kind == 'book':
        items = search_books(query, fetch)
    unique = list({(r['type'], r['externalId']): r for r in items}.values())
    result = {'items': unique, 'provider': provider, 'warnings': warnings}
    if kind in ('movie', 'tv'):
        result['catalogVersion'] = SCREEN_CATALOG_VERSION
    elif kind == 'book':
        result['catalogVersion'] = BOOK_CATALOG_VERSION
    result = attach_scores(result)
    if persist:
        result = get_catalog().save(kind, query, result)
        cache_put(cache_key, result)
    return result


def letterboxd_poster(url):
    """A Letterboxd film's poster URL ('' when it has none), cached by film link."""
    url = letterboxd_film_url(url)
    key = ('letterboxd-poster', url)
    with CACHE_LOCK:
        cached = CACHE.get(key)
    if cached is not None and time.time() - cached[0] < cache_lifetime(key, cached[1]):
        return cached[1]['image']
    # boxd.it codes are case-sensitive but stored queries are casefolded, so hex-encode them.
    cache_query = 'trace-letterboxd-poster:' + url.encode().hex()
    saved = get_catalog().cached('movie', cache_query)
    if saved is None:
        with LOCK:
            wait = max(0, LAST.get('letterboxd', 0) + 1.1 - time.monotonic())
            if wait:
                time.sleep(wait)
            LAST['letterboxd'] = time.monotonic()
        saved = get_catalog().save('movie', cache_query, {'items': [], 'provider': 'Letterboxd',
                                                         'image': fetch_letterboxd_poster(url, TLS_CONTEXT)})
    result = {'image': saved.get('image') if isinstance(saved.get('image'), str) else ''}
    cache_put(key, result)
    return result['image']


def book_description(book_id):
    if not re.fullmatch(r'/works/OL\d+W', book_id):
        raise ValueError('Invalid book ID')
    key = ('book-description', book_id)
    with CACHE_LOCK:
        cached = CACHE.get(key)
    if cached is not None:
        cached_at, cached_result = cached
        if time.time() - cached_at < cache_lifetime(key, cached_result):
            return cached_result
    data = fetch('https://openlibrary.org' + book_id + '.json', 'openlibrary')
    description = data.get('description', '')
    if isinstance(description, dict):
        description = description.get('value', '')
    result = {'description': str(description or '')[:20000]}
    cache_put(key, result)
    return result


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def valid_host(self):
        try:
            host = urlparse('http://' + self.headers.get('Host', '')).hostname
            hosted = 'https://' + self.headers.get('Host', '').lower().removesuffix(':443') in allowed_origins()
            if production_mode():
                return hosted
            return host in ('localhost', '127.0.0.1', '::1') or hosted
        except ValueError:
            return False

    def request_origin(self):
        host = self.headers.get('Host', '')
        origin = 'https://' + host.lower().removesuffix(':443')
        if origin in allowed_origins():
            return origin
        return 'http://' + host

    def cookie_secure(self):
        return '; Secure' if self.request_origin().startswith('https://') else ''

    def budget(self, group, limit=120):
        key = (group, client_ip(self.client_address[0], self.headers))
        if not API_BUDGET.allow(key, limit):
            self.send_json({'error': 'Too many requests. Please try again in a minute.'}, 429, {'Retry-After': '60'})
            return False
        return True

    def hosted_catalog_access(self):
        if not production_mode():
            return True
        try:
            if not get_social().session(self.session_token())['user']:
                self.send_json({'error': 'Sign in to search the live catalogs and refresh scores.'}, 401)
                return False
        except database.Error:
            self.send_json({'error': 'Trace is temporarily unavailable.'}, 503)
            return False
        return self.budget('catalog', 90)

    def library_get(self, parsed):
        try:
            session = get_social().session(self.session_token())
            if not session['user']:
                raise SocialError('Sign in to open your private library.', 401)
            params = parse_qs(parsed.query)
            result = get_library().pull(session['user']['id'], int(params.get('after', ['0'])[0]), int(params.get('limit', ['200'])[0]))
            return self.send_json(result)
        except SocialError as exc:
            return self.send_json({'error': str(exc)}, exc.status)
        except ValueError:
            return self.send_json({'error': 'Invalid library cursor or page size.'}, 400)
        except database.Error:
            return self.send_json({'error': 'The library database is busy. Your device changes are still saved.'}, 503)

    def library_post(self):
        try:
            if self.headers.get('Origin') != self.request_origin():
                raise SocialError('This action must come from Trace.', 403)
            session = get_social().session(self.session_token())
            if not session['user']:
                raise SocialError('Sign in to sync your private library.', 401)
            csrf = self.headers.get('X-Trace-CSRF', '')
            if not csrf or not hmac.compare_digest(csrf, session['csrf']):
                raise SocialError('Refresh Trace and try again.', 403)
            payload = self.read_json(10_000_000)
            return self.send_json(get_library().sync(session['user']['id'], payload.get('changes')))
        except SocialError as exc:
            return self.send_json({'error': str(exc)}, exc.status)
        except database.Error:
            return self.send_json({'error': 'The library database is busy. Your device changes are still saved.'}, 503)

    def photo_library(self):
        # On Vercel the function's OIDC token arrives per request; env vars cover cron and local dev.
        store = photo_store.store_from_env(self.headers.get('x-vercel-oidc-token') or None)
        if store is None:
            raise photo_store.PhotoStoreError('Photo storage is not configured.')
        return photo_store.PhotoLibrary(get_database(), store)

    def photo_post(self):
        try:
            if self.headers.get('Origin') != self.request_origin():
                raise SocialError('This action must come from Trace.', 403)
            session = get_social().session(self.session_token())
            if not session['user']:
                raise SocialError('Sign in to sync your photos.', 401)
            csrf = self.headers.get('X-Trace-CSRF', '')
            if not csrf or not hmac.compare_digest(csrf, session['csrf']):
                raise SocialError('Refresh Trace and try again.', 403)
            content_type = self.headers.get('Content-Type', '').split(';')[0].strip().lower()
            native = content_type == 'application/json'
            if not native and content_type not in photo_store.PHOTO_TYPES:
                raise photo_store.PhotoError('Choose a JPEG, PNG, or WebP photo.', 415)
            max_bytes = photo_store.photo_byte_limit()
            # Native bodies carry base64 (4 characters per 3 bytes) inside a small JSON object.
            limit = -(-max_bytes // 3) * 4 + 256 if native else max_bytes
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError:
                length = 0
            if length > limit:
                raise photo_store.PhotoError('The photo is too large.', 413)
            if length <= 0:
                raise photo_store.PhotoError('Send a photo.')
            body = self.rfile.read(length)
            if native:
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    raise photo_store.PhotoError('The request is not valid JSON.') from exc
                if not isinstance(payload, dict):
                    raise photo_store.PhotoError('Send a JSON object.')
                content_type, body = photo_store.decode_native(payload)
            photo_id = self.headers.get('X-Trace-Photo-Id', '')
            return self.send_json(self.photo_library().upload(session['user']['id'], photo_id, content_type, body))
        except (SocialError, photo_store.PhotoError) as exc:
            return self.send_json({'error': str(exc)}, exc.status)
        except (photo_store.PhotoStoreError, database.Error):
            return self.send_json({'error': 'Photo sync is temporarily unavailable. Your device changes are still saved.'}, 503)

    def photo_get(self, parsed):
        try:
            session = get_social().session(self.session_token())
            if not session['user']:
                raise SocialError('Sign in to open your photos.', 401)
            photo_id = parsed.path[len('/api/photos/'):]
            content_type, data = self.photo_library().read(session['user']['id'], photo_id)
        except SocialError as exc:
            return self.send_json({'error': str(exc)}, exc.status)
        except photo_store.PhotoMissing:
            return self.send_json({'error': 'Photo not found.'}, 404)
        except (photo_store.PhotoStoreError, database.Error):
            return self.send_json({'error': 'Photos are temporarily unavailable.'}, 503)
        if parse_qs(parsed.query).get('encoding') == ['base64']:
            return self.send_json({'contentType': content_type, 'data': base64.b64encode(data).decode()})
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'private, max-age=86400')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            return cookie['trace_session'].value if 'trace_session' in cookie else ''
        except (ValueError, KeyError):
            return ''

    def read_json(self, limit=1000000):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= limit or self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                raise SocialError('Send a JSON request within the size limit.')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise SocialError('Send a JSON object.')
            return payload
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, SocialError):
                raise
            raise SocialError('The request is not valid JSON.') from exc

    def social_post(self, path):
        try:
            if self.headers.get('Origin') != self.request_origin():
                raise SocialError('This action must come from Trace.', 403)
            payload = self.read_json()
            store, token = get_social(), self.session_token()
            if path in ('/api/auth/signup', '/api/auth/login', '/api/auth/recover'):
                if not auth_budget().allow('auth:' + client_ip(self.client_address[0], self.headers), 15):
                    raise SocialError('Too many sign-in attempts. Try again in a minute.', 429)
                if path.endswith('signup'):
                    mode = signup_mode()
                    if mode == 'closed':
                        raise SocialError('New accounts are currently closed.', 403)
                    result, new_token = store.register(payload, invite_required=mode == 'invite')
                elif path.endswith('recover'):
                    result, new_token = store.recover(payload)
                else:
                    result, new_token = store.login(payload)
                return self.send_json(result, 201 if path.endswith('signup') else 200,
                                      {'Set-Cookie': f'trace_session={new_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000{self.cookie_secure()}'})
            session = store.session(token)
            if not session['user']:
                raise SocialError('Sign in to take part.', 401)
            csrf = self.headers.get('X-Trace-CSRF', '')
            if not csrf or not hmac.compare_digest(csrf, session['csrf']):
                raise SocialError('Refresh Trace and try again.', 403)
            user_id = session['user']['id']
            if path == '/api/auth/logout':
                store.logout(token)
                return self.send_json({'user': None}, headers={'Set-Cookie': 'trace_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0' + self.cookie_secure()})
            if path == '/api/auth/recovery-codes':
                if not auth_budget().allow('auth:' + client_ip(self.client_address[0], self.headers), 15):
                    raise SocialError('Too many account requests. Try again in a minute.', 429)
                return self.send_json(store.issue_recovery_codes(user_id, payload.get('password')))
            if path == '/api/auth/delete':
                if not auth_budget().allow('auth:' + client_ip(self.client_address[0], self.headers), 15):
                    raise SocialError('Too many account requests. Try again in a minute.', 429)
                if payload.get('confirmation') != session['user']['username']:
                    raise SocialError('Type your username to confirm account deletion.')
                result = store.delete_account(user_id, payload.get('password'), payload.get('confirmation'))
                return self.send_json(result, headers={'Set-Cookie': 'trace_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0' + self.cookie_secure()})
            if path == '/api/social/profile':
                return self.send_json({'user': store.update_profile(user_id, payload)})
            if path == '/api/social/follow':
                return self.send_json({'user': store.follow(user_id, payload.get('username'), payload.get('following') is True)})
            if path == '/api/social/reviews':
                return self.send_json({'review': store.publish(user_id, payload)}, 201)
            if path == '/api/social/like':
                return self.send_json({'review': store.like(user_id, str(payload.get('id', '')), payload.get('liked') is True)})
            if path == '/api/social/comment':
                return self.send_json({'review': store.comment(user_id, str(payload.get('id', '')), payload.get('text'))}, 201)
            if path == '/api/social/hide':
                return self.send_json(store.set_hidden(user_id, str(payload.get('id', '')), payload.get('hidden') is True))
            raise SocialError('Unknown social action.', 404)
        except SocialError as exc:
            return self.send_json({'error': str(exc)}, exc.status, {'Retry-After': '60'} if exc.status == 429 else None)
        except database.Error:
            return self.send_json({'error': 'The social database is busy. Please try again.'}, 503)

    def social_get(self, parsed):
        try:
            store = get_social()
            session = store.session(self.session_token())
            viewer = session['user']['id'] if session['user'] else None
            params = parse_qs(parsed.query)
            get = lambda key, default='': params.get(key, [default])[0]
            if parsed.path == '/api/auth/session':
                return self.send_json(session)
            if parsed.path == '/api/social/feed':
                return self.send_json(store.feed(viewer, get('scope', 'everyone'), get('username'), int(get('page', '0'))))
            if parsed.path == '/api/social/people':
                return self.send_json({'items': store.people(get('q'), viewer)})
            if parsed.path == '/api/social/profile':
                return self.send_json({'user': store.profile(get('username'), viewer)})
            match = re.fullmatch(r'/api/social/reviews/([a-f0-9]{32})', parsed.path)
            if match:
                return self.send_json({'review': store.review(match[1], viewer)})
            raise SocialError('Not found.', 404)
        except SocialError as exc:
            return self.send_json({'error': str(exc)}, exc.status)
        except ValueError:
            return self.send_json({'error': 'Invalid page.'}, 400)
        except database.Error:
            return self.send_json({'error': 'The social database is busy. Please try again.'}, 503)

    def do_POST(self):
        if not self.valid_host():
            return self.send_json({'error': 'Unknown host.'}, 403)
        if production_mode() and not self.budget('write', 180):
            return
        path = urlparse(self.path).path
        if path == '/api/library/sync':
            return self.library_post()
        if path == '/api/photos':
            return self.photo_post()
        if path.startswith(('/api/social/', '/api/auth/')):
            return self.social_post(path)
        if path != '/api/profile-connect':
            return self.send_json({'error': 'Unknown endpoint.'}, 404)
        if not self.hosted_catalog_access():
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048 or 'application/json' not in self.headers.get('Content-Type', ''):
                return self.send_json({'error': 'Enter a profile URL.'}, 400)
            payload = json.loads(self.rfile.read(length))
            service = payload.get('service', '')
            profile = payload.get('profile', '')
            key = ('profile', str(service), str(profile))
            with CACHE_LOCK:
                cached = CACHE.get(key)
            if cached is not None:
                cached_at, cached_result = cached
                if time.time() - cached_at < cache_lifetime(key, cached_result):
                    return self.send_json(cached_result)
            result = read_profile_feed(service, profile, TLS_CONTEXT)
            cache_put(key, result)
            return self.send_json(result)
        except (ValueError, TypeError, AttributeError):
            return self.send_json({'error': 'Use a Letterboxd member profile or a Goodreads member profile URL.'}, 400)
        except (FeedUnavailable, HTTPError, URLError, TimeoutError, OSError):
            return self.send_json({'error': 'This public profile feed is unavailable. Check that the profile is public, try again later, or import its CSV export.'}, 502)

    def cron_maintenance(self):
        # Authenticated by the shared secret, not cookies, so it skips the Host
        # check: Vercel's scheduler may call a deployment URL, not the domain.
        secret = os.environ.get('CRON_SECRET', '')
        supplied = self.headers.get('Authorization', '') or ''
        if not secret or not hmac.compare_digest(supplied.encode(), ('Bearer ' + secret).encode()):
            return self.send_json({'error': 'Unauthorized.'}, 401)
        try:
            store = photo_store.store_from_env(self.headers.get('x-vercel-oidc-token') or None)
            return self.send_json(maintenance.run(get_database(), service=score_service(), loader_for=score_loader, photo_store=store))
        except database.Error:
            return self.send_json({'error': 'Maintenance could not reach the database.'}, 503)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/cron/maintenance':
            return self.cron_maintenance()
        if not self.valid_host():
            return self.send_json({'error': 'Unknown host.'}, 403)
        if parsed.path == '/api/health':
            # Shallow by default so uptime checks never wake the database.
            if parse_qs(parsed.query).get('deep') != ['1']:
                return self.send_json({'status': 'ok'})
            if production_mode() and not self.budget('read', 240):
                return
            try:
                with get_database().connect() as db:
                    db.execute('SELECT 1').fetchone()
                return self.send_json({'status': 'ok', 'database': 'ok'})
            except database.Error:
                return self.send_json({'status': 'unavailable'}, 503)
        if production_mode() and parsed.path.startswith('/api/') and not self.budget('read', 240):
            return
        if parsed.path == '/api/auth/config':
            return self.send_json({'signupMode': signup_mode(), 'recovery': 'codes'})
        if parsed.path in ('/api/search', '/api/ratings', '/api/book-description', '/api/letterboxd-poster') and not self.hosted_catalog_access():
            return
        if parsed.path == '/api/library':
            return self.library_get(parsed)
        if parsed.path.startswith('/api/photos/'):
            return self.photo_get(parsed)
        if parsed.path.startswith(('/api/social/', '/api/auth/')):
            return self.social_get(parsed)
        if parsed.path == '/api/ratings':
            params = parse_qs(parsed.query)
            kind = params.get('type', [''])[0]
            title, year = params.get('title', [''])[0][:300], params.get('year', [''])[0][:4]
            if kind not in PROVIDERS or not title:
                return self.send_json({'error': 'Choose a catalog title.'}, 400)
            try:
                return self.send_json(title_scores(kind, title, year, params.get('imdbId', [''])[0],
                    creator=params.get('creator', [''])[0][:500], source=params.get('source', [''])[0][:100],
                    external_id=params.get('externalId', [''])[0][:100], isbn=params.get('isbn', [''])[0][:40]))
            except database.Error:
                return self.send_json({'error': 'The ratings database is busy. Try again shortly.'}, 503)
        if parsed.path == '/api/catalog':
            params = parse_qs(parsed.query)
            try:
                return self.send_json(get_catalog().browse(params.get('type', ['all'])[0], params.get('q', [''])[0][:200], params.get('genre', [''])[0][:40], int(params.get('page', ['0'])[0])))
            except ValueError:
                return self.send_json({'error': 'Invalid catalog filter.'}, 400)
        if parsed.path == '/api/book-description':
            book_id = parse_qs(parsed.query).get('id', [''])[0]
            if not re.fullmatch(r'/works/OL\d+W', book_id):
                return self.send_json({'error': 'Invalid book ID.'}, 400)
            try:
                return self.send_json(book_description(book_id))
            except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                return self.send_json({'error': 'Description unavailable.'}, 502)
        if parsed.path == '/api/letterboxd-poster':
            try:
                image = letterboxd_poster(parse_qs(parsed.query).get('url', [''])[0])
            except ValueError:
                return self.send_json({'error': 'Use a Letterboxd film link.'}, 400)
            except (HTTPError, URLError, TimeoutError, OSError):
                return self.send_json({'error': 'Letterboxd is unavailable right now.'}, 502)
            except database.Error:
                return self.send_json({'error': 'The catalog database is busy. Try again shortly.'}, 503)
            if not image:
                return self.send_json({'error': 'No Letterboxd poster found.'}, 404)
            return self.send_json({'image': image})
        if parsed.path == '/api/search':
            params = parse_qs(parsed.query)
            kind = params.get('type', ['book'])[0]
            query = params.get('q', [''])[0].strip()
            genre = params.get('genre', [''])[0][:80] if kind == 'album' else ''
            valid_genre = bool(re.fullmatch(r"[\w &+/'’\-]{2,80}", genre))
            if kind not in PROVIDERS or len(query) > 200 or (genre and not valid_genre) or (len(query) < 2 and not (kind == 'album' and valid_genre)):
                return self.send_json({'error': 'Choose a category and enter 2–200 characters.'}, 400)
            if kind == 'book':
                query = normalize_isbn(query) or query
            try:
                return self.send_json(search(kind, query, genre=genre))
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, OSError):
                cache_query = query if not genre else f'trace-genre:{genre}:{query}'
                saved = get_catalog().cached(kind, cache_query, stale=True)
                if saved:
                    return self.send_json(safe_cached_catalog(saved))
                return self.send_json({'error': f'{PROVIDERS[kind]} is unavailable right now. Try again or add it manually.'}, 502)
        # Serve only app assets; never expose repository metadata or source/test files.
        clean = parsed.path
        if clean not in PUBLIC_FILES and not re.fullmatch(r'/assets/[\w.-]+', clean):
            return self.send_error(404)
        return super().do_GET()

    def send_json(self, data, status=200, headers=None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self.path.startswith('/api/'):
            clean = urlparse(self.path).path
            public = clean in PUBLIC_FILES or re.fullmatch(r'/assets/[\w.-]+', clean)
            self.send_header('Cache-Control', 'public, max-age=0, must-revalidate' if public else 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        super().end_headers()


if __name__ == '__main__':
    public_origin()  # Reject a malformed deployment origin before accepting requests.
    load_local_rating_key()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Trace is ready at http://localhost:{port}', flush=True)
    server.serve_forever()
