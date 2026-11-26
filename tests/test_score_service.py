from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from urllib.error import HTTPError, URLError
from unittest.mock import Mock
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ratings import RatingsStore
from score_queue import ScoreQueue
from score_service import ScoreService
from tests.pg_support import PostgresTestCase


SCORES = [{'source': 'Letterboxd', 'value': 4.1, 'max': 5}]


class ScoreServiceTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.store = RatingsStore(self.database)
        self.queue = ScoreQueue(self.database)

    def service(self, **kwargs):
        return ScoreService(RatingsStore(self.database), **kwargs)

    @staticmethod
    def providers(loader, enabled=True):
        return {'mdblist': {'enabled': enabled, 'load': loader}}

    def seed_stale(self, imdb_id='tt1234567'):
        self.store.save_provider_cache('mdblist', 'movie', imdb_id, SCORES, now=time.time()-100, refresh_after=time.time()-50)

    def queued(self):
        with self.database.connect() as db:
            return [tuple(row) for row in db.execute('SELECT provider,kind,external_id FROM score_refresh_queue ORDER BY external_id')]

    def test_success_and_negative_results_survive_service_restart(self):
        loader = Mock(return_value=SCORES)
        result = self.service().lookup('movie', 'tt1234567', self.providers(loader))
        forbidden = Mock(side_effect=AssertionError('Cached score must not load'))
        again = self.service().lookup('movie', 'tt1234567', self.providers(forbidden))
        self.assertEqual(result, again)
        self.assertEqual(result['providerStatus'], {'mdblist': 'available'})
        self.assertFalse(result['providerMeta']['mdblist']['stale'])
        loader.assert_called_once_with()
        forbidden.assert_not_called()
        empty = Mock(return_value=[])
        first_empty = self.service().lookup('movie', 'tt7654321', self.providers(empty))
        again_empty = self.service().lookup('movie', 'tt7654321', self.providers(forbidden))
        self.assertEqual(first_empty, again_empty)
        self.assertEqual(first_empty['sources'], [])
        empty.assert_called_once_with()
        forbidden.assert_not_called()
        self.assertEqual(self.queued(), [])

    def test_no_threads_are_started_for_loads_or_stale_refreshes(self):
        self.seed_stale('tt7654321')
        before = {thread.ident for thread in threading.enumerate()}
        loader = Mock(return_value=SCORES)
        service = self.service()
        service.lookup('movie', 'tt1234567', self.providers(loader))
        service.lookup('movie', 'tt7654321', self.providers(loader))
        self.assertEqual({thread.ident for thread in threading.enumerate()}, before)
        self.assertEqual(loader.call_count, 1)

    def test_cold_load_runs_in_the_caller_thread(self):
        threads = []
        def load():
            threads.append(threading.current_thread())
            return SCORES
        self.service().lookup('movie', 'tt1234567', self.providers(load))
        self.assertEqual(threads, [threading.current_thread()])

    def test_stale_scores_return_immediately_and_are_queued_for_the_cron(self):
        self.seed_stale()
        forbidden = Mock(side_effect=AssertionError('A stale read must not load'))
        service = self.service()
        for _ in range(2):  # queueing twice keeps a single row
            result = service.lookup('movie', 'tt1234567', self.providers(forbidden))
        self.assertEqual(result['sources'], SCORES)
        self.assertTrue(result['providerMeta']['mdblist']['stale'])
        self.assertEqual(result['providerStatus']['mdblist'], 'available')
        self.assertEqual(self.queued(), [('mdblist', 'movie', 'tt1234567')])
        forbidden.assert_not_called()

    def test_cron_drain_refreshes_queued_scores(self):
        self.seed_stale()
        service = self.service()
        service.lookup('movie', 'tt1234567', self.providers(Mock()), title='A Film')
        seen = []
        def loader_for(provider, kind, imdb_id, title):
            seen.append((provider, kind, imdb_id, title))
            return lambda: [{'source': 'Letterboxd', 'value': 4.3, 'max': 5}]
        self.assertEqual(service.refresh_queued(25, loader_for), {'claimed': 1, 'done': 1, 'failed': 0})
        self.assertEqual(seen, [('mdblist', 'movie', 'tt1234567', 'A Film')])
        self.assertEqual(self.queued(), [])
        refreshed = self.service().lookup('movie', 'tt1234567', self.providers(Mock()))
        self.assertEqual(refreshed['sources'][0]['value'], 4.3)
        self.assertFalse(refreshed['providerMeta']['mdblist']['stale'])

    def test_cron_skips_keys_another_request_already_refreshed(self):
        self.seed_stale()
        service = self.service()
        service.lookup('movie', 'tt1234567', self.providers(Mock()))
        self.store.save_provider_cache('mdblist', 'movie', 'tt1234567', SCORES)
        forbidden = Mock(side_effect=AssertionError('A fresh row must not load'))
        result = service.refresh_queued(25, lambda *key: forbidden)
        self.assertEqual((result['done'], result['failed']), (1, 0))
        forbidden.assert_not_called()

    def test_cron_drops_unconfigured_providers_and_requeues_failures(self):
        self.seed_stale()
        self.seed_stale('tt7654321')
        service = self.service()
        for imdb_id in ('tt1234567', 'tt7654321'):
            service.lookup('movie', imdb_id, self.providers(Mock()))
        self.assertEqual(service.refresh_queued(25, lambda *key: None), {'claimed': 2, 'done': 2, 'failed': 0})
        self.assertEqual(self.queued(), [])
        service.lookup('movie', 'tt1234567', self.providers(Mock()))
        result = service.refresh_queued(25, lambda *key: Mock(side_effect=URLError('private failure detail')))
        self.assertEqual(result, {'claimed': 1, 'done': 0, 'failed': 1})
        self.assertEqual(self.queued(), [('mdblist', 'movie', 'tt1234567')])
        self.assertEqual(self.store.get_provider_backoff('mdblist')['status'], 'unavailable')

    def test_failed_refresh_preserves_stale_scores_and_backoff_after_restart(self):
        self.seed_stale()
        original = self.store.read_provider_cache('mdblist', 'movie', 'tt1234567')
        service = self.service()
        service.lookup('movie', 'tt1234567', self.providers(Mock()))
        service.refresh_queued(25, lambda *key: Mock(side_effect=URLError('private failure detail')))
        forbidden = Mock(side_effect=AssertionError('Active cooldown must not load'))
        result = self.service().lookup('movie', 'tt1234567', self.providers(forbidden))
        self.assertEqual(result['sources'], SCORES)
        self.assertEqual(result['providerStatus']['mdblist'], 'unavailable')
        self.assertTrue(result['providerMeta']['mdblist']['stale'])
        self.assertEqual(result['providerMeta']['mdblist']['fetchedAt'], original['fetchedAt'])
        self.assertEqual(self.store.read_provider_cache('mdblist', 'movie', 'tt1234567'), original)
        self.assertNotIn('private', str(result))
        forbidden.assert_not_called()

    def test_cold_failure_sets_a_cooldown_and_never_leaks_details(self):
        loader = Mock(side_effect=URLError('private failure detail'))
        first = self.service().lookup('movie', 'tt1234567', self.providers(loader))
        second = self.service().lookup('movie', 'tt7654321', self.providers(loader))
        self.assertEqual(first['providerStatus']['mdblist'], 'unavailable')
        self.assertEqual(second['providerStatus']['mdblist'], 'unavailable')
        self.assertEqual(first['sources'], [])
        self.assertNotIn('private', str(first))
        loader.assert_called_once_with()

    def test_rate_limit_backoff_survives_restart_and_applies_to_other_titles(self):
        loader = Mock(side_effect=HTTPError('https://example.invalid/?apikey=private', 429, 'private', {'Retry-After': '600'}, None))
        first = self.service().lookup('movie', 'tt1234567', self.providers(loader))
        second = self.service().lookup('movie', 'tt7654321', self.providers(loader))
        self.assertEqual(first['providerStatus']['mdblist'], 'rate_limited')
        self.assertEqual(second['providerStatus']['mdblist'], 'rate_limited')
        self.assertGreater(second['providerMeta']['mdblist']['refreshAt'], time.time()+590)
        self.assertNotIn('private', str(second))
        loader.assert_called_once_with()

    def test_disabled_provider_displays_saved_scores_without_refresh(self):
        self.seed_stale()
        forbidden = Mock()
        result = self.service().lookup('movie', 'tt1234567', self.providers(forbidden, False))
        self.assertEqual(result['sources'], SCORES)
        self.assertTrue(result['providerMeta']['mdblist']['stale'])
        self.assertEqual(result['providerStatus']['mdblist'], 'available')
        missing = self.service().lookup('movie', 'tt7654321', self.providers(forbidden, False))
        self.assertEqual(missing['providerStatus']['mdblist'], 'not_configured')
        forbidden.assert_not_called()
        self.assertEqual(self.queued(), [])

    def test_simultaneous_cold_requests_share_one_load(self):
        entered, release = Event(), Event()
        def load():
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError()
            return SCORES
        loader = Mock(side_effect=load)
        service = self.service()
        with ThreadPoolExecutor(max_workers=2) as callers:
            try:
                first = callers.submit(service.lookup, 'movie', 'tt1234567', self.providers(loader))
                self.assertTrue(entered.wait(timeout=2))
                second = callers.submit(service.lookup, 'movie', 'tt1234567', self.providers(loader))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3)['sources'], second.result(timeout=3)['sources'])
        loader.assert_called_once_with()
        self.assertEqual(service._key_locks, {})

    def test_distinct_titles_can_load_concurrently(self):
        barrier = Barrier(2)
        def load():
            barrier.wait(timeout=2)
            return SCORES
        service = self.service()
        with ThreadPoolExecutor(max_workers=2) as callers:
            pending = [callers.submit(service.lookup, 'movie', imdb_id, self.providers(load)) for imdb_id in ('tt1234567', 'tt7654321')]
            for future in pending:
                self.assertEqual(future.result(timeout=3)['providerStatus']['mdblist'], 'available')

    def test_a_lookup_stops_loading_after_its_time_budget_and_queues_the_rest(self):
        now = [0.0]
        def slow():
            now[0] += 11
            return SCORES
        service = self.service(clock=lambda: now[0])
        third = Mock()
        providers = {'omdb': {'enabled': True, 'load': slow}, 'mdblist': {'enabled': True, 'load': third}}
        result = service.lookup('movie', 'tt1234567', providers)
        self.assertEqual(result['providerStatus'], {'omdb': 'available', 'mdblist': 'unavailable'})
        third.assert_not_called()
        self.assertEqual(self.queued(), [('mdblist', 'movie', 'tt1234567')])

    def test_a_caller_waiting_past_the_budget_gives_up_without_loading(self):
        entered, release = Event(), Event()
        def slow():
            entered.set()
            release.wait(timeout=3)
            return SCORES
        service = self.service(load_budget=0.05)
        second = Mock()
        with ThreadPoolExecutor(max_workers=1) as caller:
            first = caller.submit(service.lookup, 'movie', 'tt1234567', self.providers(slow))
            try:
                self.assertTrue(entered.wait(timeout=2))
                result = service.lookup('movie', 'tt1234567', self.providers(second))
            finally:
                release.set()
            first.result(timeout=3)
        self.assertEqual(result['providerStatus']['mdblist'], 'unavailable')
        second.assert_not_called()

    def test_invalid_titles_and_keys_never_leak_error_details(self):
        loader = Mock(side_effect=HTTPError('https://example.invalid/private', 401, 'private', {}, None))
        service = self.service()
        invalid = service.lookup('movie', '../private', self.providers(loader))
        self.assertEqual(invalid['providerStatus']['mdblist'], 'unmatched')
        loader.assert_not_called()
        result = service.lookup('movie', 'tt1234567', self.providers(loader))
        self.assertEqual(result['providerStatus']['mdblist'], 'invalid_key')
        self.assertNotIn('private', str(result))


if __name__ == '__main__':
    unittest.main()
