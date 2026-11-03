"""Trace's local server: static UI + small, cached public-catalog adapters."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path
import json
import re
import html
import sys
import time
import threading
import ssl
import hmac
import sqlite3
from http.cookies import SimpleCookie
from catalog_store import CatalogStore
from social import SocialStore, SocialError
from ratings import RatingsStore, omdb_scores
from profile_feeds import read_profile_feed, FeedUnavailable

ROOT = Path(__file__).resolve().parent
CACHE = {}
LOCK = threading.Lock()
LAST = {}
TLS_CONTEXT = ssl.create_default_context()
if not TLS_CONTEXT.get_ca_certs():
    for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if Path(bundle).is_file():
            TLS_CONTEXT.load_verify_locations(bundle)
            break
PROVIDERS = {'movie': 'Wikidata / Wikipedia', 'tv': 'TVmaze', 'anime': 'Kitsu',
             'book': 'Open Library', 'album': 'Apple Music'}

CATALOG_STORE = None
SOCIAL_STORE = None
RATINGS_STORE = None
STORE_LOCK = threading.Lock()
AUTH_ATTEMPTS = {}


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


def attach_scores(result):
    for item in result.get('items', []):
        sources = list(item.get('publicScores') or [])
        sources.extend(get_ratings().lookup(item.get('imdbId', '')))
        item['publicScores'] = list({s['source'].lower(): s for s in sources}.values())
    return result


def title_scores(kind, title, year, imdb_id=''):
    item = get_catalog().matching(kind, title, year) or {'type': kind, 'title': title, 'year': year}
    imdb_id = item.get('imdbId') or imdb_id
    if not re.fullmatch(r'tt\d{5,12}', imdb_id or ''):
        imdb_id = ''
    scores = list(item.get('publicScores') or []) + get_ratings().lookup(imdb_id)
    key = ('score-lookup', imdb_id)
    if imdb_id:
        try:
            if key in CACHE and time.time() - CACHE[key][0] < 86400:
                extra = CACHE[key][1]
            else:
                extra = omdb_scores(imdb_id, title, fetch)
                CACHE[key] = (time.time(), extra)
            scores.extend(extra)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            pass
    community = get_social().community_rating(item)
    if community:
        scores.append(community)
    return {'sources': list({s['source'].lower(): s for s in scores}.values()), 'imdbId': imdb_id}


def fetch(url, provider):
    # Coalesce public-provider requests and respect a modest per-provider request rate.
    with LOCK:
        wait = max(0, LAST.get(provider, 0) + 1.1 - time.monotonic())
        if wait:
            time.sleep(wait)
        LAST[provider] = time.monotonic()
    req = Request(url, headers={'User-Agent': 'TraceLocal/0.1 (personal collection prototype)', 'Accept': 'application/vnd.api+json' if provider == 'kitsu' else 'application/json'})
    with urlopen(req, timeout=18, context=TLS_CONTEXT) as response:
        return json.load(response)


def search(kind, query):
    cache_key = (kind, query.casefold())
    if cache_key in CACHE and time.time() - CACHE[cache_key][0] < 3600:
        return attach_scores(CACHE[cache_key][1])
    saved = get_catalog().cached(kind, query)
    if saved:
        return attach_scores(saved)
    provider = PROVIDERS[kind]
    items = []
    if kind == 'movie':
        data = fetch('https://www.wikidata.org/w/api.php?' + urlencode({
            'action': 'wbsearchentities', 'search': query, 'language': 'en', 'format': 'json', 'limit': 20}), 'wikidata')
        hits = [x for x in data.get('search', []) if re.search(r'\bfilm\b', x.get('description', ''), re.I)][:12]
        if hits:
            entities = fetch('https://www.wikidata.org/w/api.php?' + urlencode({
                'action': 'wbgetentities', 'ids': '|'.join(x['id'] for x in hits),
                'props': 'sitelinks|claims', 'format': 'json'}), 'wikidata').get('entities', {})
            titles = [x.get('sitelinks', {}).get('enwiki', {}).get('title', '') for x in entities.values()]
            titles = [x for x in titles if x]
            images = {}
            descriptions = {}
            if titles:
                try:
                    pages = fetch('https://en.wikipedia.org/w/api.php?' + urlencode({
                        'action': 'query', 'format': 'json', 'titles': '|'.join(titles), 'prop': 'pageimages|extracts', 'exintro': 1, 'explaintext': 1, 'exsentences': 5,
                        'piprop': 'thumbnail', 'pithumbsize': 500, 'pilicense': 'any'}), 'wikipedia').get('query', {}).get('pages', {})
                    images = {x['title']: x.get('thumbnail', {}).get('source', '') for x in pages.values()}
                    descriptions = {x['title']: x.get('extract', '') for x in pages.values()}
                except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                    pass  # A cover outage must not prevent logging a found film.
            genre_ids = sorted({claim.get('mainsnak', {}).get('datavalue', {}).get('value', {}).get('id')
                                for entity in entities.values() for claim in entity.get('claims', {}).get('P136', [])} - {None})
            genre_labels = {}
            if genre_ids:
                try:
                    genre_data = fetch('https://www.wikidata.org/w/api.php?' + urlencode({
                        'action': 'wbgetentities', 'ids': '|'.join(genre_ids[:50]), 'props': 'labels',
                        'languages': 'en', 'format': 'json'}), 'wikidata').get('entities', {})
                    genre_labels = {key: val.get('labels', {}).get('en', {}).get('value', '').removesuffix(' film').title() for key, val in genre_data.items()}
                except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                    pass
            for hit in hits:
                entity = entities.get(hit['id'], {})
                wiki_title = entity.get('sitelinks', {}).get('enwiki', {}).get('title', '')
                description = hit.get('description', '')
                year_match = re.search(r'\b(18|19|20)\d{2}\b', description)
                director = re.search(r'(?:directed by|film by) (.+)$', description)
                items.append({'title': hit.get('label', ''), 'creator': director.group(1) if director else '',
                              'year': year_match.group(0) if year_match else '', 'type': 'movie',
                              'genres': [genre_labels[g] for claim in entity.get('claims', {}).get('P136', []) if (g := claim.get('mainsnak', {}).get('datavalue', {}).get('value', {}).get('id')) in genre_labels][:8],
                              'imdbId': next((c.get('mainsnak', {}).get('datavalue', {}).get('value', '') for c in entity.get('claims', {}).get('P345', []) if re.fullmatch(r'tt\d{5,12}', str(c.get('mainsnak', {}).get('datavalue', {}).get('value', '')))), ''),
                              'image': images.get(wiki_title, ''), 'description': descriptions.get(wiki_title, '') or description, 'source': provider,
                              'sourceUrl': 'https://www.wikidata.org/wiki/' + hit['id'], 'externalId': hit['id']})
    elif kind == 'album':
        args = {'term': query, 'entity': 'album', 'media': 'music', 'limit': 30, 'country': 'US'}
        data = fetch('https://itunes.apple.com/search?' + urlencode(args, quote_via=quote), 'apple')
        for row in data.get('results', []):
            # Some single releases are represented as Album collections by Apple.
            title = row.get('collectionName', '')
            if row.get('collectionType') != 'Album' or re.search(r' - (Single|EP)$', title, re.I):
                continue
            items.append({'title': title, 'creator': row.get('artistName', ''), 'year': row.get('releaseDate', '')[:4],
                          'type': kind, 'image': row.get('artworkUrl100', '').replace('100x100bb', '600x600bb'),
                          'source': provider, 'sourceUrl': row.get('collectionViewUrl', ''),
                          'externalId': str(row.get('collectionId', '')), 'genres': [row['primaryGenreName']] if row.get('primaryGenreName') else [], 'description': f"{title} is an album by {row.get('artistName', 'an unknown artist')}." + (f" Released in {row['releaseDate'][:4]}." if row.get('releaseDate') else '')})
    elif kind == 'tv':
        data = fetch('https://api.tvmaze.com/search/shows?' + urlencode({'q': query}), 'tvmaze')
        for result in data[:16]:
            row = result['show']
            items.append({'title': row['name'], 'creator': (row.get('network') or row.get('webChannel') or {}).get('name', ''),
                          'year': (row.get('premiered') or '')[:4], 'type': kind,
                          'image': (row.get('image') or {}).get('medium', ''), 'source': provider,
                          'sourceUrl': row.get('url', ''), 'externalId': str(row['id']), 'genres': row.get('genres') or [], 'imdbId': (row.get('externals') or {}).get('imdb') or '', 'publicScores': [{'source':'TVmaze','value':row['rating']['average'],'max':10,'count':None,'url':row.get('url','')}] if (row.get('rating') or {}).get('average') is not None else [], 'description': html.unescape(re.sub(r'<[^>]*>', '', row.get('summary') or ''))})
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
    result = attach_scores({'items': unique, 'provider': provider})
    get_catalog().save(kind, query, result)
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
            return host in ('localhost', '127.0.0.1', '::1')
        except ValueError:
            return False

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
            if self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
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
                                      {'Set-Cookie': f'trace_session={new_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000'})
            session = store.session(token)
            if not session['user']:
                raise SocialError('Sign in to take part.', 401)
            csrf = self.headers.get('X-Trace-CSRF', '')
            if not csrf or not hmac.compare_digest(csrf, session['csrf']):
                raise SocialError('Refresh Trace and try again.', 403)
            user_id = session['user']['id']
            if path == '/api/auth/logout':
                store.logout(token)
                return self.send_json({'user': None}, headers={'Set-Cookie': 'trace_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'})
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
        if parsed.path.startswith(('/api/social/', '/api/auth/')):
            return self.social_get(parsed)
        if parsed.path == '/api/ratings':
            params = parse_qs(parsed.query)
            kind = params.get('type', [''])[0]
            title, year = params.get('title', [''])[0][:300], params.get('year', [''])[0][:4]
            if kind not in PROVIDERS or not title:
                return self.send_json({'error': 'Choose a catalog title.'}, 400)
            return self.send_json(title_scores(kind, title, year, params.get('imdbId', [''])[0]))
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
                    return self.send_json(saved)
                return self.send_json({'error': f'{PROVIDERS[kind]} is unavailable right now. Try again or add it manually.'}, 502)
        # Serve only app assets; never expose repository metadata or source/test files.
        clean = parsed.path
        if clean not in ('/', '/index.html', '/styles.css', '/app.js', '/core.mjs', '/data.js', '/views.mjs', '/social.js') and not re.fullmatch(r'/assets/[\w.-]+', clean):
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
            self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        super().end_headers()


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Trace is ready at http://localhost:{port}', flush=True)
    server.serve_forever()
