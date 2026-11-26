"""Fill the public movie catalog with notable films and their Wikipedia posters.

For each release year, Wikidata lists films with an IMDb ID, an English Wikipedia
article and enough sitelinks to be notable; the poster is that exact article's
lead image (matched by Wikidata ID, never by title). Saved through CatalogStore,
the same path live searches use. No account or library table is touched.

    TRACE_DATABASE_URL=... python3 scripts/seed_movies.py [--from 1900] [--to 2026] [--min-links 5]
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from movie_catalog import WIKIDATA_SOURCE  # noqa: E402

UA = {'User-Agent': 'TraceCatalog/1.0 (media diary; public movie catalog seed)'}
IMAGE_PREFIX = 'https://upload.wikimedia.org/'


def year_query(year, min_links):
    return f"""SELECT ?f ?fLabel ?imdb ?enwiki (MIN(YEAR(?d)) AS ?year)
      (GROUP_CONCAT(DISTINCT ?dirLabel; separator=", ") AS ?dirs) WHERE {{
      ?f wdt:P31 wd:Q11424 ; wdt:P577 ?d ; wdt:P345 ?imdb ; wikibase:sitelinks ?links .
      FILTER(YEAR(?d) = {int(year)}) FILTER(?links >= {int(min_links)})
      ?enwiki schema:about ?f ; schema:isPartOf <https://en.wikipedia.org/> .
      OPTIONAL {{ ?f wdt:P57 ?dir . ?dir rdfs:label ?dirLabel . FILTER(LANG(?dirLabel) = "en") }}
      ?f rdfs:label ?fLabel . FILTER(LANG(?fLabel) = "en")
    }} GROUP BY ?f ?fLabel ?imdb ?enwiki"""


def films(rows):
    """Wikidata bindings -> {qid: film}; the earliest release year wins a repeat."""
    found = {}
    for row in rows:
        qid = row.get('f', {}).get('value', '').rsplit('/', 1)[-1]
        imdb = row.get('imdb', {}).get('value', '')
        article = row.get('enwiki', {}).get('value', '')
        title = row.get('fLabel', {}).get('value', '').strip()
        if not (re.fullmatch(r'Q[1-9]\d*', qid) and re.fullmatch(r'tt\d{5,12}', imdb) and title
                and article.startswith('https://en.wikipedia.org/wiki/')):
            continue
        found.setdefault(qid, {'qid': qid, 'title': title[:500], 'imdb': imdb, 'year': row.get('year', {}).get('value', '')[:4],
                               'creator': row.get('dirs', {}).get('value', '')[:500],
                               'article': unquote(article.rsplit('/wiki/', 1)[1]).replace('_', ' ')})
    return found


def posters(pages_response, batch):
    """pageimages response -> {requested article title: poster URL}."""
    query = pages_response.get('query', {}) if isinstance(pages_response, dict) else {}
    rename = {row['from']: row['to'] for row in query.get('normalized', []) + query.get('redirects', [])
              if isinstance(row, dict) and 'from' in row and 'to' in row}
    by_title = {}
    for page in (query.get('pages') or {}).values():
        source = (page.get('thumbnail') or {}).get('source', '') if isinstance(page, dict) else ''
        if isinstance(source, str) and source.startswith(IMAGE_PREFIX):
            by_title[page.get('title')] = source
    result = {}
    for title in batch:
        target = rename.get(title, title)
        target = rename.get(target, target)
        if by_title.get(target):
            result[title] = by_title[target]
    return result


def item(film, image):
    return {'title': film['title'], 'creator': film['creator'], 'year': film['year'], 'type': 'movie', 'genres': [],
            'imdbId': film['imdb'], 'image': image, 'description': '', 'source': WIKIDATA_SOURCE,
            'sourceUrl': 'https://www.wikidata.org/wiki/' + film['qid'], 'externalId': film['qid']}


def year_items(year, fetch, min_links):
    data = fetch('https://query.wikidata.org/sparql', urlencode({'query': year_query(year, min_links)}).encode())
    found = films(data.get('results', {}).get('bindings', []) if isinstance(data, dict) else [])
    by_article = {film['article']: film for film in found.values()}
    articles, items = sorted(by_article), []
    for start in range(0, len(articles), 50):
        batch = articles[start:start + 50]
        response = fetch('https://en.wikipedia.org/w/api.php?' + urlencode({
            'action': 'query', 'format': 'json', 'redirects': 1, 'prop': 'pageimages', 'piprop': 'thumbnail',
            'pithumbsize': 500, 'pilicense': 'any', 'titles': '|'.join(batch)}))
        for article, image in posters(response, batch).items():
            items.append(item(by_article[article], image))
    return items


def http_fetch(context):
    def fetch(url, data=None):
        headers = {**UA, 'Accept': 'application/sparql-results+json' if data else 'application/json'}
        if data:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        for attempt in range(5):
            try:
                with urlopen(Request(url, data=data, headers=headers), timeout=90, context=context) as response:
                    return json.loads(response.read(30_000_000))
            except Exception as error:  # Timeouts and 429s: back off, then give up on this request.
                print('retry', type(error).__name__, getattr(error, 'code', ''), flush=True)
                wait = getattr(error, 'headers', None) and error.headers.get('Retry-After')
                time.sleep(int(wait) if wait and wait.isdigit() else 30 * (attempt + 1))
            finally:
                time.sleep(1)  # One polite request a second.
        return {}
    return fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--from', dest='first', type=int, default=1900)
    parser.add_argument('--to', dest='last', type=int, default=2026)
    parser.add_argument('--min-links', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    import server
    from catalog_store import CatalogStore
    store = None if args.dry_run else CatalogStore(server.get_database())
    fetch = http_fetch(server.create_tls_context())
    total = 0
    with ThreadPoolExecutor(max_workers=1) as pool:  # Wikimedia rate-limits bursts.
        jobs = {year: pool.submit(year_items, year, fetch, args.min_links) for year in range(args.last, args.first - 1, -1)}
        for year, job in jobs.items():
            items = job.result()
            if store and items:
                for start in range(0, len(items), 250):
                    store.save('movie', None, {'items': items[start:start + 250]})
            total += len(items)
            print(f'{year}: +{len(items)} (total {total})', flush=True)
    print(json.dumps({'saved' if store else 'found': total}), flush=True)


if __name__ == '__main__':
    main()
