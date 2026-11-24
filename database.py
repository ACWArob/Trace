"""Thin Postgres access layer for Trace (psycopg 3).

Every store uses one short-lived connection per unit of work.  Public API
(stable; Steps 2-5 build on it):

``Database(dsn, search_path=None, *, statement_timeout='8s', connect_timeout=5)``
    Holds connection settings only; no connection is opened until ``connect``.
    ``search_path`` (tests only; a str or a sequence of schema names) is sent as
    a startup option, which Neon's pooled URL does not support, so production
    leaves it ``None``.  ``Database.from_env()`` reads ``TRACE_DATABASE_URL``,
    then ``DATABASE_URL``.

``with database.connect(isolation=None) as db:``
    Opens a connection, starts a transaction with
    ``SET LOCAL statement_timeout``, commits when the block exits normally,
    rolls back when it raises, and always closes.  ``isolation`` is ``None``
    (server default, read committed), ``'read committed'``,
    ``'repeatable read'`` or ``'serializable'``.  ``db`` is a ``Connection``:

    ``db.execute(sql, params=())`` -> psycopg cursor (``fetchone``,
        ``fetchall``, iteration, ``rowcount``).  SQL uses SQLite-style ``?``
        placeholders; they become ``%s`` and literal ``%`` is escaped, so
        ``LIKE '50%'`` survives.  ``?`` inside '...' literals, "..."
        identifiers and comments is untouched (write the jsonb ``?``
        operator as ``jsonb_exists(...)``).  With no params the SQL is sent
        verbatim, which also allows several ``;``-separated statements.
    ``db.executemany(sql, seq_of_params)``
    ``db.commit()`` / ``db.rollback()`` end the current transaction and start
        a fresh one (statement timeout re-applied); the context manager still
        commits/rolls back the rest.
    ``db.closed`` -> bool.

``Row``
    Every fetched row: a tuple subclass, so ``row[0]``, unpacking and
    ``tuple(row)`` work, plus ``row['name']``, ``row.keys()`` and
    ``dict(row)`` (like ``sqlite3.Row``).  An unknown name raises IndexError.

``lock(db, name)``
    ``pg_advisory_xact_lock(hashtextextended(name, 0))``: blocks until no other
    transaction holds ``name``; released automatically at commit/rollback.
    Transaction-scoped, so it is safe on Neon's transaction pooler.  Use it in
    place of SQLite's ``BEGIN IMMEDIATE`` for global writers (e.g.
    ``lock(db, 'catalog-write')``).  Per-user serialization should use
    ``SELECT ... FOR UPDATE`` on that user's row instead.

``Error`` (= ``psycopg.Error``), ``IntegrityError`` (= ``psycopg.IntegrityError``;
    unique, foreign-key, check and not-null violations).  Catch
    ``database.Error`` where code caught ``sqlite3.Error``; connection
    failures are ``database.Error`` too.

jsonb / json columns load as ``str`` (a text loader is registered on every
connection), so ``json.loads(row['payload'])`` is unchanged.  Write them by
passing ``json.dumps(...)`` strings.  Note jsonb rejects ``\\u0000``.

``migrate(database, directory=MIGRATIONS_DIR)`` -> list of applied versions
    The single migration runner (also behind ``scripts/migrate_db.py``).
    Applies pending ``migrations/NNN_name.sql`` files in order inside one
    transaction holding the ``trace-migrations`` advisory lock and records
    each in ``schema_migrations``; running it again is a no-op.  No store runs
    DDL at start-up.

Schema notes (see migrations/): timestamps and scores are DOUBLE PRECISION
epoch seconds; revisions/cursors/votes are BIGINT; ``users.username`` is
``citext`` (case-insensitive equality and uniqueness, original case kept);
``reviews.spoiler`` is BOOLEAN; salts/hashes are BYTEA (returned as bytes);
``catalog.payload``, ``catalog.genres`` and ``reviews.item`` are JSONB; other
JSON-bearing columns (``catalog_queries.payload``,
``provider_score_cache.sources_json``, ``private_library_entries.entry``) stay
TEXT so stored strings round-trip byte for byte.
"""

import os
import re
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import psycopg
from psycopg.types.string import TextLoader

__all__ = ['Database', 'Connection', 'Row', 'Error', 'IntegrityError', 'lock', 'migrate', 'translate', 'MIGRATIONS_DIR']

Error = psycopg.Error
IntegrityError = psycopg.IntegrityError

MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'
MIGRATION_LOCK = 'trace-migrations'
ISOLATION_LEVELS = {
    'read committed': psycopg.IsolationLevel.READ_COMMITTED,
    'repeatable read': psycopg.IsolationLevel.REPEATABLE_READ,
    'serializable': psycopg.IsolationLevel.SERIALIZABLE,
}
_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_TIMEOUT = re.compile(r'^[0-9]+(ms|s|min)?$')
# Quoted strings, quoted identifiers and comments are copied as-is (except
# that % is doubled); a bare ? becomes %s; a bare % becomes %%.
_TOKENS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/|\?|%", re.S)


@lru_cache(maxsize=1024)
def translate(sql):
    """Convert SQLite ``?`` placeholders to psycopg ``%s`` and escape ``%``."""
    def replace(match):
        token = match.group(0)
        if token == '?':
            return '%s'
        return token.replace('%', '%%')
    return _TOKENS.sub(replace, sql)


class Row(tuple):
    __slots__ = ()
    _names = ()

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                key = self._names.index(key)
            except ValueError:
                raise IndexError(f'No column named {key!r}') from None
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    if cursor.description is None:
        return tuple
    names = tuple(column.name for column in cursor.description)
    row_type = type('Row', (Row,), {'__slots__': (), '_names': names})
    return lambda values: tuple.__new__(row_type, values)


class Connection:
    def __init__(self, raw, statement_timeout):
        self._raw = raw
        self._statement_timeout = statement_timeout

    def _begin(self):
        self._raw.execute(f"SET LOCAL statement_timeout = '{self._statement_timeout}'")

    def execute(self, sql, params=()):
        if not params:
            return self._raw.execute(sql)
        return self._raw.execute(translate(sql), params)

    def executemany(self, sql, seq_of_params):
        cursor = self._raw.cursor()
        cursor.executemany(translate(sql), seq_of_params)
        return cursor

    def commit(self):
        self._raw.commit()
        self._begin()

    def rollback(self):
        self._raw.rollback()
        self._begin()

    @property
    def closed(self):
        return self._raw.closed


class Database:
    def __init__(self, dsn, search_path=None, *, statement_timeout='8s', connect_timeout=5):
        if not dsn:
            raise ValueError('A Postgres connection URL is required.')
        if not _TIMEOUT.match(str(statement_timeout)):
            raise ValueError('statement_timeout must look like 8s, 500ms or 1min.')
        if isinstance(search_path, str):
            search_path = [part.strip() for part in search_path.split(',')]
        if search_path is not None:
            search_path = tuple(search_path)
            if not search_path or not all(_IDENTIFIER.match(part) for part in search_path):
                raise ValueError('search_path must be a list of plain schema names.')
        self.dsn = dsn
        self.search_path = search_path
        self.statement_timeout = str(statement_timeout)
        self.connect_timeout = connect_timeout

    @classmethod
    def from_env(cls, environ=None, **kwargs):
        environ = os.environ if environ is None else environ
        return cls(environ.get('TRACE_DATABASE_URL') or environ.get('DATABASE_URL'), **kwargs)

    @contextmanager
    def connect(self, isolation=None):
        if isolation is not None and isolation not in ISOLATION_LEVELS:
            raise ValueError('isolation must be read committed, repeatable read or serializable.')
        options = {}
        if self.search_path:
            options['options'] = '-c search_path=' + ','.join(self.search_path)
        raw = psycopg.connect(self.dsn, autocommit=False, prepare_threshold=None,
                              connect_timeout=self.connect_timeout, row_factory=_row_factory, **options)
        try:
            raw.adapters.register_loader('jsonb', TextLoader)
            raw.adapters.register_loader('json', TextLoader)
            if isolation is not None:
                raw.isolation_level = ISOLATION_LEVELS[isolation]
            db = Connection(raw, self.statement_timeout)
            db._begin()
            try:
                yield db
            except BaseException:
                if not raw.closed:
                    try:
                        raw.rollback()
                    except Error:
                        pass  # keep the original exception; close() discards the transaction
                raise
            else:
                raw.commit()
        finally:
            raw.close()


def lock(db, name):
    """Take a transaction-scoped advisory lock named ``name``."""
    db.execute('SELECT pg_advisory_xact_lock(hashtextextended(?, 0))', (name,))


def migrate(database, directory=MIGRATIONS_DIR):
    """Apply pending migrations; return the versions applied (empty if none)."""
    files = sorted(Path(directory).glob('[0-9][0-9][0-9]_*.sql'))
    applied = []
    with database.connect() as db:
        lock(db, MIGRATION_LOCK)
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations ('
                   'version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
        done = {row[0] for row in db.execute('SELECT version FROM schema_migrations')}
        for path in files:
            if path.stem in done:
                continue
            db.execute(path.read_text(encoding='utf-8'))
            db.execute('INSERT INTO schema_migrations(version) VALUES(?)', (path.stem,))
            applied.append(path.stem)
    return applied
