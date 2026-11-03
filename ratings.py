"""Public score adapters. IMDb's optional dataset stays local and is never bundled."""
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
import csv
import gzip
import io
import json
import os
import re
import sqlite3
import time
from catalog_store import database_path


class RatingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else database_path()
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS public_scores (
                source TEXT NOT NULL, external_id TEXT NOT NULL, value REAL NOT NULL,
                maximum REAL NOT NULL, votes INTEGER, url TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(source,external_id))''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def lookup(self, imdb_id):
        if not re.fullmatch(r'tt\d{5,12}', imdb_id or ''):
            return []
        with self.db() as db:
            rows = db.execute('SELECT source,value,maximum,votes,url FROM public_scores WHERE external_id=?', (imdb_id,)).fetchall()
        return [{'source': r[0], 'value': r[1], 'max': r[2], 'count': r[3], 'url': r[4]} for r in rows]

    def import_imdb(self, context=None):
        request = Request('https://datasets.imdbws.com/title.ratings.tsv.gz', headers={'User-Agent': 'TraceLocal/0.1 (personal non-commercial catalog)'})
        now, count = time.time(), 0
        with urlopen(request, timeout=60, context=context) as response:
            with gzip.GzipFile(fileobj=response) as zipped:
                reader = csv.DictReader(io.TextIOWrapper(zipped, encoding='utf-8'), delimiter='\t')
                batch = []
                with self.db() as db:
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
                            db.executemany('INSERT OR REPLACE INTO public_scores VALUES(?,?,?,?,?,?,?)', batch); batch.clear()
                    if batch:
                        db.executemany('INSERT OR REPLACE INTO public_scores VALUES(?,?,?,?,?,?,?)', batch)
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
            count = None; url = 'https://www.rottentomatoes.com/search?' + urlencode({'search': title})
        elif source == 'Metacritic':
            count = None; url = 'https://www.metacritic.com/search/' + quote(title) + '/'
        else:
            continue
        scores.append({'source': source, 'value': number, 'max': maximum, 'count': count, 'url': url, 'via': 'OMDb'})
    return scores


if __name__ == '__main__':
    import sys
    if sys.argv[1:] != ['--import-imdb']:
        raise SystemExit('Usage: python3 ratings.py --import-imdb (personal/non-commercial data only)')
    # Use the same verified system TLS context as catalog requests.
    from server import TLS_CONTEXT
    print('Importing the official IMDb ratings dataset for local non-commercial use…', flush=True)
    print(f'Imported {RatingsStore().import_imdb(TLS_CONTEXT):,} IMDb rating records.', flush=True)
