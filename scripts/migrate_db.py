#!/usr/bin/env python3
"""Apply Trace's Postgres migrations (migrations/*.sql) once each.

Uses the direct (unpooled) URL: --database-url, else TRACE_DATABASE_URL,
DATABASE_URL_UNPOOLED, then DATABASE_URL. Advisory-locked and idempotent, so
running it twice, or from two shells at once, is safe. The URL is never
printed. --schema targets (and creates) a non-public schema, e.g. a scratch
schema for checks; the citext extension always lives in public.
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database  # noqa: E402


def database_url(environ):
    for name in ('TRACE_DATABASE_URL', 'DATABASE_URL_UNPOOLED', 'DATABASE_URL'):
        if environ.get(name):
            return environ[name]
    return None


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description='Apply pending Trace Postgres migrations.')
    parser.add_argument('--database-url', help='direct Postgres URL (default: TRACE_DATABASE_URL, DATABASE_URL_UNPOOLED, DATABASE_URL)')
    parser.add_argument('--schema', help='apply into this schema instead of the default search_path')
    args = parser.parse_args(argv)
    url = args.database_url or database_url(environ)
    if not url:
        parser.error('set TRACE_DATABASE_URL, DATABASE_URL_UNPOOLED or DATABASE_URL (or pass --database-url)')
    if args.schema is not None and not re.match(r'^[a-z_][a-z0-9_]*$', args.schema):
        parser.error('--schema must be a lowercase plain identifier')
    try:
        if args.schema:
            with database.Database(url).connect() as db:
                db.execute(f'CREATE SCHEMA IF NOT EXISTS {args.schema}')
            target = database.Database(url, search_path=(args.schema, 'public'), statement_timeout='60s')
        else:
            target = database.Database(url, statement_timeout='60s')
        applied = database.migrate(target)
    except database.Error as error:
        print(f'Migration failed: {type(error).__name__}: {error.sqlstate or "connection error"}', file=sys.stderr)
        return 1
    print('Applied: ' + ', '.join(applied) if applied else 'Schema is up to date.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
