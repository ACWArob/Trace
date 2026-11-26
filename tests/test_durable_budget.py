"""DurableBudget uses a throwaway Postgres schema; no real data is touched."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import unittest

from durable_budget import DurableBudget
from tests.pg_support import PostgresTestCase


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class DurableBudgetTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.budget = DurableBudget(self.database, clock=self.clock)

    def test_the_sixteenth_attempt_in_one_window_is_denied(self):
        results = [self.budget.allow('203.0.113.7', 15) for _ in range(16)]
        self.assertEqual(results, [True] * 15 + [False])
        self.assertFalse(self.budget.allow('203.0.113.7', 15))

    def test_the_next_window_is_allowed_again(self):
        for _ in range(16):
            self.budget.allow('203.0.113.7', 15)
        self.clock.now += 60
        self.assertEqual([self.budget.allow('203.0.113.7', 15) for _ in range(16)], [True] * 15 + [False])

    def test_window_is_fixed_not_sliding(self):
        self.clock.now = 1_800_000_000.0 - 1_800_000_000.0 % 60 + 59   # last second of a window
        for _ in range(15):
            self.assertTrue(self.budget.allow('a', 15))
        self.assertFalse(self.budget.allow('a', 15))
        self.clock.now += 1                                           # first second of the next
        self.assertTrue(self.budget.allow('a', 15))

    def test_keys_are_independent_and_limits_and_windows_are_per_call(self):
        self.assertTrue(self.budget.allow('a', 1))
        self.assertFalse(self.budget.allow('a', 1))
        self.assertTrue(self.budget.allow('b', 1))
        self.assertTrue(self.budget.allow('c', 2, seconds=10))
        self.assertTrue(self.budget.allow('c', 2, seconds=10))
        self.assertFalse(self.budget.allow('c', 2, seconds=10))
        self.clock.now += 10
        self.assertTrue(self.budget.allow('c', 2, seconds=10))

    def test_keys_are_stored_hashed_never_raw(self):
        self.budget.allow('203.0.113.7', 15)
        self.budget.allow('2001:db8:1:2::/64', 15)
        with self.database.connect() as db:
            stored = sorted(row[0] for row in db.execute('SELECT key FROM auth_budget'))
        expected = sorted(hashlib.sha256(key.encode()).hexdigest() for key in ('203.0.113.7', '2001:db8:1:2::/64'))
        self.assertEqual(stored, expected)
        self.assertTrue(all(len(key) == 64 and '.' not in key and ':' not in key for key in stored))

    def test_state_is_shared_between_instances(self):
        other = DurableBudget(self.database, clock=self.clock)
        for _ in range(15):
            self.budget.allow('shared', 15)
        self.assertFalse(other.allow('shared', 15))

    def test_concurrent_attempts_never_exceed_the_limit(self):
        def attempt(_):
            return DurableBudget(self.database, clock=self.clock).allow('burst', 15)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(attempt, range(40)))
        self.assertEqual(results.count(True), 15)
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT hits FROM auth_budget').fetchone()[0], 40)

    def test_an_older_instance_clock_does_not_reopen_the_window(self):
        for _ in range(16):
            self.budget.allow('skew', 15)
        behind = DurableBudget(self.database, clock=Clock(self.clock.now - 120))
        self.assertFalse(behind.allow('skew', 15))

    def test_purge_removes_only_old_windows_and_clear_empties_the_table(self):
        self.budget.allow('old', 5)
        self.clock.now += 7200
        self.budget.allow('new', 5)
        self.assertEqual(self.budget.purge(older_than=3600), 1)
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auth_budget').fetchone()[0], 1)
        self.budget.clear()
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auth_budget').fetchone()[0], 0)

    def test_invalid_window_is_rejected(self):
        for seconds in (0, -5):
            with self.assertRaises(ValueError):
                self.budget.allow('a', 1, seconds=seconds)


if __name__ == '__main__':
    unittest.main()
