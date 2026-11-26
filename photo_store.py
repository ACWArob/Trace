"""Private memory photos: validation, storage backends, rows and garbage collection.

A photo's id is the SHA-256 hex of its bytes, so retries are idempotent.  Rows
live in ``photos`` (migrations/006_photos.sql); bytes live in a store with one
interface, ``put(pathname, data, content_type)``, ``get(pathname) -> bytes``
(``PhotoMissing`` when absent) and ``delete(pathname)`` (idempotent):

``BlobPhotoStore``
    A private Vercel Blob store over its HTTPS API (stdlib ``urllib``; the
    official ``vercel-blob`` SDK needs httpx and pydantic).  Wire format from
    the SDK sources and https://vercel.com/docs/vercel-blob/private-storage:
    ``PUT https://vercel.com/api/blob/?pathname=...`` and
    ``POST https://vercel.com/api/blob/delete {"urls": [...]}`` with
    ``x-vercel-blob-store-id``; reads use
    ``GET https://<store>.private.blob.vercel-storage.com/<pathname>``.  Every
    call sends ``Authorization: Bearer <token>``, where the token is the
    function's OIDC token (request header ``x-vercel-oidc-token``, else env
    ``VERCEL_OIDC_TOKEN``) paired with ``BLOB_STORE_ID``, or else a
    ``BLOB_READ_WRITE_TOKEN`` (its store id is embedded in the token).
``LocalPhotoStore``
    Files under a directory (``TRACE_PHOTO_DIR`` or a temporary directory):
    tests and local development.  Never used in production.
"""
import base64
import binascii
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import database
from library_store import STORAGE_FULL
from social import SocialError

PHOTO_TYPES = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}
ID_PATTERN = re.compile(r'[0-9a-f]{64}')
REF_PATTERN = re.compile(r'trace-photo:[0-9a-f]{64}')
PATH_PATTERN = re.compile(r'photos/[A-Za-z0-9_-]{1,100}/[0-9a-f]{64}\.(?:jpg|png|webp)')
DEFAULT_PHOTO_BYTES = 3_000_000
DEFAULT_PHOTO_QUOTA = 100 * 1024 * 1024
GC_GRACE_SECONDS = 7 * 86400
BLOB_API = 'https://vercel.com/api/blob'
BLOB_API_VERSION = '12'  # vercel-blob 0.6.2 (Python SDK); the JS SDK sends 13.
BLOB_TIMEOUT = 20


class PhotoError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class PhotoMissing(LookupError):
    """No such photo for this account (or no such blob)."""


class PhotoStoreError(RuntimeError):
    """The storage backend failed; the request may be retried."""


def _env_int(name, default, minimum, maximum):
    value = int(os.environ.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}.')
    return value


def photo_byte_limit():
    return _env_int('TRACE_PHOTO_BYTES', DEFAULT_PHOTO_BYTES, 1000, DEFAULT_PHOTO_BYTES)


def photo_quota():
    return _env_int('TRACE_PHOTO_BYTES_TOTAL', DEFAULT_PHOTO_QUOTA, 1, 1024 ** 4)


def detect_type(data):
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def decode_native(payload):
    """Native JSON body ``{"contentType", "data": "<plain base64>"}`` -> (type, bytes)."""
    content_type, data = payload.get('contentType'), payload.get('data')
    if not isinstance(content_type, str) or content_type not in PHOTO_TYPES:
        raise PhotoError('Choose a JPEG, PNG, or WebP photo.', 415)
    if not isinstance(data, str):
        raise PhotoError('The photo data is invalid.')
    try:
        return content_type, base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PhotoError('The photo data is invalid.') from exc


def validate_photo(photo_id, content_type, data, max_bytes):
    if content_type not in PHOTO_TYPES:
        raise PhotoError('Choose a JPEG, PNG, or WebP photo.', 415)
    if len(data) > max_bytes:
        raise PhotoError('The photo is too large.', 413)
    if not isinstance(photo_id, str) or not ID_PATTERN.fullmatch(photo_id):
        raise PhotoError('The photo ID is invalid.')
    if detect_type(data) != content_type:
        raise PhotoError('The photo does not match its file type.')
    if sha256(data).hexdigest() != photo_id:
        raise PhotoError('The photo did not arrive intact. Try again.')


def pathname(user_id, photo_id, content_type):
    return f'photos/{user_id}/{photo_id}.{PHOTO_TYPES[content_type]}'


def _check_path(path):
    if not isinstance(path, str) or not PATH_PATTERN.fullmatch(path):
        raise ValueError('Invalid photo pathname.')
    return path


class LocalPhotoStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _file(self, path):
        return self.root / _check_path(path)

    def put(self, path, data, content_type):
        target = self._file(path)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix='.upload-')
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def get(self, path):
        try:
            return self._file(path).read_bytes()
        except FileNotFoundError as exc:
            raise PhotoMissing(path) from exc

    def delete(self, path):
        self._file(path).unlink(missing_ok=True)


class BlobPhotoStore:
    def __init__(self, store_id, token, *, max_bytes=DEFAULT_PHOTO_BYTES, opener=urlopen, timeout=BLOB_TIMEOUT):
        if not store_id or not token:
            raise ValueError('A Blob store id and token are required.')
        self.store_id = store_id.removeprefix('store_')
        self.token = token
        self.max_bytes = max_bytes
        self.opener = opener
        self.timeout = timeout

    def _url(self, path):
        return f'https://{self.store_id.lower()}.private.blob.vercel-storage.com/{quote(_check_path(path))}'

    def _headers(self, **extra):
        return {'Authorization': 'Bearer ' + self.token, 'x-api-version': BLOB_API_VERSION,
                'x-vercel-blob-store-id': self.store_id, **extra}

    def _send(self, request, limit=65536):
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read(limit + 1)
        except HTTPError as exc:
            if exc.code == 404:
                raise PhotoMissing(request.full_url) from exc
            raise PhotoStoreError(f'Blob storage returned {exc.code}.') from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PhotoStoreError('Blob storage is unreachable.') from exc
        if len(body) > limit:
            raise PhotoStoreError('Blob storage returned more data than expected.')
        return body

    def put(self, path, data, content_type):
        query = urlencode({'pathname': _check_path(path)})
        headers = self._headers(**{'x-vercel-blob-access': 'private', 'x-add-random-suffix': '0',
                                   'x-allow-overwrite': '1', 'x-content-type': content_type})
        self._send(Request(f'{BLOB_API}/?{query}', data=data, headers=headers, method='PUT'))

    def get(self, path):
        request = Request(self._url(path), headers={'Authorization': 'Bearer ' + self.token}, method='GET')
        return self._send(request, self.max_bytes)

    def delete(self, path):
        body = json.dumps({'urls': [self._url(path)]}).encode()
        request = Request(f'{BLOB_API}/delete', data=body, method='POST',
                          headers=self._headers(**{'Content-Type': 'application/json'}))
        try:
            self._send(request)
        except PhotoMissing:
            pass


_LOCAL_DIR = None


def store_from_env(request_token=None, environ=None):
    """The photo store for this request, or None when production has no Blob credentials."""
    global _LOCAL_DIR
    environ = os.environ if environ is None else environ
    store_id = environ.get('BLOB_STORE_ID', '')
    oidc = request_token or environ.get('VERCEL_OIDC_TOKEN', '')
    if store_id and oidc:
        return BlobPhotoStore(store_id, oidc)
    rw_token = environ.get('BLOB_READ_WRITE_TOKEN', '')
    parts = rw_token.split('_')
    if rw_token.startswith('vercel_blob_rw_') and len(parts) >= 5 and parts[3]:
        return BlobPhotoStore(parts[3], rw_token)
    if environ.get('TRACE_ENV', '').strip() == 'production':
        return None
    directory = environ.get('TRACE_PHOTO_DIR')
    if not directory:
        _LOCAL_DIR = _LOCAL_DIR or tempfile.mkdtemp(prefix='trace-photos-')
        directory = _LOCAL_DIR
    return LocalPhotoStore(directory)


class PhotoLibrary:
    """Per-account photo rows over a store.  Writers for one account are
    serialized by a transaction-scoped advisory lock; the GC takes the same lock."""

    def __init__(self, database_, store, *, quota=None, max_bytes=None):
        self.database = database_
        self.store = store
        self.quota = photo_quota() if quota is None else quota
        self.max_bytes = photo_byte_limit() if max_bytes is None else max_bytes

    def upload(self, user_id, photo_id, content_type, data):
        validate_photo(photo_id, content_type, data, self.max_bytes)
        path = pathname(user_id, photo_id, content_type) if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(user_id)) else None
        with self.database.connect() as db:
            database.lock(db, 'photos:' + str(user_id))
            if path is None or not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
                raise SocialError('Sign in to save photos.', 401)
            now = time.time()
            # A retry restarts the GC grace period so the entry that will reference it can sync.
            if db.execute('UPDATE photos SET created_at=? WHERE user_id=? AND id=? AND deleted_at IS NULL',
                          (now, user_id, photo_id)).rowcount:
                return {'id': photo_id}
            used = db.execute('SELECT coalesce(sum(bytes),0)::bigint FROM photos WHERE user_id=? AND deleted_at IS NULL',
                              (user_id,)).fetchone()[0]
            if used + len(data) > self.quota:
                raise PhotoError(STORAGE_FULL)
            # The blob is written while this account's lock is held, so the GC
            # cannot delete it between the write and the row insert.
            self.store.put(path, data, content_type)
            db.execute('INSERT INTO photos(user_id,id,pathname,content_type,bytes,created_at) VALUES(?,?,?,?,?,?)',
                       (user_id, photo_id, path, content_type, len(data), now))
        return {'id': photo_id}

    def read(self, user_id, photo_id):
        if not isinstance(photo_id, str) or not ID_PATTERN.fullmatch(photo_id):
            raise PhotoMissing(photo_id)
        with self.database.connect() as db:
            row = db.execute('SELECT pathname,content_type FROM photos WHERE user_id=? AND id=? AND deleted_at IS NULL',
                             (user_id, photo_id)).fetchone()
        if not row:
            raise PhotoMissing(photo_id)
        return row['content_type'], self.store.get(row['pathname'])


# Marked by delete_account, or older than the grace period and not referenced
# by any of the owner's live library entries (image or attachments[].image).
GC_CONDITION = '''(p.deleted_at IS NOT NULL OR (p.created_at < ? AND NOT EXISTS (
    SELECT 1 FROM private_library_entries e WHERE e.user_id=p.user_id AND e.entry IS NOT NULL
    AND strpos(e.entry, 'trace-photo:' || p.id) > 0)))'''


def collect_garbage(database_, store, *, now=None, limit=50, should_stop=lambda: False):
    """Delete up to ``limit`` unreferenced or marked photos: blob first, then row."""
    cutoff = (time.time() if now is None else now) - GC_GRACE_SECONDS
    with database_.connect() as db:
        candidates = db.execute(f'SELECT p.user_id,p.id FROM photos p WHERE {GC_CONDITION} '
                                'ORDER BY p.deleted_at NULLS LAST, p.created_at LIMIT ?', (cutoff, limit)).fetchall()
    deleted = failed = 0
    for user_id, photo_id in candidates:
        if should_stop():
            break
        try:
            with database_.connect() as db:
                database.lock(db, 'photos:' + user_id)
                row = db.execute(f'SELECT p.pathname FROM photos p WHERE p.user_id=? AND p.id=? AND {GC_CONDITION}',
                                 (user_id, photo_id, cutoff)).fetchone()
                if not row:
                    continue
                store.delete(row['pathname'])
                db.execute('DELETE FROM photos WHERE user_id=? AND id=?', (user_id, photo_id))
                deleted += 1
        except (PhotoStoreError, database.Error):
            failed += 1
    return {'deleted': deleted, 'failed': failed}
