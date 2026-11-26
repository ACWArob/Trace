"""Public movie catalog seed: exact-ID posters only, valid identifiers only."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import seed_movies  # noqa: E402


def binding(qid, title, imdb='tt0133093', article='The_Matrix', year='1999', dirs='Lana Wachowski'):
    return {'f': {'value': 'http://www.wikidata.org/entity/' + qid}, 'fLabel': {'value': title},
            'imdb': {'value': imdb}, 'enwiki': {'value': 'https://en.wikipedia.org/wiki/' + article},
            'year': {'value': year}, 'dirs': {'value': dirs}}


class SeedMoviesTests(unittest.TestCase):
    def test_films_require_ids_and_an_english_article(self):
        found = seed_movies.films([binding('Q83495', 'The Matrix'), binding('bad', 'X'),
                                   binding('Q2', 'No IMDb', imdb=''), binding('Q3', '', article='Y')])
        self.assertEqual(list(found), ['Q83495'])
        self.assertEqual(found['Q83495']['article'], 'The Matrix')

    def test_year_items_use_the_exact_article_poster(self):
        calls = []

        def fetch(url, data=None):
            calls.append(url)
            if data:
                return {'results': {'bindings': [binding('Q83495', 'The Matrix'),
                                                 binding('Q5', 'Stills Only', imdb='tt0000005', article='Stills_Only')]}}
            return {'query': {'pages': {
                '1': {'title': 'The Matrix', 'thumbnail': {'source': 'https://upload.wikimedia.org/wikipedia/en/c/c1/The_Matrix_Poster.jpg'}},
                '2': {'title': 'Stills Only', 'thumbnail': {'source': 'https://evil.example/x.jpg'}}}}}

        items = seed_movies.year_items(1999, fetch, 5)
        self.assertEqual([(i['externalId'], i['imdbId'], i['type'], i['source']) for i in items],
                         [('Q83495', 'tt0133093', 'movie', 'Wikidata / Wikipedia')])
        self.assertTrue(items[0]['image'].startswith('https://upload.wikimedia.org/'))
        self.assertEqual(calls[0], 'https://query.wikidata.org/sparql')

    def test_failed_requests_yield_nothing(self):
        self.assertEqual(seed_movies.year_items(1999, lambda url, data=None: {}, 5), [])


if __name__ == '__main__':
    unittest.main()
