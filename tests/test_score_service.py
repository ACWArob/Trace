from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from urllib.error import HTTPError, URLError
from unittest.mock import Mock
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ratings import RatingsStore
from score_service import ScoreService


SCORES = [{'source': 'Letterboxd', 'value': 4.1, 'max': 5}]


class ScoreServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'test.sqlite3'
        self.store = RatingsStore(self.path)

    def service(self):
        service = ScoreService(RatingsStore(self.path))
        self.addCleanup(service.close)
        return service

    @staticmethod
    def providers(loader, enabled=True):
        return {'mdblist': {'enabled': enabled, 'load': loader}}

    def seed_stale(self, imdb_id='tt1234567'):
        self.store.save_provider_cache('mdblist', 'movie', imdb_id, SCORES, now=time.time()-100, refresh_after=time.time()-50)

    def test_success_and_negative_results_survive_service_restart(self):
        loader = Mock(return_value=SCORES)
        first = self.service()
        result = first.lookup('movie', 'tt1234567', self.providers(loader))
        first.close()
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

    def test_stale_scores_return_while_refresh_is_blocked_then_update(self):
        self.seed_stale()
        entered, release = Event(), Event()
        def load():
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError()
            return [{'source': 'Letterboxd', 'value': 4.3, 'max': 5}]
        service = self.service()
        try:
            with ThreadPoolExecutor(max_workers=1) as caller:
                pending = caller.submit(service.lookup, 'movie', 'tt1234567', self.providers(load))
                self.assertTrue(entered.wait(timeout=2))
                result = pending.result(timeout=1)
                self.assertEqual(result['sources'], SCORES)
                self.assertTrue(result['providerMeta']['mdblist']['stale'])
        finally:
            release.set()
        service.close()
        refreshed = self.service().lookup('movie', 'tt1234567', self.providers(Mock()))
        self.assertEqual(refreshed['sources'][0]['value'], 4.3)
        self.assertFalse(refreshed['providerMeta']['mdblist']['stale'])

    def test_failed_refresh_preserves_stale_scores_and_backoff_after_restart(self):
        self.seed_stale()
        original = self.store.read_provider_cache('mdblist', 'movie', 'tt1234567')
        service = self.service()
        service.lookup('movie', 'tt1234567', self.providers(Mock(side_effect=URLError('private failure detail'))))
        service.close()
        forbidden = Mock(side_effect=AssertionError('Active cooldown must not load'))
        result = self.service().lookup('movie', 'tt1234567', self.providers(forbidden))
        self.assertEqual(result['sources'], SCORES)
        self.assertEqual(result['providerStatus']['mdblist'], 'unavailable')
        self.assertTrue(result['providerMeta']['mdblist']['stale'])
        self.assertEqual(result['providerMeta']['mdblist']['fetchedAt'], original['fetchedAt'])
        self.assertEqual(self.store.read_provider_cache('mdblist', 'movie', 'tt1234567'), original)
        self.assertNotIn('private', str(result))
        forbidden.assert_not_called()

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

    def test_outstanding_refresh_work_is_bounded(self):
        release = Event()
        def load():
            if not release.wait(timeout=3):
                raise TimeoutError()
            return SCORES
        service = self.service()
        try:
            for index in range(8):
                imdb_id = f'tt{index:07d}'
                self.seed_stale(imdb_id)
                result = service.lookup('movie', imdb_id, self.providers(load))
                self.assertTrue(result['providerMeta']['mdblist']['stale'])
            forbidden = Mock()
            result = service.lookup('movie', 'tt9999999', self.providers(forbidden))
            self.assertEqual(result['providerStatus']['mdblist'], 'unavailable')
            forbidden.assert_not_called()
        finally:
            release.set()

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
