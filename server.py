"""Trace's local server: static UI + small, cached public-catalog adapters."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path
import json
import re
import sys
import time
import threading
import ssl

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
        return CACHE[cache_key][1]
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
            if titles:
                try:
                    pages = fetch('https://en.wikipedia.org/w/api.php?' + urlencode({
                        'action': 'query', 'format': 'json', 'titles': '|'.join(titles), 'prop': 'pageimages',
                        'piprop': 'thumbnail', 'pithumbsize': 500, 'pilicense': 'any'}), 'wikipedia').get('query', {}).get('pages', {})
                    images = {x['title']: x.get('thumbnail', {}).get('source', '') for x in pages.values()}
                except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                    pass  # A cover outage must not prevent logging a found film.
            for hit in hits:
                entity = entities.get(hit['id'], {})
                wiki_title = entity.get('sitelinks', {}).get('enwiki', {}).get('title', '')
                description = hit.get('description', '')
                year_match = re.search(r'\b(18|19|20)\d{2}\b', description)
                director = re.search(r'(?:directed by|film by) (.+)$', description)
                items.append({'title': hit.get('label', ''), 'creator': director.group(1) if director else '',
                              'year': year_match.group(0) if year_match else '', 'type': 'movie',
                              'image': images.get(wiki_title, ''), 'source': provider,
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
                          'externalId': str(row.get('collectionId', ''))})
    elif kind == 'tv':
        data = fetch('https://api.tvmaze.com/search/shows?' + urlencode({'q': query}), 'tvmaze')
        for result in data[:16]:
            row = result['show']
            items.append({'title': row['name'], 'creator': (row.get('network') or row.get('webChannel') or {}).get('name', ''),
                          'year': (row.get('premiered') or '')[:4], 'type': kind,
                          'image': (row.get('image') or {}).get('medium', ''), 'source': provider,
                          'sourceUrl': row.get('url', ''), 'externalId': str(row['id'])})
    elif kind == 'anime':
        data = fetch('https://kitsu.io/api/edge/anime?' + urlencode({'filter[text]': query, 'page[limit]': 16}), 'kitsu')
        for row in data.get('data', []):
            attr = row.get('attributes', {})
            items.append({'title': attr.get('titles', {}).get('en') or attr.get('canonicalTitle', ''),
                          'creator': '', 'year': (attr.get('startDate') or '')[:4], 'type': kind,
                          'image': (attr.get('posterImage') or {}).get('medium', ''),
                          'source': provider, 'sourceUrl': 'https://kitsu.app/anime/' + str(row['id']),
                          'externalId': str(row['id'])})
    elif kind == 'book':
        data = fetch('https://openlibrary.org/search.json?' + urlencode({'q': query, 'limit': 16,
                     'fields': 'key,title,author_name,first_publish_year,cover_i'}), 'openlibrary')
        for row in data.get('docs', []):
            items.append({'title': row['title'], 'creator': ', '.join(row.get('author_name', [])[:2]),
                          'year': str(row.get('first_publish_year', '')), 'type': kind,
                          'image': f"https://covers.openlibrary.org/b/id/{row['cover_i']}-L.jpg" if row.get('cover_i') else '',
                          'source': provider, 'sourceUrl': 'https://openlibrary.org' + row['key'], 'externalId': row['key']})
    unique = list({(r['type'], r['externalId']): r for r in items}.values())
    result = {'items': unique, 'provider': provider}
    CACHE[cache_key] = (time.time(), result)
    return result


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/search':
            params = parse_qs(parsed.query)
            kind = params.get('type', ['book'])[0]
            query = params.get('q', [''])[0].strip()
            if kind not in PROVIDERS or not 2 <= len(query) <= 200:
                return self.send_json({'error': 'Choose a category and enter 2–200 characters.'}, 400)
            try:
                return self.send_json(search(kind, query))
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, OSError):
                return self.send_json({'error': f'{PROVIDERS[kind]} is unavailable right now. Try again or add it manually.'}, 502)
        # Serve only app assets; never expose repository metadata or source/test files.
        clean = parsed.path
        if clean not in ('/', '/index.html', '/styles.css', '/app.js', '/core.mjs', '/data.js') and not re.fullmatch(r'/assets/[\w.-]+', clean):
            return self.send_error(404)
        return super().do_GET()

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
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
