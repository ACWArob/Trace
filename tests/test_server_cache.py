import unittest
from unittest.mock import patch

import server


class ServerCacheTests(unittest.TestCase):
    @patch('server.fetch', return_value={'description': 'A book description.'})
    def test_cache_keeps_newest_entries_within_capacity(self, fetch):
        server.CACHE.clear()
        self.addCleanup(server.CACHE.clear)

        for number in range(4000):
            server.book_description(f'/works/OL{number}W')

        self.assertLessEqual(len(server.CACHE), 2000)
        self.assertNotIn(('book-description', '/works/OL0W'), server.CACHE)
        for number in range(3990, 4000):
            self.assertEqual(server.CACHE[('book-description', f'/works/OL{number}W')][1],
                             {'description': 'A book description.'})
