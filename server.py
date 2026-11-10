"""Trace's local server: static UI + small, cached public-catalog adapters."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path
import json
import os
import re
import html
import sys
import time
import threading
import ssl
import hmac
import sqlite3
from http.cookies import SimpleCookie
from catalog_store import CatalogStore, query_cache_ttl
from music_catalog import search_albums, CATALOG_VERSION as MUSIC_CATALOG_VERSION
from screen_catalog import search_wikidata, CATALOG_VERSION as SCREEN_CATALOG_VERSION
from movie_catalog import is_movie_metadata, is_non_work_description, WIKIDATA_SOURCE
from social import SocialStore, SocialError
from library_store import LibraryStore
from score_service import ScoreService
from ratings import RatingsStore, omdb_scores, mdblist_scores, merge_scores, load_local_rating_key
from profile_feeds import read_profile_feed, FeedUnavailable

ROOT = Path(__file__).resolve().parent
CACHE = {}
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

CATALOG_STORE = None
SOCIAL_STORE = None
RATINGS_STORE = None
LIBRARY_STORE = None
STORE_LOCK = threading.Lock()
AUTH_ATTEMPTS = {}
PUBLIC_FILES = frozenset(('/', '/index.html', '/styles.css', '/app.js', '/core.mjs', '/data.js', '/views.mjs', '/social.js', '/library_sync.mjs', '/api_client.mjs', '/artwork.mjs', '/mobile.js', '/sw.js', '/manifest.webmanifest'))
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


def get_catalog():
    global CATALOG_STORE
    with STORE_LOCK:
        if CATALOG_STORE is None:
            CATALOG_STORE = CatalogStore()
    return CATALOG_STORE


def get_social():
    global SOCIAL_STORE
    with STORE_LOCK:
        if SOCIAL_STORE is None:
            SOCIAL_STORE = SocialStore()
    return SOCIAL_STORE


def get_ratings():
    global RATINGS_STORE
    with STORE_LOCK:
        if RATINGS_STORE is None:
            RATINGS_STORE = RatingsStore()
    return RATINGS_STORE


def get_library():
    get_social()  # Existing accounts own private library rows.
    global LIBRARY_STORE
    with STORE_LOCK:
        if LIBRARY_STORE is None:
            LIBRARY_STORE = LibraryStore()
    return LIBRARY_STORE


def attach_scores(result):
    for item in result.get('items', []):
        sources = list(item.get('publicScores') or [])
        sources.extend(get_ratings().lookup(item.get('imdbId', '')))
        item['publicScores'] = merge_scores(sources)
    return result


def remote_scores(kind, title, imdb_id):
    global SCORE_SERVICE
    store = get_ratings()
    with STORE_LOCK:
        if SCORE_SERVICE is None or SCORE_SERVICE.store is not store:
            SCORE_SERVICE = ScoreService(store)
        service = SCORE_SERVICE
    return service.lookup(kind, imdb_id, {
        'omdb': {'enabled': bool(os.environ.get('OMDB_API_KEY')), 'load': lambda: omdb_scores(imdb_id, title, fetch)},
        'mdblist': {'enabled': bool(os.environ.get('MDBLIST_API_KEY')), 'load': lambda: mdblist_scores(imdb_id, title, kind, fetch)},
    })


def title_scores(kind, title, year, imdb_id=''):
    item = get_catalog().matching(kind, title, year) or {'type': kind, 'title': title, 'year': year}
    imdb_id = item.get('imdbId') or imdb_id
    if not re.fullmatch(r'tt\d{5,12}', imdb_id or ''):
        imdb_id = ''
    scores = list(item.get('publicScores') or []) + get_ratings().lookup(imdb_id)
    extra = remote_scores(kind, title, imdb_id)
    scores.extend(extra['sources'])
    community = get_social().community_rating(item)
    if community:
        scores.append(community)
    return {'sources': merge_scores(scores), 'imdbId': imdb_id, 'providerStatus': extra['providerStatus'], 'providerMeta': extra['providerMeta']}


def fetch(url, provider):
    # Coalesce public-provider requests and respect a modest per-provider request rate.
    with LOCK:
        wait = max(0, LAST.get(provider, 0) + 1.1 - time.monotonic())
        if wait:
            time.sleep(wait)
        LAST[provider] = time.monotonic()
    req = Request(url, headers={'User-Agent': 'Trace/0.1 (https://github.com/ACWArob/Trace)', 'Accept': 'application/vnd.api+json' if provider == 'kitsu' else 'application/json'})
    with urlopen(req, timeout=18, context=TLS_CONTEXT) as response:
        return json.load(response)


def catalog_cache_current(kind, result):
    if kind not in ('album', 'movie', 'tv'):
        return True
    return result.get('catalogVersion') == (MUSIC_CATALOG_VERSION if kind == 'album' else SCREEN_CATALOG_VERSION)


def safe_cached_catalog(result):
    def eligible(item):
        if not is_movie_metadata(item):
            return False
        return not (item.get('type') == 'tv' and item.get('source') == WIKIDATA_SOURCE
                    and is_non_work_description(item.get('description'), item.get('title', '')))
    return {**result, 'items': [item for item in result.get('items', []) if isinstance(item, dict) and eligible(item)]}


def search(kind, query):
    cache_key = (kind, query.casefold())
    if cache_key in CACHE:
        cached_at, cached_result = CACHE[cache_key]
        lifetime = min(3600, query_cache_ttl(cached_result))
        if time.time() - cached_at < lifetime and catalog_cache_current(kind, cached_result):
            return attach_scores(cached_result)
    saved = get_catalog().cached(kind, query)
    if saved and catalog_cache_current(kind, saved):
        return attach_scores(saved)
    provider = PROVIDERS[kind]
    items = []
    warnings = []
    if kind == 'movie':
        items = search_wikidata(query, 'movie', fetch)
    elif kind == 'album':
        result = attach_scores(search_albums(query, fetch))
        result = get_catalog().save(kind, query, result)
        CACHE[cache_key] = (time.time(), result)
        return result
    elif kind == 'tv':
        try:
            data = fetch('https://api.tvmaze.com/search/shows?' + urlencode({'q': query}), 'tvmaze')
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            data = []
            warnings.append('TVmaze is unavailable. Other television results are shown when available.')
        for result in data[:16]:
            row = result['show']
            items.append({'title': row['name'], 'creator': (row.get('network') or row.get('webChannel') or {}).get('name', ''),
                          'year': (row.get('premiered') or '')[:4], 'type': kind,
                          'image': (row.get('image') or {}).get('medium', ''), 'source': 'TVmaze',
                          'sourceUrl': row.get('url', ''), 'externalId': str(row['id']), 'genres': row.get('genres') or [], 'imdbId': (row.get('externals') or {}).get('imdb') or '', 'publicScores': [{'source':'TVmaze','value':row['rating']['average'],'max':10,'count':None,'url':row.get('url','')}] if (row.get('rating') or {}).get('average') is not None else [], 'description': html.unescape(re.sub(r'<[^>]*>', '', row.get('summary') or ''))})
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
        data = fetch('https://openlibrary.org/search.json?' + urlencode({'q': query, 'limit': 16,
                     'fields': 'key,title,author_name,first_publish_year,cover_i,subject,ratings_average,ratings_count'}), 'openlibrary')
        for row in data.get('docs', []):
            items.append({'title': row['title'], 'creator': ', '.join(row.get('author_name', [])[:2]),
                          'year': str(row.get('first_publish_year', '')), 'type': kind,
                          'image': f"https://covers.openlibrary.org/b/id/{row['cover_i']}-L.jpg" if row.get('cover_i') else '',
                          'source': provider, 'sourceUrl': 'https://openlibrary.org' + row['key'], 'externalId': row['key'], 'genres': (row.get('subject') or [])[:8], 'publicScores': [{'source':'Open Library','value':row['ratings_average'],'max':5,'count':row.get('ratings_count'),'url':'https://openlibrary.org'+row['key']}] if row.get('ratings_average') is not None else []})
    unique = list({(r['type'], r['externalId']): r for r in items}.values())
    result = attach_scores({'items': unique, 'provider': provider, 'warnings': warnings, **({'catalogVersion':SCREEN_CATALOG_VERSION} if kind in ('movie','tv') else {})})
    result = get_catalog().save(kind, query, result)
    CACHE[cache_key] = (time.time(), result)
    return result


def book_description(book_id):
    if not re.fullmatch(r'/works/OL\d+W', book_id):
        raise ValueError('Invalid book ID')
    key = ('book-description', book_id)
    if key in CACHE and time.time() - CACHE[key][0] < 3600:
        return CACHE[key][1]
    data = fetch('https://openlibrary.org' + book_id + '.json', 'openlibrary')
    description = data.get('description', '')
    if isinstance(description, dict):
        description = description.get('value', '')
    result = {'description': str(description or '')[:20000]}
    CACHE[key] = (time.time(), result)
    return result


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def valid_host(self):
        try:
            host = urlparse('http://' + self.headers.get('Host', '')).hostname
            configured = public_origin()
            return host in ('localhost', '127.0.0.1', '::1') or bool(configured and 'https://' + self.headers.get('Host', '').lower().removesuffix(':443') == configured)
        except ValueError:
            return False

    def request_origin(self):
        configured = public_origin()
        host = self.headers.get('Host', '')
        if configured and 'https://' + host.lower().removesuffix(':443') == configured:
            return configured
        return 'http://' + host

    def cookie_secure(self):
        return '; Secure' if self.request_origin().startswith('https://') else ''

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
        except sqlite3.Error:
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
        except sqlite3.Error:
            return self.send_json({'error': 'The library database is busy. Your device changes are still saved.'}, 503)

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
            if path in ('/api/auth/signup', '/api/auth/login'):
                key, now = self.client_address[0], time.monotonic()
                with STORE_LOCK:
                    attempts = [t for t in AUTH_ATTEMPTS.get(key, []) if now - t < 60]
                    if len(attempts) >= 15:
                        raise SocialError('Too many sign-in attempts. Try again in a minute.', 429)
                    AUTH_ATTEMPTS[key] = attempts + [now]
                result, new_token = store.register(payload) if path.endswith('signup') else store.login(payload)
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
            return self.send_json({'error': str(exc)}, exc.status)
        except sqlite3.Error:
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
        except sqlite3.Error:
            return self.send_json({'error': 'The social database is busy. Please try again.'}, 503)

    def do_POST(self):
        if not self.valid_host():
            return self.send_json({'error': 'Unknown host.'}, 403)
        path = urlparse(self.path).path
        if path == '/api/library/sync':
            return self.library_post()
        if path.startswith(('/api/social/', '/api/auth/')):
            return self.social_post(path)
        if path != '/api/profile-connect':
            return self.send_json({'error': 'Unknown endpoint.'}, 404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048 or 'application/json' not in self.headers.get('Content-Type', ''):
                return self.send_json({'error': 'Enter a profile URL.'}, 400)
            payload = json.loads(self.rfile.read(length))
            service = payload.get('service', '')
            profile = payload.get('profile', '')
            key = ('profile', str(service), str(profile))
            if key in CACHE and time.time() - CACHE[key][0] < 300:
                return self.send_json(CACHE[key][1])
            result = read_profile_feed(service, profile, TLS_CONTEXT)
            CACHE[key] = (time.time(), result)
            return self.send_json(result)
        except (ValueError, TypeError, AttributeError):
            return self.send_json({'error': 'Use a Letterboxd member profile or a Goodreads member profile URL.'}, 400)
        except (FeedUnavailable, HTTPError, URLError, TimeoutError, OSError):
            return self.send_json({'error': 'This public profile feed is unavailable. Check that the profile is public, try again later, or import its CSV export.'}, 502)

    def do_GET(self):
        if not self.valid_host():
            return self.send_json({'error': 'Unknown host.'}, 403)
        parsed = urlparse(self.path)
        if parsed.path == '/api/library':
            return self.library_get(parsed)
        if parsed.path.startswith(('/api/social/', '/api/auth/')):
            return self.social_get(parsed)
        if parsed.path == '/api/ratings':
            params = parse_qs(parsed.query)
            kind = params.get('type', [''])[0]
            title, year = params.get('title', [''])[0][:300], params.get('year', [''])[0][:4]
            if kind not in PROVIDERS or not title:
                return self.send_json({'error': 'Choose a catalog title.'}, 400)
            try:
                return self.send_json(title_scores(kind, title, year, params.get('imdbId', [''])[0]))
            except sqlite3.Error:
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
        if parsed.path == '/api/search':
            params = parse_qs(parsed.query)
            kind = params.get('type', ['book'])[0]
            query = params.get('q', [''])[0].strip()
            if kind not in PROVIDERS or not 2 <= len(query) <= 200:
                return self.send_json({'error': 'Choose a category and enter 2–200 characters.'}, 400)
            try:
                return self.send_json(search(kind, query))
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, OSError):
                saved = get_catalog().cached(kind, query, stale=True)
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
