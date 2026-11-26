"""Public book catalog seed: only covered, attributed works, no repeats."""
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import seed_books  # noqa: E402


def doc(key, cover=1, author='Author'):
    return {'key': key, 'title': 'Title ' + key, 'author_name': [author] if author else [], 'cover_i': cover,
            'first_publish_year': 1990, 'subject': ['Fiction']}


class SeedBooksTests(unittest.TestCase):
    def test_keeps_covered_works_once_across_queries(self):
        urls = []

        def fetch(url):
            urls.append(url)
            return {'docs': [doc('/works/OL1W'), doc('/works/OL2W', cover=None), doc('/works/OL3W', author=''),
                             doc('/books/OL9M'), doc('OL4W')]}

        batches = [batch for _, _, batch in seed_books.collect(fetch, pages=1)]
        keys = [item['externalId'] for batch in batches for item in batch]
        self.assertEqual(keys, ['/works/OL1W', '/works/OL4W'])
        self.assertEqual(len(urls), len(seed_books.SUBJECTS) + len(seed_books.LANGUAGES))
        query = parse_qs(urlparse(urls[0]).query)
        self.assertEqual(urlparse(urls[0]).netloc, 'openlibrary.org')
        self.assertEqual((query['sort'], query['limit']), (['readinglog'], ['100']))
        first = batches[0][0]
        self.assertEqual((first['type'], first['source']), ('book', 'Open Library'))
        self.assertTrue(first['image'].startswith('https://covers.openlibrary.org/b/id/'))

    def test_bad_responses_are_skipped(self):
        batches = [batch for _, _, batch in seed_books.collect(lambda url: {}, pages=1)]
        self.assertFalse(any(batches))


if __name__ == '__main__':
    unittest.main()
