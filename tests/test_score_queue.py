"""ScoreQueue uses a throwaway Postgres schema; no real data is touched."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from score_queue import ScoreQueue
from tests.pg_support import PostgresTestCase


class ScoreQueueTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.queue = ScoreQueue(self.database)

    def keys(self):
        with self.database.connect() as db:
            return [tuple(row) for row in db.execute('SELECT provider,kind,external_id FROM score_refresh_queue ORDER BY queued_at,external_id')]

    def test_enqueue_is_idempotent_and_keeps_the_original_place(self):
        self.assertTrue(self.queue.enqueue('mdblist', 'movie', 'tt1234567', now=10))
        self.assertFalse(self.queue.enqueue('mdblist', 'movie', 'tt1234567', now=99))
        self.assertTrue(self.queue.enqueue('omdb', 'movie', 'tt1234567', now=20))
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT queued_at FROM score_refresh_queue WHERE provider=?', ('mdblist',)).fetchone()[0], 10)
        self.assertEqual(self.queue.size(), 2)

    def test_invalid_keys_are_rejected(self):
        for args in (('BAD', 'movie', 'tt1234567'), ('mdblist', '', 'tt1234567'), ('mdblist', 'movie', ''), ('mdblist', 'movie', 'x' * 129), (None, 'movie', 'tt1234567')):
            with self.assertRaises(ValueError):
                self.queue.enqueue(*args)

    def test_a_full_queue_drops_new_keys(self):
        small = ScoreQueue(self.database, max_rows=2)
        self.assertTrue(small.enqueue('mdblist', 'movie', 'tt0000001'))
        self.assertTrue(small.enqueue('mdblist', 'movie', 'tt0000002'))
        self.assertFalse(small.enqueue('mdblist', 'movie', 'tt0000003'))
        self.assertEqual(small.size(), 2)

    def test_titles_are_carried_to_the_loader_and_kept_on_retry(self):
        self.assertTrue(self.queue.enqueue('omdb', 'movie', 'tt0000001', title='Nedjma', now=1))
        self.queue.enqueue('mdblist', 'tv', 'tt0000002', now=2)
        seen = []
        def loader(provider, kind, external_id, title):
            seen.append((external_id, title))
            raise RuntimeError('provider down')
        self.queue.drain(5, loader, now=50)
        self.assertEqual(seen, [('tt0000001', 'Nedjma'), ('tt0000002', None)])
        seen.clear()
        self.queue.drain(5, lambda *row: seen.append(row))
        self.assertEqual(sorted(seen), [('mdblist', 'tv', 'tt0000002', None), ('omdb', 'movie', 'tt0000001', 'Nedjma')])

    def test_titles_are_bounded_text(self):
        for title in [5, 'x' * 501]:
            with self.assertRaises(ValueError):
                self.queue.enqueue('omdb', 'movie', 'tt0000001', title=title)

    def test_drain_takes_the_oldest_rows_first_and_only_up_to_the_limit(self):
        for index in range(5):
            self.queue.enqueue('mdblist', 'movie', f'tt000000{index}', now=100 - index)
        seen = []
        result = self.queue.drain(3, lambda *key: seen.append(key[2]))
        self.assertEqual(seen, ['tt0000004', 'tt0000003', 'tt0000002'])
        self.assertEqual(result, {'claimed': 3, 'done': 3, 'failed': 0})
        self.assertEqual(self.keys(), [('mdblist', 'movie', 'tt0000001'), ('mdblist', 'movie', 'tt0000000')])
        self.assertEqual(self.queue.size(), 2)
        self.assertEqual(self.queue.drain(10, lambda *key: None)['claimed'], 2)
        self.assertEqual(self.queue.drain(10, lambda *key: None), {'claimed': 0, 'done': 0, 'failed': 0})

    def test_failed_rows_are_requeued_at_the_back_and_others_continue(self):
        self.queue.enqueue('mdblist', 'movie', 'tt0000001', now=1)
        self.queue.enqueue('mdblist', 'movie', 'tt0000002', now=2)
        def loader(provider, kind, external_id, title):
            if external_id == 'tt0000001':
                raise RuntimeError('provider down')
        result = self.queue.drain(5, loader, now=500)
        self.assertEqual(result, {'claimed': 2, 'done': 1, 'failed': 1})
        with self.database.connect() as db:
            rows = db.execute('SELECT external_id,queued_at FROM score_refresh_queue').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('tt0000001', 500)])

    def test_the_loader_runs_without_holding_a_transaction_or_row_lock(self):
        self.queue.enqueue('mdblist', 'movie', 'tt0000001', now=1)
        def loader(provider, kind, external_id, title):
            # A competing write to the same key must not block while we "load".
            self.assertTrue(ScoreQueue(self.database).enqueue(provider, kind, external_id, now=5))
        self.queue.drain(1, loader)
        self.assertEqual(self.queue.size(), 1)

    def test_concurrent_drains_never_process_a_row_twice(self):
        for index in range(40):
            self.queue.enqueue('mdblist', 'movie', f'tt{index:07d}', now=index)
        barrier, seen = Barrier(4), []
        def drain(_):
            barrier.wait(timeout=5)
            return ScoreQueue(self.database).drain(10, lambda *key: seen.append(key[2]))
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(drain, range(4)))
        self.assertEqual(sum(r['claimed'] for r in results), 40)
        self.assertEqual(len(seen), len(set(seen)), 'a row was claimed twice')
        self.assertEqual(self.queue.size(), 0)

    def test_invalid_limits_are_rejected(self):
        for limit in (0, -1, 1001, True, '5', None):
            with self.assertRaises(ValueError):
                self.queue.drain(limit, lambda *key: None)


if __name__ == '__main__':
    unittest.main()
