import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database  # noqa: E402
from tests.pg_support import TEST_DATABASE_URL, PostgresTestCase, create_schema, drop_schema  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class PlaceholderTranslationTest(unittest.TestCase):
    def test_question_marks_become_psycopg_placeholders(self):
        self.assertEqual(database.translate('SELECT * FROM t WHERE a=? AND b=?'),
                         'SELECT * FROM t WHERE a=%s AND b=%s')

    def test_literal_percent_is_escaped(self):
        self.assertEqual(database.translate("SELECT '50%' WHERE a LIKE ?"),
                         "SELECT '50%%' WHERE a LIKE %s")

    def test_question_marks_inside_quotes_are_left_alone(self):
        self.assertEqual(database.translate("SELECT 'why?', \"col?\" FROM t WHERE a=? AND b='it''s?'"),
                         "SELECT 'why?', \"col?\" FROM t WHERE a=%s AND b='it''s?'")


class ConnectionTest(PostgresTestCase):
    def test_parameters_and_literal_percent_survive(self):
        with self.database.connect() as db:
            row = db.execute("SELECT '50%' AS literal, ? AS value, 'why?' AS question", ('x%y',)).fetchone()
            self.assertEqual(tuple(row), ('50%', 'x%y', 'why?'))
            self.assertEqual(db.execute("SELECT '100%'").fetchone()[0], '100%')
            self.assertEqual(db.execute("SELECT '100%'", ()).fetchone()[0], '100%')

    def test_rows_support_index_name_and_dict_access(self):
        with self.database.connect() as db:
            row = db.execute('SELECT 1 AS one, ? AS two', ('b',)).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row['two'], 'b')
        self.assertEqual(row.keys(), ['one', 'two'])
        self.assertEqual(dict(row), {'one': 1, 'two': 'b'})
        one, two = row
        self.assertEqual((one, two), (1, 'b'))
        with self.assertRaises(IndexError):
            row['missing']

    def test_commit_on_success_is_visible_to_other_connections(self):
        with self.database.connect() as db:
            db.execute('INSERT INTO catalog_queries VALUES(?,?,?,?)', ('movie', 'a', '[]', 1.0))
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM catalog_queries').fetchone()[0], 1)

    def test_exception_rolls_back_and_propagates(self):
        with self.assertRaises(RuntimeError):
            with self.database.connect() as db:
                db.execute('INSERT INTO catalog_queries VALUES(?,?,?,?)', ('movie', 'a', '[]', 1.0))
                raise RuntimeError('boom')
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM catalog_queries').fetchone()[0], 0)

    def test_explicit_commit_and_rollback(self):
        with self.database.connect() as db:
            db.execute('INSERT INTO catalog_queries VALUES(?,?,?,?)', ('movie', 'kept', '[]', 1.0))
            db.commit()
            db.execute('INSERT INTO catalog_queries VALUES(?,?,?,?)', ('movie', 'dropped', '[]', 1.0))
            db.rollback()
            self.assertEqual(db.execute('SHOW statement_timeout').fetchone()[0], '8s')
        with self.database.connect() as db:
            rows = db.execute('SELECT query FROM catalog_queries ORDER BY query').fetchall()
        self.assertEqual([r['query'] for r in rows], ['kept'])

    def test_executemany_translates_placeholders(self):
        with self.database.connect() as db:
            db.executemany('INSERT INTO catalog_queries VALUES(?,?,?,?)',
                           [('movie', 'a', '[]', 1.0), ('movie', 'b', '[]', 2.0)])
            self.assertEqual(db.execute('SELECT count(*) FROM catalog_queries').fetchone()[0], 2)

    def test_connection_is_closed_after_block(self):
        with self.database.connect() as db:
            pass
        self.assertTrue(db.closed)

    def test_duplicate_key_maps_to_integrity_error(self):
        with self.database.connect() as db:
            db.execute('INSERT INTO catalog_queries VALUES(?,?,?,?)', ('movie', 'a', '[]', 1.0))
        with self.assertRaises(database.IntegrityError) as caught:
            with self.database.connect() as db:
                db.execute('INSERT INTO catalog_queries VALUES(?,?,?,?)', ('movie', 'a', '[]', 1.0))
        self.assertIsInstance(caught.exception, database.Error)

    def test_bad_sql_maps_to_error(self):
        with self.assertRaises(database.Error):
            with self.database.connect() as db:
                db.execute('SELECT * FROM no_such_table')

    def test_unreachable_server_maps_to_error(self):
        unreachable = database.Database('postgresql://postgres:x@127.0.0.1:1/postgres')
        with self.assertRaises(database.Error):
            with unreachable.connect():
                pass

    def test_isolation_parameter(self):
        with self.database.connect(isolation='repeatable read') as db:
            self.assertEqual(db.execute('SHOW transaction_isolation').fetchone()[0], 'repeatable read')
        with self.database.connect() as db:
            self.assertEqual(db.execute('SHOW transaction_isolation').fetchone()[0], 'read committed')
        with self.assertRaises(ValueError):
            with self.database.connect(isolation='chaos'):
                pass

    def test_statement_timeout_is_set_per_transaction(self):
        with self.database.connect() as db:
            self.assertEqual(db.execute('SHOW statement_timeout').fetchone()[0], '8s')

    def test_jsonb_columns_load_as_text(self):
        payload = json.dumps({'title': 'Amélie', 'isbn': '1'})
        with self.database.connect() as db:
            db.execute('INSERT INTO catalog(item_key,kind,title,creator,genres,payload,updated_at) VALUES(?,?,?,?,?,?,?)',
                       ('m:1', 'movie', 'Amélie', 'Jeunet', json.dumps(['Comedy']), payload, 1.0))
            row = db.execute("SELECT payload, genres, payload->>'isbn' AS isbn, title_norm FROM catalog").fetchone()
        self.assertIsInstance(row['payload'], str)
        self.assertEqual(json.loads(row['payload']), {'title': 'Amélie', 'isbn': '1'})
        self.assertEqual(json.loads(row['genres']), ['Comedy'])
        self.assertEqual(row['isbn'], '1')
        self.assertEqual(row['title_norm'], '')

    def test_advisory_lock_is_held_until_transaction_ends(self):
        with self.database.connect() as db:
            database.lock(db, 'catalog-write')
            with self.database.connect() as other:
                taken = other.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(?,0))', ('catalog-write',)).fetchone()[0]
            self.assertFalse(taken)
        with self.database.connect() as other:
            taken = other.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(?,0))', ('catalog-write',)).fetchone()[0]
        self.assertTrue(taken)


class SchemaTypeTest(PostgresTestCase):
    def add_user(self, db, user_id, username):
        db.execute('INSERT INTO users(id,username,display_name,salt,password_hash,created_at) VALUES(?,?,?,?,?,?)',
                   (user_id, username, username, b'\x00salt', b'\x01hash', 1795000000.5))

    def test_username_is_unique_case_insensitively(self):
        with self.database.connect() as db:
            self.add_user(db, 'u1', 'Ada')
        with self.assertRaises(database.IntegrityError):
            with self.database.connect() as db:
                self.add_user(db, 'u2', 'ada')
        with self.database.connect() as db:
            row = db.execute('SELECT id, username, salt FROM users WHERE username=?', ('ADA',)).fetchone()
        self.assertEqual(row['id'], 'u1')
        self.assertEqual(row['username'], 'Ada')
        self.assertEqual(row['salt'], b'\x00salt')
        self.assertIsInstance(row['salt'], bytes)

    def test_bigint_revision_and_cursor_hold_two_to_the_53_minus_one(self):
        big = 2 ** 53 - 1
        with self.database.connect() as db:
            self.add_user(db, 'u1', 'ada')
            db.execute('INSERT INTO private_library_state(user_id,cursor) VALUES(?,?)', ('u1', big))
            db.execute('INSERT INTO private_library_entries(user_id,id,entry,revision) VALUES(?,?,?,?)', ('u1', 'e1', '{}', big))
            row = db.execute('SELECT s.cursor, e.revision FROM private_library_state s JOIN private_library_entries e USING(user_id)').fetchone()
        self.assertEqual(tuple(row), (big, big))

    def test_double_precision_epoch_round_trips(self):
        epoch = 1795000123.456789
        with self.database.connect() as db:
            self.add_user(db, 'u1', 'ada')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', ('t', 'u1', 'c', epoch))
            self.assertEqual(db.execute('SELECT expires_at FROM sessions').fetchone()[0], epoch)

    def test_review_spoiler_is_boolean_and_item_is_jsonb(self):
        with self.database.connect() as db:
            self.add_user(db, 'u1', 'ada')
            db.execute('INSERT INTO reviews(id,user_id,item,body,rating,spoiler,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       ('r1', 'u1', json.dumps({'type': 'book', 'title': 'Nedjma'}), 'Good', 8.5, True, 1.0, 1.0))
            row = db.execute("SELECT spoiler, rating, item->>'type' AS type FROM reviews").fetchone()
        self.assertIs(row['spoiler'], True)
        self.assertEqual(row['rating'], 8.5)
        self.assertEqual(row['type'], 'book')

    def test_library_rows_require_an_existing_user(self):
        with self.assertRaises(database.IntegrityError):
            with self.database.connect() as db:
                db.execute('INSERT INTO private_library_state(user_id,cursor) VALUES(?,?)', ('ghost', 0))

    def test_runtime_and_photo_tables_exist(self):
        with self.database.connect() as db:
            db.execute('INSERT INTO auth_budget(key,window_start,hits) VALUES(?,?,?)', ('k', 1.0, 1))
            db.execute('INSERT INTO score_refresh_queue(provider,kind,external_id,queued_at) VALUES(?,?,?,?)', ('mdblist', 'movie', 'tt1', 1.0))
            db.execute('INSERT INTO photos(user_id,id,pathname,content_type,bytes,created_at) VALUES(?,?,?,?,?,?)',
                       ('deleted-user-ok', 'a' * 64, 'photos/u/a.jpg', 'image/jpeg', 1234, 1.0))
            db.execute('INSERT INTO public_scores VALUES(?,?,?,?,?,?,?)', ('imdb', 'tt1', 7.5, 10.0, 3_000_000_000, 'https://x', 1.0))
            db.execute('INSERT INTO provider_score_cache VALUES(?,?,?,?,?,?)', ('mdblist', 'movie', 'tt1', '[]', 1.0, 2.0))
            db.execute('INSERT INTO provider_score_backoff VALUES(?,?,?)', ('mdblist', 'rate_limited', 1.0))
            self.assertEqual(db.execute('SELECT votes FROM public_scores').fetchone()[0], 3_000_000_000)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.schema, self.database = create_schema(migrate=False)

    def tearDown(self):
        drop_schema(self.schema)

    def versions(self):
        with self.database.connect() as db:
            return [r[0] for r in db.execute('SELECT version FROM schema_migrations ORDER BY version')]

    def test_migrate_applies_all_files_once(self):
        applied = database.migrate(self.database)
        self.assertEqual(applied, ['001_catalog', '002_social', '003_library', '004_ratings', '005_runtime', '006_photos', '007_cover_checks'])
        self.assertEqual(database.migrate(self.database), [])
        self.assertEqual(self.versions(), applied)

    def test_concurrent_migrations_are_serialized(self):
        errors, results = [], []

        def run():
            try:
                results.append(database.migrate(self.database))
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(len(r) for r in results), [0, 0, 7])
        self.assertEqual(len(self.versions()), 7)

    def test_cli_migrates_named_schema_twice_without_printing_the_url(self):
        script = [sys.executable, str(ROOT / 'scripts' / 'migrate_db.py'), '--schema', self.schema]
        env = {'TRACE_DATABASE_URL': TEST_DATABASE_URL, 'PATH': '/usr/bin:/bin'}
        first = subprocess.run(script, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('007_cover_checks', first.stdout)
        second = subprocess.run(script, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('up to date', second.stdout)
        self.assertNotIn('trace@', first.stdout + first.stderr + second.stdout + second.stderr)
        self.assertEqual(len(self.versions()), 7)

    def test_cli_requires_a_database_url(self):
        script = [sys.executable, str(ROOT / 'scripts' / 'migrate_db.py')]
        result = subprocess.run(script, env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn('DATABASE_URL', result.stderr)


if __name__ == '__main__':
    unittest.main()
