"""Private, revisioned account libraries. Nothing here publishes social content."""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
import base64
import binascii
import json
import math
import re
import sqlite3

from catalog_store import database_path
from social import SocialError, TYPES

MAX_ENTRIES = 30000
MAX_ENTRY_BYTES = 1500000
MAX_CHANGES = 100
MAX_REVISION = 9007199254740991
ID_PATTERN = re.compile(r'[a-zA-Z0-9_-]{1,100}')


def _invalid(message='The library entry is invalid.'):
    raise SocialError(message)


def _integer(value, minimum=0, maximum=MAX_REVISION):
    return type(value) is int and minimum <= value <= maximum


def _string(value, maximum, nonempty=False):
    if not isinstance(value, str) or len(value) > maximum or (nonempty and not value.strip()):
        _invalid()


def _number(value, minimum, maximum):
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        _invalid()


def _date(value):
    if value == '':
        return
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        _invalid()
    try:
        date.fromisoformat(value)
    except ValueError:
        _invalid()


def _url(value, image=False):
    _string(value, MAX_ENTRY_BYTES if image else 2000)
    if not value:
        return
    if image and value.startswith('data:'):
        if not re.fullmatch(r'data:image/(?:png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=]+', value):
            _invalid('Use a JPEG, PNG, or WebP library image.')
        try:
            base64.b64decode(value.split(',', 1)[1], validate=True)
        except (ValueError, binascii.Error):
            _invalid('The library image is invalid.')
        return
    if image and re.fullmatch(r'/assets/[\w.-]+', value):
        return
    try:
        parsed = urlparse(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                 and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid or len(value) > 2000 or any(ord(c) < 32 for c in value):
        _invalid('The library URL is invalid.')


def _json_tree(value, depth=0):
    """Bound forward-compatible metadata without dropping unknown fields."""
    if depth > 8:
        _invalid('Library metadata is nested too deeply.')
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            _string(value, MAX_ENTRY_BYTES)
        return
    if type(value) in (int, float):
        if abs(value) > MAX_REVISION or not math.isfinite(value):
            _invalid()
        return
    if isinstance(value, list) and len(value) <= 10000:
        for item in value:
            _json_tree(item, depth + 1)
        return
    if isinstance(value, dict) and len(value) <= 100:
        for key, item in value.items():
            _string(key, 100, nonempty=True)
            _json_tree(item, depth + 1)
        return
    _invalid()


def _entry(value, entry_id):
    if not isinstance(value, dict):
        _invalid()
    _json_tree(value)
    if value.get('id') != entry_id:
        _invalid('The library entry ID does not match its change.')
    _string(value.get('title'), 500, nonempty=True)
    if not isinstance(value.get('type'), str) or value['type'] not in TYPES:
        _invalid()
    for key in ('creator', 'year', 'source', 'externalId', 'addedAt', 'imdbId'):
        if key in value:
            _string(value[key], 500)
    for key in ('notes', 'description'):
        if key in value:
            _string(value[key], 20000)
    if 'metadataSource' in value:
        _string(value['metadataSource'], 100)
    if 'status' in value and (not isinstance(value['status'], str) or value['status'] not in ('done', 'want', 'progress')):
        _invalid()
    for key in ('favorite', 'forLater'):
        if key in value and type(value[key]) is not bool:
            _invalid()
    for key in ('rating', 'legacyRating'):
        if key in value:
            _number(value[key], 0, 5)
    if 'legacyType' in value and (not isinstance(value['legacyType'], str) or value['legacyType'] not in ('restaurant', 'place')):
        _invalid()
    if 'date' in value:
        _date(value['date'])
    if 'history' in value:
        if not isinstance(value['history'], list) or len(value['history']) > 10000:
            _invalid()
        for item in value['history']:
            _date(item)
    for key in ('sourceUrl', 'metadataSourceUrl'):
        if key in value:
            _url(value[key])
    if 'image' in value:
        _url(value['image'], image=True)
    if 'genres' in value:
        if not isinstance(value['genres'], list) or len(value['genres']) > 8:
            _invalid()
        for item in value['genres']:
            _string(item, 40, nonempty=True)
    if 'attachments' in value:
        if not isinstance(value['attachments'], list) or len(value['attachments']) > 12:
            _invalid()
        for item in value['attachments']:
            if not isinstance(item, dict) or not isinstance(item.get('type'), str) or item['type'] not in TYPES - {'memory'}:
                _invalid()
            _string(item.get('title'), 300, nonempty=True)
            if 'id' in item and item['id'] != '' and (not isinstance(item['id'], str) or not ID_PATTERN.fullmatch(item['id'])):
                _invalid()
            for key, maximum in (('creator', 300), ('year', 4)):
                if key in item:
                    _string(item[key], maximum)
            if 'image' in item:
                _url(item['image'], image=True)
    if 'publicScores' in value:
        if not isinstance(value['publicScores'], list) or len(value['publicScores']) > 8:
            _invalid()
        for item in value['publicScores']:
            if not isinstance(item, dict):
                _invalid()
            _string(item.get('source'), 60, nonempty=True)
            _number(item.get('max'), 0.000001, 100)
            _number(item.get('value'), 0, item['max'])
            if item.get('count') is not None:
                _number(item['count'], 1, MAX_REVISION)
            if 'url' in item:
                _url(item['url'])
            if 'via' in item:
                _string(item['via'], 60)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        size = len(encoded.encode('utf-8'))
    except (ValueError, TypeError, UnicodeError, OverflowError):
        _invalid()
    if size > MAX_ENTRY_BYTES:
        _invalid('A library entry exceeds the 1.5 MB limit.')
    return encoded


class LibraryStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS private_library_state (
                    user_id TEXT PRIMARY KEY REFERENCES users(id),
                    cursor INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS private_library_entries (
                    user_id TEXT NOT NULL REFERENCES users(id),
                    id TEXT NOT NULL, entry TEXT, revision INTEGER NOT NULL,
                    PRIMARY KEY (user_id, id)
                );
                CREATE INDEX IF NOT EXISTS private_library_revision
                    ON private_library_entries(user_id, revision);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _user(db, user_id):
        if not isinstance(user_id, str) or not user_id or not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            raise SocialError('Sign in to use your account library.', 401)

    @staticmethod
    def _record(row, entry_id=None):
        return {'id': row['id'] if row else entry_id,
                'entry': json.loads(row['entry']) if row and row['entry'] is not None else None,
                'revision': row['revision'] if row else 0}

    def pull(self, user_id, after=0, limit=200):
        if not _integer(after) or not _integer(limit, 1, 200):
            _invalid('Use a valid library cursor and a limit from 1 to 200.')
        with self.db() as db:
            db.execute('BEGIN')
            self._user(db, user_id)
            state = db.execute('SELECT cursor FROM private_library_state WHERE user_id=?', (user_id,)).fetchone()
            cursor = state['cursor'] if state else 0
            if after > cursor:
                _invalid('The library cursor is ahead of this account.')
            rows = db.execute('SELECT id,entry,revision FROM private_library_entries WHERE user_id=? AND revision>? ORDER BY revision LIMIT ?', (user_id, after, limit + 1)).fetchall()
            has_more = len(rows) > limit
            changes = [self._record(row) for row in rows[:limit]]
            total = db.execute('SELECT count(*) FROM private_library_entries WHERE user_id=? AND entry IS NOT NULL', (user_id,)).fetchone()[0]
            return {'changes': changes, 'cursor': changes[-1]['revision'] if has_more else cursor,
                    'hasMore': has_more, 'total': total}

    def sync(self, user_id, changes):
        if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
            _invalid('Send at most 100 library changes at once.')
        validated, seen = [], set()
        for change in changes:
            if not isinstance(change, dict) or set(change) != {'id', 'entry', 'baseRevision'}:
                _invalid('A library change needs an ID, entry, and base revision.')
            entry_id = change['id']
            if not isinstance(entry_id, str) or not ID_PATTERN.fullmatch(entry_id) or entry_id in seen:
                _invalid('Library change IDs must be valid and unique.')
            if not _integer(change['baseRevision']):
                _invalid('The library base revision is invalid.')
            seen.add(entry_id)
            encoded = None if change['entry'] is None else _entry(change['entry'], entry_id)
            validated.append((entry_id, encoded, change['baseRevision']))
        accepted, conflicts = [], []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._user(db, user_id)
            state = db.execute('SELECT cursor FROM private_library_state WHERE user_id=?', (user_id,)).fetchone()
            cursor = state['cursor'] if state else 0
            writes = []
            total = db.execute('SELECT count(*) FROM private_library_entries WHERE user_id=? AND entry IS NOT NULL', (user_id,)).fetchone()[0]
            for entry_id, encoded, base in validated:
                row = db.execute('SELECT id,entry,revision FROM private_library_entries WHERE user_id=? AND id=?', (user_id, entry_id)).fetchone()
                if base != (row['revision'] if row else 0):
                    conflicts.append(self._record(row, entry_id))
                    continue
                total += int(encoded is not None) - int(row is not None and row['entry'] is not None)
                writes.append((entry_id, encoded))
            if total > MAX_ENTRIES:
                _invalid('Your account library exceeds the 30,000-item limit.')
            if cursor + len(writes) > MAX_REVISION:
                _invalid('The account library revision limit has been reached.')
            for entry_id, encoded in writes:
                cursor += 1
                db.execute('INSERT INTO private_library_entries(user_id,id,entry,revision) VALUES(?,?,?,?) ON CONFLICT(user_id,id) DO UPDATE SET entry=excluded.entry,revision=excluded.revision', (user_id, entry_id, encoded, cursor))
                accepted.append({'id': entry_id, 'entry': json.loads(encoded) if encoded is not None else None, 'revision': cursor})
            if writes:
                db.execute('INSERT INTO private_library_state(user_id,cursor) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET cursor=excluded.cursor', (user_id, cursor))
        return {'accepted': accepted, 'conflicts': conflicts}
