"""Fill the public book catalog with widely read Open Library works, covers included.

Searches Open Library by subject and original language, most-read first, and saves
each work with a cover through CatalogStore (the same path live searches use).
Only public catalog data is written; no account or library table is touched.

    TRACE_DATABASE_URL=... python3 scripts/seed_books.py [--pages 2] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from book_catalog import WORK_FIELDS, work_item  # noqa: E402

SUBJECTS = [
    'fiction', 'literary fiction', 'classics', 'fantasy', 'science fiction', 'mystery', 'thriller', 'crime',
    'horror', 'romance', 'historical fiction', 'young adult fiction', 'graphic novels', 'manga', 'comics',
    'poetry', 'drama', 'short stories', 'humor', 'adventure', 'dystopias', 'magical realism', 'gothic fiction',
    'biography', 'autobiography', 'memoir', 'history', 'philosophy', 'psychology', 'self-help', 'essays',
    'science', 'popular science', 'mathematics', 'economics', 'business', 'politics', 'sociology',
    'feminism', 'religion', 'art', 'music', 'film', 'photography', 'architecture', 'design', 'cooking',
    'travel', 'nature', 'true crime', 'war', 'children\'s fiction', 'picture books', 'mythology',
    'african literature', 'arabic literature', 'japanese literature', 'french literature', 'russian literature',
    'latin american literature', 'korean literature', 'chinese literature', 'indian literature',
    'german literature', 'italian literature', 'spanish literature', 'persian literature', 'turkish literature',
]
LANGUAGES = ['fre', 'ara', 'spa', 'jpn', 'kor', 'ger', 'ita', 'por', 'rus', 'chi', 'tur', 'per', 'hin', 'urd', 'pol', 'swe']
UA = {'User-Agent': 'TraceCatalog/1.0 (media diary; public book catalog seed)', 'Accept': 'application/json'}


def queries(pages):
    for subject in SUBJECTS:
        for page in range(1, pages + 1):
            yield f'subject:"{subject}"', page
    for language in LANGUAGES:
        for page in range(1, pages + 1):
            yield f'language:{language}', page


def search_url(query, page):
    return 'https://openlibrary.org/search.json?' + urlencode({
        'q': query, 'sort': 'readinglog', 'limit': 100, 'page': page, 'fields': WORK_FIELDS})


def collect(fetch, pages, seen=None):
    """Yield new catalog items (with covers) for every query, skipping repeats."""
    seen = set() if seen is None else seen
    for query, page in queries(pages):
        data = fetch(search_url(query, page))
        docs = data.get('docs') if isinstance(data, dict) else None
        batch = []
        for item in map(work_item, docs if isinstance(docs, list) else []):
            if item and item['image'] and item['creator'] and item['externalId'] not in seen:
                seen.add(item['externalId'])
                batch.append(item)
        yield query, page, batch


def http_fetch(context):
    def fetch(url):
        for attempt in range(4):
            try:
                with urlopen(Request(url, headers=UA), timeout=60, context=context) as response:
                    return json.loads(response.read(8_000_000))
            except Exception as error:  # Network blips and 429s: back off, then skip.
                print('retry', type(error).__name__, getattr(error, 'code', ''), flush=True)
                time.sleep(5 * (attempt + 1))
        return {}
    return fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pages', type=int, default=2, help='pages of 100 per subject or language')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    import server
    from catalog_store import CatalogStore
    store = None if args.dry_run else CatalogStore(server.get_database())
    total = 0
    for query, page, batch in collect(http_fetch(server.create_tls_context()), args.pages):
        if store and batch:
            for start in range(0, len(batch), 250):
                store.save('book', None, {'items': batch[start:start + 250]})
        total += len(batch)
        print(f'{query} p{page}: +{len(batch)} (total {total})', flush=True)
        time.sleep(1)
    print(json.dumps({'saved' if store else 'found': total}), flush=True)


if __name__ == '__main__':
    main()
