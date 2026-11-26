"""Hourly server-side cover pass on throwaway Postgres schemas; network is always mocked."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

import cover_maintenance
import server
from library_store import LibraryStore
from social import SocialStore
from tests.test_production import request
from tests.pg_support import PostgresTestCase, bind_server

SECRET = 'synthetic-cron-secret-for-tests'
WIKI = 'https://upload.wikimedia.org/wikipedia/commons/a/ab/Still.jpg'
TVMAZE = 'https://static.tvmaze.com/uploads/images/medium_portrait/1/1.jpg'
KITSU = 'https://media.kitsu.app/anime/poster_images/1/medium.jpg'
OPENLIB = 'https://covers.openlibrary.org/b/id/1-L.jpg'
LBXD = 'https://a.ltrbxd.com/resized/film-poster/1/1-heat-0-230-0-345-crop.jpg'
NOW = 1_800_000_000.0


class FakeLookups:
    """Answers by title; records every call. ``fail`` names titles that raise like an outage."""
    def __init__(self, covers=None, posters=None, fail=(), on_call=None):
        self.covers, self.posters, self.fail, self.on_call = covers or {}, posters or {}, set(fail), on_call
        self.calls = []

    def letterboxd(self, url):
        self.calls.append(('letterboxd', url))
        if self.on_call:
            self.on_call(url)
        if url in self.fail:
            raise URLError('offline')
        return self.posters.get(url, '')

    def cover(self, kind, title, year, creator, isbn):
        self.calls.append((kind, title, year, creator, isbn))
        if self.on_call:
            self.on_call(title)
        if title in self.fail:
            raise URLError('offline')
        return self.covers.get(title, '')


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class CoverPassTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.social = SocialStore(self.database)
        self.library = LibraryStore(self.database)
        self.users = {}

    def user(self, name):
        if name not in self.users:
            session, _ = self.social.register({'username': name, 'password': 'synthetic-password-1'})
            self.users[name] = session['user']['id']
        return self.users[name]

    def add(self, name, entry_id, **fields):
        value = {'id': entry_id, 'title': entry_id.replace('-', ' ').title(), 'type': 'tv', **fields}
        result = self.library.sync(self.user(name), [{'id': entry_id, 'entry': value, 'baseRevision': 0}])
        return result['accepted'][0]['revision']

    def entries(self, name):
        changes = self.library.pull(self.user(name))['changes']
        return {row['id']: row for row in changes}

    def run_pass(self, lookups, **options):
        options.setdefault('budget_seconds', 20)
        options.setdefault('limit', 60)
        options.setdefault('clock', Clock())
        options.setdefault('now', NOW)
        return cover_maintenance.run_cover_pass(self.database, lookups=lookups, **options)

    def test_replaces_only_missing_or_wikimedia_images(self):
        self.add('ana', 'no-image')
        self.add('ana', 'wiki-image', image=WIKI)
        self.add('ana', 'anime-show', type='anime')
        self.add('ana', 'a-book', type='book', creator='Assia Djebar', isbn='978-0-7432-7356-5', year='1985')
        self.add('ana', 'linked-film', type='movie', sourceUrl='https://boxd.it/AbCd', image=WIKI)
        untouched = {
            'own-image': {'image': 'https://example.org/poster.jpg'},
            'chosen-wiki': {'image': WIKI, 'coverChosen': True},
            'chosen-empty': {'coverChosen': True},
            'photo': {'image': 'trace-photo:' + 'a' * 64},
            'data-photo': {'image': 'data:image/png;base64,iVBORw0KGgo='},
            'a-memory': {'type': 'memory'},
            'an-album': {'type': 'album'},
            'unlinked-film': {'type': 'movie'},
        }
        for entry_id, fields in untouched.items():
            self.add('ana', entry_id, **fields)
        before = self.entries('ana')
        lookups = FakeLookups(covers={'No Image': TVMAZE, 'Wiki Image': TVMAZE, 'Anime Show': KITSU, 'A Book': OPENLIB},
                              posters={'https://boxd.it/AbCd': LBXD})
        result = self.run_pass(lookups)
        self.assertEqual(result, {'checked': 5, 'updated': 5, 'skipped': 0, 'conflicts': 0})
        after = self.entries('ana')
        expected = {'no-image': TVMAZE, 'wiki-image': TVMAZE, 'anime-show': KITSU, 'a-book': OPENLIB, 'linked-film': LBXD}
        for entry_id, image in expected.items():
            self.assertEqual(after[entry_id]['entry'], {**before[entry_id]['entry'], 'image': image}, entry_id)
            self.assertGreater(after[entry_id]['revision'], before[entry_id]['revision'])
        for entry_id in untouched:
            self.assertEqual(after[entry_id], before[entry_id], entry_id)
        self.assertIn(('book', 'A Book', '1985', 'Assia Djebar', '9780743273565'), lookups.calls)
        looked_up = {call[1] for call in lookups.calls}
        self.assertFalse(looked_up & {entry_id.replace('-', ' ').title() for entry_id in untouched})

    def test_devices_pull_the_new_image(self):
        self.add('ana', 'no-image')
        cursor = self.library.pull(self.user('ana'))['cursor']
        self.run_pass(FakeLookups(covers={'No Image': TVMAZE}))
        pulled = self.library.pull(self.user('ana'), after=cursor)
        self.assertEqual([(row['id'], row['entry']['image']) for row in pulled['changes']], [('no-image', TVMAZE)])

    def test_letterboxd_comes_first_then_the_dedicated_source(self):
        self.add('ana', 'linked-show', sourceUrl='https://letterboxd.com/film/linked-show/')
        lookups = FakeLookups(covers={'Linked Show': TVMAZE})
        self.assertEqual(self.run_pass(lookups)['updated'], 1)
        self.assertEqual([call[0] for call in lookups.calls], ['letterboxd', 'tv'])
        self.assertEqual(self.entries('ana')['linked-show']['entry']['image'], TVMAZE)

    def test_never_writes_a_wikimedia_or_off_host_image(self):
        self.add('ana', 'wiki-image', image=WIKI)
        self.add('ana', 'stray-host')
        lookups = FakeLookups(covers={'Wiki Image': WIKI, 'Stray Host': 'https://evil.example/x.jpg'})
        self.assertEqual(self.run_pass(lookups), {'checked': 2, 'updated': 0, 'skipped': 2, 'conflicts': 0})
        self.assertEqual(self.entries('ana')['wiki-image']['entry']['image'], WIKI)
        self.assertNotIn('image', self.entries('ana')['stray-host']['entry'])

    def test_a_user_edit_during_the_pass_wins(self):
        revision = self.add('ana', 'no-image')
        user = self.user('ana')

        def user_edits(_):
            value = {'id': 'no-image', 'title': 'No Image', 'type': 'tv', 'notes': 'edited on my phone'}
            self.library.sync(user, [{'id': 'no-image', 'entry': value, 'baseRevision': revision}])

        result = self.run_pass(FakeLookups(covers={'No Image': TVMAZE}, on_call=user_edits))
        self.assertEqual(result, {'checked': 1, 'updated': 0, 'skipped': 0, 'conflicts': 1})
        entry = self.entries('ana')['no-image']['entry']
        self.assertEqual(entry['notes'], 'edited on my phone')
        self.assertNotIn('image', entry)

    def test_count_bound(self):
        for index in range(5):
            self.add('ana', f'show-{index}')
        lookups = FakeLookups()
        self.assertEqual(self.run_pass(lookups, limit=2)['checked'], 2)
        self.assertEqual(len(lookups.calls), 2)

    def test_time_bound(self):
        for index in range(5):
            self.add('ana', f'show-{index}')
        clock = Clock()

        def slow(_):
            clock.now += 8

        lookups = FakeLookups(on_call=slow)
        result = self.run_pass(lookups, clock=clock, budget_seconds=20)
        self.assertEqual(result['checked'], 3)  # starts at 0, 8 and 16 s; never after 20 s
        self.assertEqual(len(lookups.calls), 3)

    def test_no_fallback_lookup_after_the_deadline(self):
        self.add('ana', 'linked-show', sourceUrl='https://boxd.it/AbCd')
        clock = Clock()

        def slow(_):
            clock.now += 25

        lookups = FakeLookups(covers={'Linked Show': TVMAZE}, on_call=slow)
        result = self.run_pass(lookups, clock=clock, budget_seconds=20)
        self.assertEqual([call[0] for call in lookups.calls], ['letterboxd'])
        self.assertEqual(result, {'checked': 1, 'updated': 0, 'skipped': 1, 'conflicts': 0})
        # Not marked as checked: the fallback was never asked, so the next run retries it.
        self.assertEqual(self.run_pass(FakeLookups(covers={'Linked Show': TVMAZE}))['updated'], 1)

    def test_round_robin_across_users_oldest_first(self):
        for index in range(4):
            self.add('ana', f'ana-{index}')
        for index in range(4):
            self.add('ben', f'ben-{index}')
        lookups = FakeLookups()
        self.assertEqual(self.run_pass(lookups, limit=4)['checked'], 4)
        titles = [call[1] for call in lookups.calls]
        self.assertEqual(sorted(titles), ['Ana 0', 'Ana 1', 'Ben 0', 'Ben 1'])
        self.assertEqual({titles[0][:3], titles[1][:3]}, {'Ana', 'Ben'})
        # The next run moves on to each account's next-oldest entries.
        lookups = FakeLookups()
        self.run_pass(lookups, limit=4)
        self.assertEqual(sorted(call[1] for call in lookups.calls), ['Ana 2', 'Ana 3', 'Ben 2', 'Ben 3'])

    def test_misses_wait_three_days_unless_the_entry_changes(self):
        revision = self.add('ana', 'no-image')
        self.add('ana', 'other-show')
        self.assertEqual(self.run_pass(FakeLookups())['skipped'], 2)
        lookups = FakeLookups(covers={'No Image': TVMAZE})
        self.assertEqual(self.run_pass(lookups, now=NOW + 3600)['checked'], 0)
        self.assertEqual(lookups.calls, [])
        value = {'id': 'no-image', 'title': 'No Image', 'type': 'tv', 'year': '2004'}
        self.library.sync(self.user('ana'), [{'id': 'no-image', 'entry': value, 'baseRevision': revision}])
        lookups = FakeLookups(covers={'No Image': TVMAZE})
        self.assertEqual(self.run_pass(lookups, now=NOW + 7200), {'checked': 1, 'updated': 1, 'skipped': 0, 'conflicts': 0})
        lookups = FakeLookups()
        self.assertEqual(self.run_pass(lookups, now=NOW + 3 * 86400 + 1)['checked'], 1)
        self.assertEqual([call[1] for call in lookups.calls], ['Other Show'])

    def test_outages_are_not_marked_and_stop_that_provider(self):
        self.add('ana', 'show-a')
        self.add('ana', 'show-b')
        self.add('ana', 'a-book', type='book')
        lookups = FakeLookups(covers={'A Book': OPENLIB}, fail={'Show A', 'Show B'})
        result = self.run_pass(lookups)
        self.assertEqual(result, {'checked': 3, 'updated': 1, 'skipped': 2, 'conflicts': 0})
        self.assertEqual([call[1] for call in lookups.calls].count('Show B') + [call[1] for call in lookups.calls].count('Show A'), 1)
        lookups = FakeLookups(covers={'Show A': TVMAZE, 'Show B': TVMAZE})
        self.assertEqual(self.run_pass(lookups, now=NOW + 3600)['updated'], 2)

    def test_an_unusable_title_is_a_miss_not_an_outage(self):
        self.add('ana', 'bad-title', title='Bad\x01Title')
        self.add('ana', 'good-show')

        def strict(kind, title, year, creator, isbn):
            cover_maintenance.cover_params(kind, title, year, creator, isbn)
            return TVMAZE

        lookups = SimpleNamespace(letterboxd=lambda url: '', cover=strict)
        self.assertEqual(self.run_pass(lookups), {'checked': 2, 'updated': 1, 'skipped': 1, 'conflicts': 0})
        self.assertEqual(self.entries('ana')['good-show']['entry']['image'], TVMAZE)
        self.assertEqual(self.run_pass(lookups, now=NOW + 3600)['checked'], 0)  # the miss is marked

    def test_account_deletion_removes_markers(self):
        self.add('ana', 'no-image')
        self.run_pass(FakeLookups())
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cover_checks').fetchone()[0], 1)
        self.social.delete_account(self.user('ana'), 'synthetic-password-1')
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cover_checks').fetchone()[0], 0)

    def test_nothing_to_do_is_one_query_and_no_lookups(self):
        self.add('ana', 'own-image', image=TVMAZE)
        self.add('ana', 'a-memory', type='memory')
        lookups, connects = FakeLookups(), []
        original = self.database.connect

        def counted(*args, **kwargs):
            connects.append(args)
            return original(*args, **kwargs)

        with patch.object(self.database, 'connect', counted):
            result = self.run_pass(lookups)
        self.assertEqual(result, {'checked': 0, 'updated': 0, 'skipped': 0, 'conflicts': 0})
        self.assertEqual((len(connects), lookups.calls), (1, []))

    def test_candidate_query_uses_the_partial_index(self):
        with self.database.connect() as db:
            db.execute('SET LOCAL enable_seqscan=off')
            plan = '\n'.join(row[0] for row in db.execute('EXPLAIN ' + cover_maintenance.CANDIDATES_SQL,
                                                          (NOW - cover_maintenance.RETRY_SECONDS, 'salt', 60)))
        self.assertIn('private_library_cover_candidates', plan)


class CoverCronRouteTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        bind_server(self, self.database)
        env = patch.dict('os.environ', {'TRACE_ENV': 'production', 'TRACE_PUBLIC_ORIGIN': 'https://trace.example',
                                        'TRACE_TRUST_PROXY': 'vercel', 'CRON_SECRET': SECRET})
        env.start()
        self.addCleanup(env.stop)
        server.API_BUDGET.buckets.clear()
        cache = patch.dict(server.CACHE, clear=True)
        cache.start()
        self.addCleanup(cache.stop)

    def cron(self, authorization=None):
        headers = {'HTTP_HOST': 'trace-abc123.vercel.app', 'HTTP_X_VERCEL_FORWARDED_FOR': '203.0.113.30'}
        if authorization is not None:
            headers['HTTP_AUTHORIZATION'] = authorization
        return request('/api/cron/covers', headers=headers)

    def add(self, username, entry_id, title):
        session, _ = server.get_social().register({'username': username, 'password': 'synthetic-password-1'})
        user_id = session['user']['id']
        value = {'id': entry_id, 'title': title, 'type': 'tv'}
        server.get_library().sync(user_id, [{'id': entry_id, 'entry': value, 'baseRevision': 0}])
        return user_id

    def test_missing_or_wrong_bearer_is_rejected(self):
        for authorization in [None, '', 'Bearer', 'Bearer wrong-secret', SECRET, 'bearer ' + SECRET]:
            response = self.cron(authorization)
            self.assertEqual(response['status'], 401, authorization)
            self.assertEqual(response['body'], json.dumps({'error': 'Unauthorized.'}).encode())
        with patch.dict('os.environ', {'CRON_SECRET': ''}):
            self.assertEqual(self.cron('Bearer ')['status'], 401)

    def test_returns_counts_only_and_shares_the_cover_cache(self):
        ana = self.add('cron_ana', 'show', 'Shared Show')
        self.add('cron_ben', 'show', 'Shared Show')
        show = {'id': 1, 'name': 'Shared Show', 'premiered': '2004-01-01', 'image': {'medium': TVMAZE},
                'url': 'https://www.tvmaze.com/shows/1/shared-show'}
        with patch.object(server, 'fetch', return_value=[{'show': show}]) as fetch, \
                patch.object(server, 'wait_turn'):
            response = self.cron('Bearer ' + SECRET)
        self.assertEqual(response['status'], 200)
        self.assertEqual(dict(response['headers'])['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(response['body']), {'checked': 2, 'updated': 2, 'skipped': 0, 'conflicts': 0})
        self.assertEqual(fetch.call_count, 1)  # the second account's lookup came from the shared cache
        self.assertEqual(server.get_library().pull(ana)['changes'][0]['entry']['image'], TVMAZE)

    def test_nothing_to_do(self):
        with patch.object(server, 'fetch') as fetch:
            response = self.cron('Bearer ' + SECRET)
        self.assertEqual(json.loads(response['body']), {'checked': 0, 'updated': 0, 'skipped': 0, 'conflicts': 0})
        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
