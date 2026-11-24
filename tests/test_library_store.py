"""Private library checks use disposable databases only."""
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import database
from library_store import LibraryStore, MAX_ENTRY_BYTES, MAX_REVISION
from social import SocialError
from tests.pg_support import PostgresTestCase


def entry(entry_id='one', **extra):
    return {'id': entry_id, 'title': 'A private title', 'type': 'book', **extra}


def change(entry_id='one', value=None, revision=0):
    return {'id': entry_id, 'entry': value, 'baseRevision': revision}


class LibraryStoreTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        with self.database.connect() as db:
            for user_id in ('alice', 'bob'):
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)', (user_id, user_id, user_id, '', b'salt', b'hash', 0))
        self.store = LibraryStore(self.database)

    def drop_fail_trigger(self):
        with self.database.connect() as db:
            db.execute('DROP TRIGGER IF EXISTS fail_second ON private_library_entries')

    def assert_bad(self, operation, status=400):
        with self.assertRaises(SocialError) as error:
            operation()
        self.assertEqual(error.exception.status, status)

    def test_accounts_are_isolated_even_with_identical_entry_ids(self):
        a = self.store.sync('alice', [change(value=entry(notes='Only Alice'))])
        b = self.store.sync('bob', [change(value=entry(notes='Only Bob'))])
        self.assertEqual(a['accepted'][0]['revision'], 1)
        self.assertEqual(b['accepted'][0]['revision'], 1)
        self.assertEqual(self.store.pull('alice')['changes'][0]['entry']['notes'], 'Only Alice')
        self.assertEqual(self.store.pull('bob')['changes'][0]['entry']['notes'], 'Only Bob')
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reviews').fetchone()[0], 0)
        for unknown in (None, '', 'missing', True, ['alice']):
            self.assert_bad(lambda: self.store.pull(unknown), 401)
            self.assert_bad(lambda: self.store.sync(unknown, []), 401)

    def test_duplicate_create_stale_edit_and_missing_record_conflicts(self):
        original = self.store.sync('alice', [change(value=entry(notes='Original'))])['accepted'][0]
        for revision in (0, 5):
            result = self.store.sync('alice', [change(value=entry(notes='Overwritten'), revision=revision)])
            self.assertEqual(result, {'accepted': [], 'conflicts': [original]})
        result = self.store.sync('alice', [change('missing', entry('missing'), 1)])
        self.assertEqual(result['conflicts'], [{'id': 'missing', 'entry': None, 'revision': 0}])
        result = self.store.sync('alice', [change(value=entry(notes='Edited'), revision=1)])
        self.assertEqual(result['accepted'][0]['revision'], 2)

    def test_tombstones_incremental_paging_and_resurrection(self):
        self.store.sync('alice', [change('one', entry('one')), change('two', entry('two'))])
        self.store.sync('alice', [change('one', None, 1)])
        first = self.store.pull('alice', limit=1)
        self.assertEqual(first['changes'][0]['id'], 'two')
        self.assertEqual((first['cursor'], first['hasMore'], first['total']), (2, True, 1))
        last = self.store.pull('alice', after=first['cursor'], limit=1)
        self.assertEqual(last, {'changes': [{'id': 'one', 'entry': None, 'revision': 3}], 'cursor': 3, 'hasMore': False, 'total': 1})
        self.assertEqual(self.store.pull('alice', after=3)['changes'], [])
        self.assertEqual(self.store.sync('alice', [change('one', entry())])['conflicts'], last['changes'])
        self.assertEqual(self.store.sync('alice', [change('one', entry(), 3)])['accepted'][0]['revision'], 4)
        self.assertEqual(self.store.pull('alice')['total'], 2)

    def test_all_private_fields_are_preserved_without_normalization(self):
        photo = 'data:image/png;base64,aGVsbG8='
        private = entry(type='memory', title=' An unchanged memory ', creator='At home', year='',
                        status='done', favorite=True, forLater=False, rating=0, date='',
                        history=['2024-02-29', '2025-03-05'], notes='A private note\nwith details 💛',
                        description='Private description', source='Manual entry', externalId='old-id',
                        addedAt='2025-03-05T12:13:14.000Z', imdbId='', publicScores=[{
                            'source': 'IMDb', 'value': 8.3, 'max': 10, 'count': 1234,
                            'url': 'https://www.imdb.com/title/tt123/', 'via': 'Provider'}],
                        genres=['Life'], image=photo, sourceUrl='https://example.org/original',
                        metadataSource='Open Library', metadataSourceUrl='https://openlibrary.org/works/OL1W',
                        legacyType='place', legacyRating=4.5, attachments=[{
                            'id': 'book_1', 'type': 'book', 'title': 'Kept snapshot', 'creator': 'Author',
                            'year': '2001', 'image': '/assets/book.jpg'}],
                        futureMetadata={'edition': 'Unchanged', 'nested': [1, None, True]})
        result = self.store.sync('alice', [change(value=private)])
        self.assertEqual(result['accepted'][0]['entry'], private)
        self.assertEqual(self.store.pull('alice')['changes'][0]['entry'], private)

    def test_new_store_instance_keeps_cursor_and_social_data(self):
        with self.database.connect() as db:
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?)', ('review', 'alice', '{}', 'Public review', 4, False, 1, 1, None))
        self.store.sync('alice', [change(value=entry())])
        restarted = LibraryStore(self.database)
        self.assertEqual(restarted.pull('alice')['cursor'], 1)
        self.assertEqual(restarted.sync('alice', [change('two', entry('two'))])['accepted'][0]['revision'], 2)
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT body FROM reviews WHERE id=?', ('review',)).fetchone()[0], 'Public review')
            self.assertEqual(len(db.execute('SELECT * FROM users').fetchall()), 2)
        with self.assertRaises(database.IntegrityError):
            with self.database.connect() as db:
                db.execute('INSERT INTO private_library_entries VALUES(?,?,?,?)', ('missing', 'bad', '{}', 1))

    def test_invalid_batch_rolls_back_all_changes(self):
        bad = entry('bad', notes='x' * 20001)
        self.assert_bad(lambda: self.store.sync('alice', [change(value=entry()), change('bad', bad)]))
        self.assertEqual(self.store.pull('alice'), {'changes': [], 'cursor': 0, 'hasMore': False, 'total': 0})
        with self.database.connect() as db:
            db.execute("CREATE FUNCTION fail_second() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id='two' THEN RAISE EXCEPTION 'test failure' USING ERRCODE='23514'; END IF; RETURN NEW; END $$")
            db.execute('CREATE TRIGGER fail_second BEFORE INSERT ON private_library_entries FOR EACH ROW EXECUTE FUNCTION fail_second()')
        self.addCleanup(self.drop_fail_trigger)
        with self.assertRaises(database.IntegrityError):
            self.store.sync('alice', [change(value=entry()), change('two', entry('two'))])
        self.assertEqual(self.store.pull('alice')['changes'], [])
        self.assertEqual(self.store.pull('alice')['cursor'], 0)

    def test_malformed_fields_ids_revisions_and_query_limits(self):
        for revision in (True, False, -1, 1.2, '0', None, 9007199254740992):
            self.assert_bad(lambda: self.store.sync('alice', [change(value=entry(), revision=revision)]))
        for invalid_id in ('', 'has space', 'x' * 101, True, None, ['one']):
            self.assert_bad(lambda: self.store.sync('alice', [change(invalid_id, None)]))
        for value in (None, {}, [None], [change()] * 2, [change(str(i)) for i in range(101)]):
            self.assert_bad(lambda: self.store.sync('alice', value))
        for patch_value in ({'id': 'different'}, {'title': ''}, {'type': []}, {'rating': True},
                            {'rating': float('nan')}, {'rating': 6}, {'favorite': 'yes'}, {'notes': 1},
                            {'history': ['2025-02-30']}, {'date': None}, {'genres': ['x'] * 9},
                            {'attachments': [{'title': 'Bad', 'type': 'memory'}]},
                            {'publicScores': [{'source': 'IMDb', 'value': 11, 'max': 10}]},
                            {'image': 'data:image/svg+xml;base64,PHN2Zz4='},
                            {'image': 'data:image/png;base64,abc'}, {'sourceUrl': 'javascript:alert(1)'},
                            {'hugeNumber': 10 ** 1000}):
            self.assert_bad(lambda: self.store.sync('alice', [change(value=entry(**patch_value))]))
        for after, limit in ((True, 1), (-1, 1), (1, 1), (0, False), (0, 0), (0, 201), ('0', 1)):
            self.assert_bad(lambda: self.store.pull('alice', after, limit))
        self.assertEqual(self.store.pull('alice')['total'], 0)

    def test_utf8_size_and_live_entry_limits_are_atomic(self):
        large = entry(futureMetadata='💛' * (MAX_ENTRY_BYTES // 4))
        self.assert_bad(lambda: self.store.sync('alice', [change(value=large)]))
        with patch('library_store.MAX_ENTRIES', 2):
            self.store.sync('alice', [change('one', entry()), change('two', entry('two'))])
            self.assert_bad(lambda: self.store.sync('alice', [change('three', entry('three')), change('one', entry(notes='changed'), 1)]))
            self.assertEqual(self.store.pull('alice')['cursor'], 2)
            self.assertNotIn('notes', self.store.pull('alice')['changes'][0]['entry'])
            self.store.sync('alice', [change('three', entry('three')), change('one', None, 1)])
            self.assertEqual(self.store.pull('alice')['total'], 2)

    def test_concurrent_create_accepts_exactly_one_writer(self):
        def write(note):
            return LibraryStore(self.database).sync('alice', [change(value=entry(notes=note))])
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, ['First', 'Second']))
        self.assertEqual(sum(len(result['accepted']) for result in results), 1)
        self.assertEqual(sum(len(result['conflicts']) for result in results), 1)
        self.assertEqual(self.store.pull('alice')['cursor'], 1)

    def test_account_byte_quota_is_utf8_atomic_and_allows_space_recovery(self):
        store = LibraryStore(self.database, byte_limit=200)
        store.sync('alice', [change(value=entry(notes='💛' * 12))])
        before = store.pull('alice')
        self.assert_bad(lambda: store.sync('alice', [change('two', entry('two', notes='💛' * 40)), change('one', entry(notes='Changed'), 1)]))
        self.assertEqual(store.pull('alice'), before)
        store.sync('bob', [change(value=entry())])
        # A reduced quota must not prevent the owner shrinking or deleting old data.
        store = LibraryStore(self.database, byte_limit=20)
        store.sync('alice', [change(value=entry(), revision=1)])
        store.sync('alice', [change(value=None, revision=2)])
        self.assertEqual(store.pull('alice')['total'], 0)
        self.assertEqual(store.pull('bob')['total'], 1)

    def test_photo_pages_are_byte_bounded_without_skipping_revisions(self):
        self.store.sync('alice', [change('one', entry(notes='x' * 70)), change('two', entry('two', notes='x' * 70)), change('three', None)])
        with patch('library_store.MAX_PULL_BYTES', 150):
            first = self.store.pull('alice')
            self.assertEqual([x['id'] for x in first['changes']], ['one'])
            self.assertTrue(first['hasMore'])
            second = self.store.pull('alice', after=first['cursor'])
            self.assertEqual([x['id'] for x in second['changes']], ['two', 'three'])
            self.assertFalse(second['hasMore'])
            self.assertEqual(second['cursor'], 3)

    def test_concurrent_syncs_never_reuse_a_revision_and_leave_no_gaps(self):
        workers, per_worker = 8, 5

        def write(worker):
            store = LibraryStore(self.database)
            revisions = []
            for index in range(per_worker):
                entry_id = f'w{worker}-{index}'
                result = store.sync('alice', [change(entry_id, entry(entry_id))])
                self.assertEqual(result['conflicts'], [])
                revisions.append(result['accepted'][0]['revision'])
            return revisions
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(write, range(workers)))
        issued = sorted(revision for revisions in results for revision in revisions)
        self.assertEqual(issued, list(range(1, workers * per_worker + 1)))
        pulled = self.store.pull('alice', limit=200)
        self.assertEqual((pulled['cursor'], pulled['total'], pulled['hasMore']), (workers * per_worker, workers * per_worker, False))
        self.assertEqual([c['revision'] for c in pulled['changes']], issued)
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT cursor FROM private_library_state WHERE user_id=?', ('alice',)).fetchone()[0], workers * per_worker)

    def test_concurrent_multi_change_batches_keep_revisions_contiguous(self):
        def write(worker):
            return LibraryStore(self.database).sync('alice', [change(f'b{worker}-{i}', entry(f'b{worker}-{i}')) for i in range(3)])
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(write, range(6)))
        for result in results:
            revisions = [x['revision'] for x in result['accepted']]
            self.assertEqual(revisions, list(range(revisions[0], revisions[0] + 3)))
        self.assertEqual(sorted(x['revision'] for r in results for x in r['accepted']), list(range(1, 19)))
        self.assertEqual(self.store.pull('alice', limit=200)['cursor'], 18)

    def test_revision_limit_is_accepted_at_the_edge_and_rejected_beyond(self):
        self.store.sync('alice', [change(value=entry())])
        with self.database.connect() as db:
            db.execute('UPDATE private_library_state SET cursor=? WHERE user_id=?', (MAX_REVISION - 1, 'alice'))
            db.execute('UPDATE private_library_entries SET revision=? WHERE user_id=?', (MAX_REVISION - 1, 'alice'))
        top = self.store.sync('alice', [change(value=entry(notes='Edge'), revision=MAX_REVISION - 1)])
        self.assertEqual(top['accepted'][0]['revision'], MAX_REVISION)
        pulled = self.store.pull('alice', after=MAX_REVISION - 1)
        self.assertEqual((pulled['cursor'], pulled['changes'][0]['revision']), (MAX_REVISION, MAX_REVISION))
        self.assertEqual(self.store.pull('alice', after=MAX_REVISION)['changes'], [])
        self.assert_bad(lambda: self.store.sync('alice', [change(value=entry(notes='Over'), revision=MAX_REVISION)]))
        self.assertEqual(self.store.sync('alice', [change('two', entry('two'), MAX_REVISION)])['conflicts'],
                         [{'id': 'two', 'entry': None, 'revision': 0}])
        self.assert_bad(lambda: self.store.sync('alice', [change(value=entry(), revision=MAX_REVISION + 1)]))
        self.assertEqual(self.store.pull('alice')['cursor'], MAX_REVISION)

    def test_a_user_cannot_pull_or_conflict_into_another_users_rows(self):
        self.store.sync('alice', [change('secret', entry('secret', notes='Alice only'))])
        self.assertEqual(self.store.pull('bob'), {'changes': [], 'cursor': 0, 'hasMore': False, 'total': 0})
        self.assert_bad(lambda: self.store.pull('bob', after=1))
        result = self.store.sync('bob', [change('secret', entry('secret', notes='Bob copy'), 1)])
        self.assertEqual(result, {'accepted': [], 'conflicts': [{'id': 'secret', 'entry': None, 'revision': 0}]})
        self.assertEqual(self.store.pull('alice')['changes'][0]['entry']['notes'], 'Alice only')

    def test_pull_reads_one_repeatable_snapshot(self):
        self.store.sync('alice', [change('one', entry('one'))])
        with self.database.connect(isolation='repeatable read') as db:
            self.assertEqual(db.execute('SELECT cursor FROM private_library_state WHERE user_id=?', ('alice',)).fetchone()[0], 1)
            self.store.sync('alice', [change('two', entry('two'))])
            self.assertEqual(db.execute('SELECT count(*) FROM private_library_entries WHERE user_id=?', ('alice',)).fetchone()[0], 1)
        seen = []
        original = LibraryStore._record

        def record(row, entry_id=None):
            if not seen:
                seen.append(1)
                LibraryStore(self.database).sync('alice', [change('three', entry('three'))])
            return original(row, entry_id)
        with patch.object(LibraryStore, '_record', staticmethod(record)):
            page = self.store.pull('alice')
        self.assertEqual((page['cursor'], page['total'], [c['id'] for c in page['changes']]), (2, 2, ['one', 'two']))
        self.assertEqual(self.store.pull('alice')['cursor'], 3)


if __name__ == '__main__':
    unittest.main()
