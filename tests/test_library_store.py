"""Private library checks use disposable databases only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from library_store import LibraryStore, MAX_ENTRY_BYTES
from social import SocialError, SocialStore


def entry(entry_id='one', **extra):
    return {'id': entry_id, 'title': 'A private title', 'type': 'book', **extra}


def change(entry_id='one', value=None, revision=0):
    return {'id': entry_id, 'entry': value, 'baseRevision': revision}


class LibraryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'library.sqlite3'
        self.social = SocialStore(self.path)
        with self.social.db() as db:
            for user_id in ('alice', 'bob'):
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)', (user_id, user_id, user_id, '', b'salt', b'hash', 0))
        self.store = LibraryStore(self.path)

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
        with self.social.db() as db:
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

    def test_additive_schema_restart_preserves_social_data(self):
        with self.social.db() as db:
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?)', ('review', 'alice', '{}', 'Public review', 4, 0, 1, 1, None))
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.store.sync('alice', [change(value=entry())])
        restarted = LibraryStore(self.path)
        self.assertEqual(restarted.pull('alice')['cursor'], 1)
        self.assertEqual(restarted.sync('alice', [change('two', entry('two'))])['accepted'][0]['revision'], 2)
        with self.social.db() as db:
            self.assertTrue(tables <= {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            self.assertEqual(db.execute('SELECT body FROM reviews WHERE id=?', ('review',)).fetchone()[0], 'Public review')
            self.assertEqual(len(db.execute('SELECT * FROM users').fetchall()), 2)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO private_library_entries VALUES(?,?,?,?)', ('missing', 'bad', '{}', 1))

    def test_invalid_batch_rolls_back_all_changes(self):
        bad = entry('bad', notes='x' * 20001)
        self.assert_bad(lambda: self.store.sync('alice', [change(value=entry()), change('bad', bad)]))
        self.assertEqual(self.store.pull('alice'), {'changes': [], 'cursor': 0, 'hasMore': False, 'total': 0})
        with self.store.db() as db:
            db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON private_library_entries WHEN NEW.id='two' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
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
            return LibraryStore(self.path).sync('alice', [change(value=entry(notes=note))])
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, ['First', 'Second']))
        self.assertEqual(sum(len(result['accepted']) for result in results), 1)
        self.assertEqual(sum(len(result['conflicts']) for result in results), 1)
        self.assertEqual(self.store.pull('alice')['cursor'], 1)


if __name__ == '__main__':
    unittest.main()
