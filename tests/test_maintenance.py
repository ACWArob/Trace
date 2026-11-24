"""Daily maintenance (cron) on a throwaway Postgres schema; no real data is touched."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
import time
import unittest
from unittest.mock import patch

import maintenance
import server
from durable_budget import DurableBudget
from ratings import RatingsStore
from score_queue import ScoreQueue
from score_service import ScoreService
from tests.test_production import request
from tests.pg_support import PostgresTestCase, bind_server

SECRET = 'synthetic-cron-secret-for-tests'
SCORES = [{'source': 'Letterboxd', 'value': 4.1, 'max': 5}]


class MaintenanceTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        bind_server(self, self.database)
        env = patch.dict('os.environ', {'TRACE_ENV': 'production', 'TRACE_PUBLIC_ORIGIN': 'https://trace.example',
                                        'TRACE_TRUST_PROXY': 'vercel', 'CRON_SECRET': SECRET})
        env.start()
        self.addCleanup(env.stop)
        server.API_BUDGET.buckets.clear()

    def cron(self, authorization=None, host='trace.example'):
        headers = {'HTTP_HOST': host, 'HTTP_X_VERCEL_FORWARDED_FOR': '203.0.113.30'}
        if authorization is not None:
            headers['HTTP_AUTHORIZATION'] = authorization
        return request('/api/cron/maintenance', headers=headers)

    def seed(self, now):
        social = server.get_social()
        session, token = social.register({'username': 'cron_user', 'password': 'synthetic-password-1'})
        live_invite = social.create_invite()
        with self.database.connect() as db:
            user_id = session['user']['id']
            db.execute('INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)',
                       ('expired-hash', user_id, 'csrf', now - 10))
            db.execute('INSERT INTO beta_invites VALUES(?,?,NULL,NULL)', ('expired-invite', now - 10))
            db.execute('INSERT INTO beta_invites VALUES(?,?,?,?)', ('claimed-invite', now - 10, now - 20, user_id))
            db.execute('INSERT INTO auth_budget VALUES(?,?,?)', ('old-budget', now - 7200, 3))
            db.execute('INSERT INTO auth_budget VALUES(?,?,?)', ('new-budget', now - 30, 3))
        return token, live_invite

    def rows(self, sql):
        with self.database.connect() as db:
            return sorted(row[0] for row in db.execute(sql))

    def test_missing_or_wrong_bearer_is_rejected(self):
        for authorization in [None, '', 'Bearer', 'Bearer wrong-secret', SECRET, 'Basic ' + SECRET, 'bearer ' + SECRET + 'x']:
            response = self.cron(authorization)
            self.assertEqual(response['status'], 401, authorization)
            self.assertEqual(response['body'], json.dumps({'error': 'Unauthorized.'}).encode())
        with patch.dict('os.environ', {'CRON_SECRET': ''}):
            self.assertEqual(self.cron('Bearer ')['status'], 401)

    def test_correct_bearer_purges_only_expired_rows(self):
        now = time.time()
        token, _ = self.seed(now)
        response = self.cron('Bearer ' + SECRET)
        self.assertEqual(response['status'], 200)
        self.assertEqual(dict(response['headers'])['Cache-Control'], 'no-store')
        result = json.loads(response['body'])
        self.assertEqual((result['sessions'], result['invites'], result['budgets']), (1, 1, 1))
        self.assertEqual(result['scores'], {'claimed': 0, 'done': 0, 'failed': 0})
        self.assertEqual(len(self.rows('SELECT token_hash FROM sessions')), 1)
        self.assertIsNotNone(server.get_social().session(token)['user'])
        self.assertEqual(len(self.rows('SELECT invite_hash FROM beta_invites')), 2)
        self.assertIn('claimed-invite', self.rows('SELECT invite_hash FROM beta_invites'))
        self.assertEqual(self.rows('SELECT key FROM auth_budget'), ['new-budget'])

    def test_cron_route_does_not_depend_on_the_request_host(self):
        self.assertEqual(self.cron('Bearer ' + SECRET, host='trace-abc123.vercel.app')['status'], 200)
        self.assertEqual(self.cron(None, host='trace-abc123.vercel.app')['status'], 401)

    def test_queued_scores_are_refreshed_with_their_titles(self):
        ScoreQueue(self.database).enqueue('omdb', 'movie', 'tt1234567', title='Nedjma')
        calls = []

        def omdb(imdb_id, title, fetch):
            calls.append((imdb_id, title))
            return SCORES

        with patch.dict('os.environ', {'OMDB_API_KEY': 'synthetic-omdb'}), patch.object(server, 'omdb_scores', omdb):
            result = json.loads(self.cron('Bearer ' + SECRET)['body'])
        self.assertEqual(result['scores'], {'claimed': 1, 'done': 1, 'failed': 0})
        self.assertEqual(calls, [('tt1234567', 'Nedjma')])
        self.assertEqual(server.get_ratings().read_provider_cache('omdb', 'movie', 'tt1234567')['sources'][0]['value'], 4.1)

    def test_open_library_keys_are_rebuilt_from_their_identifier(self):
        loaders = {key: server.score_loader('openlibrary', 'book', key, 'A Book')
                   for key in ['/works/OL123W', 'isbn:9780140328721', 'apple:1']}
        self.assertIsNone(loaders['apple:1'])
        self.assertIsNone(server.score_loader('googlebooks', 'book', 'isbn:9780140328721', 'A Book'))
        seen = []
        with patch.object(server, 'openlibrary_scores', lambda item, fetch: seen.append(item) or SCORES):
            loaders['/works/OL123W']()
            loaders['isbn:9780140328721']()
        self.assertEqual(seen, [{'externalId': '/works/OL123W', 'title': 'A Book'},
                                {'isbn': '9780140328721', 'title': 'A Book'}])

    def test_unconfigured_providers_are_dropped_from_the_queue(self):
        ScoreQueue(self.database).enqueue('mdblist', 'movie', 'tt1234567', title='A Film')
        with patch.dict('os.environ', {'MDBLIST_API_KEY': ''}):
            result = json.loads(self.cron('Bearer ' + SECRET)['body'])
        self.assertEqual(result['scores'], {'claimed': 1, 'done': 1, 'failed': 0})
        self.assertEqual(ScoreQueue(self.database).size(), 0)

    def test_rows_past_the_time_budget_are_requeued(self):
        queue = ScoreQueue(self.database)
        for index in range(3):
            queue.enqueue('omdb', 'movie', f'tt000000{index}', title='Film', now=index)
        clock = iter([0, 0, 5, 30, 31, 32])
        loads = []

        def loader_for(provider, kind, imdb_id, title):
            loads.append(imdb_id)
            return lambda: SCORES

        service = ScoreService(RatingsStore(self.database))
        result = maintenance.run(self.database, service=service, loader_for=loader_for,
                                 budget_seconds=20, clock=lambda: next(clock))
        self.assertEqual(result['scores'], {'claimed': 3, 'done': 2, 'failed': 1})
        self.assertEqual(loads, ['tt0000000', 'tt0000001'])
        self.assertEqual(queue.size(), 1)

    def test_concurrent_runs_never_refresh_a_row_twice(self):
        queue = ScoreQueue(self.database)
        for index in range(40):
            queue.enqueue('omdb', 'movie', f'tt{index:07d}', title='Film', now=index)
        loads = []
        barrier = Barrier(3)

        def loader_for(provider, kind, imdb_id, title):
            loads.append(imdb_id)
            return lambda: SCORES

        def run(_):
            barrier.wait()
            return maintenance.run(self.database, service=ScoreService(RatingsStore(self.database)),
                                   loader_for=loader_for, limit=25)

        with ThreadPoolExecutor(3) as executor:
            results = list(executor.map(run, range(3)))
        self.assertEqual(sum(result['scores']['done'] for result in results), 40)
        self.assertEqual(sorted(loads), sorted(set(loads)))
        self.assertEqual(len(loads), 40)
        self.assertEqual(queue.size(), 0)

    def test_budget_purge_uses_the_durable_budget(self):
        budget = DurableBudget(self.database)
        budget.allow('auth:198.51.100.1', 15)
        result = maintenance.run(self.database, now=time.time() + 7200)
        self.assertEqual(result['budgets'], 1)
        self.assertNotIn('scores', result)


if __name__ == '__main__':
    unittest.main()
