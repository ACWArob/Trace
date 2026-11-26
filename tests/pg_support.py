"""Postgres helpers for tests: one throwaway schema per test class.

Usage::

    from tests.pg_support import PostgresTestCase

    class StoreTest(PostgresTestCase):
        def test_something(self):
            store = CatalogStore(self.database)   # migrated, empty schema

``PostgresTestCase`` creates a uniquely named schema (``trace_test_<hex>``) in
``setUpClass``, applies ``migrations/`` to it, truncates every table in
``setUp`` so each test starts empty, and drops the schema in
``tearDownClass``.  ``self.database`` / ``cls.database`` is a
``database.Database`` whose ``search_path`` is ``<schema>, public`` (``public``
holds the ``citext`` extension).  Parallel test runs never share tables.

Use ``create_schema()`` / ``drop_schema()`` directly for a fresh schema per
test (``create_schema(migrate=False)`` returns an unmigrated one).

The server comes from ``TRACE_TEST_DATABASE_URL`` (default: the container
started by ``scripts/dev_postgres.sh``).  If it is unreachable the tests are
skipped with a message saying how to start it.  Never point this at a
database that holds real data.
"""

import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database  # noqa: E402

DEFAULT_TEST_DATABASE_URL = 'postgresql://postgres:trace@127.0.0.1:54329/postgres'
TEST_DATABASE_URL = os.environ.get('TRACE_TEST_DATABASE_URL') or DEFAULT_TEST_DATABASE_URL
SKIP_MESSAGE = ('Postgres is unreachable at TRACE_TEST_DATABASE_URL '
                '(default 127.0.0.1:54329); run scripts/dev_postgres.sh')

_reachable = None


def require_postgres():
    """Raise unittest.SkipTest when the test server cannot be reached."""
    global _reachable
    if _reachable is None:
        try:
            with database.Database(TEST_DATABASE_URL, connect_timeout=3).connect() as db:
                db.execute('SELECT 1')
            _reachable = True
        except database.Error:
            _reachable = False
    if not _reachable:
        raise unittest.SkipTest(SKIP_MESSAGE)


def create_schema(migrate=True):
    """Create a throwaway schema; return (name, Database bound to it)."""
    require_postgres()
    name = 'trace_test_' + uuid.uuid4().hex
    with database.Database(TEST_DATABASE_URL).connect() as db:
        db.execute(f'CREATE SCHEMA {name}')
    bound = database.Database(TEST_DATABASE_URL, search_path=(name, 'public'))
    if migrate:
        database.migrate(bound)
    return name, bound


def drop_schema(name):
    if not name.startswith('trace_test_') or not name.isidentifier():
        raise ValueError('Refusing to drop a schema that is not a test schema.')
    with database.Database(TEST_DATABASE_URL).connect() as db:
        db.execute(f'DROP SCHEMA IF EXISTS {name} CASCADE')


def truncate_all(bound, schema):
    with bound.connect() as db:
        tables = [r[0] for r in db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=? AND tablename<>'schema_migrations'", (schema,))]
        if tables:
            db.execute('TRUNCATE ' + ','.join(f'{schema}.{t}' for t in tables) + ' CASCADE')


class PostgresTestCase(unittest.TestCase):
    schema = None
    database = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema, cls.database = create_schema()

    @classmethod
    def tearDownClass(cls):
        if cls.schema:
            drop_schema(cls.schema)
            cls.schema = cls.database = None
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        truncate_all(self.database, self.schema)


SERVER_GLOBALS = ('CATALOG_STORE', 'SOCIAL_STORE', 'RATINGS_STORE', 'LIBRARY_STORE',
                  'SCORE_SERVICE', 'AUTH_BUDGET')


def bind_server(case, bound):
    """Point server.py's lazily built Database, stores and budgets at ``bound``
    for one test; everything is rebuilt on first use and restored on cleanup."""
    from unittest.mock import patch
    import server
    for name, value in (('DATABASE', bound),) + tuple((name, None) for name in SERVER_GLOBALS):
        patcher = patch.object(server, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)
