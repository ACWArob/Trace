"""Photo storage: validation, the local and Blob backends, rows, quota and GC.

Blob tests never touch the network: an injected opener records each request.
Postgres tests run on a throwaway schema; no real library is touched.
"""
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import maintenance
import photo_store
from photo_store import (BlobPhotoStore, LocalPhotoStore, PhotoError, PhotoLibrary, PhotoMissing,
                         PhotoStoreError, detect_type, store_from_env)
from social import SocialError, SocialStore
from library_store import LibraryStore, STORAGE_FULL
from tests.pg_support import PostgresTestCase

JPEG = b'\xff\xd8\xff\xe0' + b'synthetic jpeg body'
PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic png body'
WEBP = b'RIFF\x10\x00\x00\x00WEBPVP8 ' + b'synthetic webp body'


def digest(data):
    return sha256(data).hexdigest()


class ValidationTests(unittest.TestCase):
    def test_magic_bytes_identify_supported_types_only(self):
        self.assertEqual(detect_type(JPEG), 'image/jpeg')
        self.assertEqual(detect_type(PNG), 'image/png')
        self.assertEqual(detect_type(WEBP), 'image/webp')
        for data in [b'', b'GIF89a....', b'RIFF\x10\x00\x00\x00WAVE', b'<svg/>', b'\xff\xd8']:
            self.assertIsNone(detect_type(data), data)

    def test_limits_come_from_the_environment_with_safe_defaults(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(photo_store.photo_byte_limit(), 3_000_000)
            self.assertEqual(photo_store.photo_quota(), 100 * 1024 * 1024)
        with patch.dict('os.environ', {'TRACE_PHOTO_BYTES': '5000', 'TRACE_PHOTO_BYTES_TOTAL': '9000'}):
            self.assertEqual((photo_store.photo_byte_limit(), photo_store.photo_quota()), (5000, 9000))
        for name, value in [('TRACE_PHOTO_BYTES', '3000001'), ('TRACE_PHOTO_BYTES', '0'), ('TRACE_PHOTO_BYTES_TOTAL', '-1')]:
            with patch.dict('os.environ', {name: value}), self.assertRaises(ValueError):
                photo_store.photo_byte_limit() if name == 'TRACE_PHOTO_BYTES' else photo_store.photo_quota()

    def test_native_payload_decodes_plain_base64_only(self):
        import base64
        encoded = base64.b64encode(JPEG).decode()
        self.assertEqual(photo_store.decode_native({'contentType': 'image/jpeg', 'data': encoded}), ('image/jpeg', JPEG))
        for payload, status in [({'contentType': 'image/gif', 'data': encoded}, 415),
                                ({'contentType': 'image/jpeg', 'data': 'not base64!'}, 400),
                                ({'contentType': 'image/jpeg', 'data': 'data:image/jpeg;base64,' + encoded}, 400),
                                ({'contentType': 'image/jpeg', 'data': 7}, 400),
                                ({'contentType': 'image/jpeg'}, 400)]:
            with self.assertRaises(PhotoError) as caught:
                photo_store.decode_native(payload)
            self.assertEqual(caught.exception.status, status, payload)


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = LocalPhotoStore(self.directory.name)

    def test_put_get_delete_round_trip(self):
        path = 'photos/user1/' + digest(JPEG) + '.jpg'
        self.store.put(path, JPEG, 'image/jpeg')
        self.assertEqual(self.store.get(path), JPEG)
        self.assertEqual((Path(self.directory.name) / path).stat().st_mode & 0o777, 0o600)
        self.store.delete(path)
        with self.assertRaises(PhotoMissing):
            self.store.get(path)
        self.store.delete(path)  # deleting a missing photo is not an error

    def test_pathnames_cannot_escape_the_directory(self):
        for path in ['../x.jpg', 'photos/../../x.jpg', '/etc/passwd', 'photos/u/x.gif', 'photos/u/' + 'A' * 64 + '.jpg']:
            with self.assertRaises(ValueError, msg=path):
                self.store.put(path, JPEG, 'image/jpeg')


class FakeResponse(io.BytesIO):
    def __init__(self, body=b'', status=200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class BlobStoreTests(unittest.TestCase):
    def setUp(self):
        self.requests, self.responses = [], []

    def opener(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0) if self.responses else FakeResponse(b'{}')
        if isinstance(response, Exception):
            raise response
        return response

    def store(self, **kwargs):
        options = {'store_id': 'store_AbC123', 'token': 'oidc-token'}
        options.update(kwargs)
        return BlobPhotoStore(opener=self.opener, **options)

    def header(self, request, name):
        return {key.lower(): value for key, value in request.header_items()}.get(name.lower())

    def test_put_sends_private_overwrite_request_with_oidc_and_store_id(self):
        path = 'photos/user1/' + digest(JPEG) + '.jpg'
        self.store().put(path, JPEG, 'image/jpeg')
        request, timeout = self.requests[0]
        self.assertEqual(request.get_method(), 'PUT')
        self.assertEqual(request.full_url, 'https://vercel.com/api/blob/?pathname=photos%2Fuser1%2F' + digest(JPEG) + '.jpg')
        self.assertEqual(request.data, JPEG)
        for name, value in [('Authorization', 'Bearer oidc-token'), ('x-vercel-blob-store-id', 'AbC123'),
                            ('x-vercel-blob-access', 'private'), ('x-add-random-suffix', '0'),
                            ('x-allow-overwrite', '1'), ('x-content-type', 'image/jpeg'),
                            ('x-api-version', photo_store.BLOB_API_VERSION)]:
            self.assertEqual(self.header(request, name), value, name)
        self.assertLessEqual(timeout, 30)

    def test_get_reads_the_private_url_and_maps_missing_to_photo_missing(self):
        path = 'photos/user1/' + digest(PNG) + '.png'
        self.responses.append(FakeResponse(PNG))
        self.assertEqual(self.store().get(path), PNG)
        request, _ = self.requests[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.full_url, 'https://abc123.private.blob.vercel-storage.com/' + path)
        self.assertEqual(self.header(request, 'Authorization'), 'Bearer oidc-token')
        self.responses.append(HTTPError(request.full_url, 404, 'Not Found', {}, io.BytesIO()))
        with self.assertRaises(PhotoMissing):
            self.store().get(path)
        self.responses.append(HTTPError(request.full_url, 403, 'Forbidden', {}, io.BytesIO()))
        with self.assertRaises(PhotoStoreError):
            self.store().get(path)
        self.responses.append(OSError('network down'))
        with self.assertRaises(PhotoStoreError):
            self.store().get(path)

    def test_get_refuses_an_oversized_blob(self):
        self.responses.append(FakeResponse(b'x' * 3_000_001))
        with self.assertRaises(PhotoStoreError):
            self.store(max_bytes=3_000_000).get('photos/user1/' + digest(JPEG) + '.jpg')

    def test_delete_posts_the_blob_url(self):
        path = 'photos/user1/' + digest(WEBP) + '.webp'
        self.store().delete(path)
        request, _ = self.requests[0]
        self.assertEqual((request.get_method(), request.full_url), ('POST', 'https://vercel.com/api/blob/delete'))
        self.assertEqual(json.loads(request.data), {'urls': ['https://abc123.private.blob.vercel-storage.com/' + path]})
        self.assertEqual(self.header(request, 'Content-Type'), 'application/json')
        self.assertEqual(self.header(request, 'x-vercel-blob-store-id'), 'AbC123')

    def test_credentials_prefer_request_oidc_then_env_oidc_then_read_write_token(self):
        env = {'BLOB_STORE_ID': 'store_Env1', 'VERCEL_OIDC_TOKEN': 'env-oidc', 'BLOB_READ_WRITE_TOKEN': 'vercel_blob_rw_Tok9_secret'}
        store = store_from_env(environ=env, request_token='header-oidc')
        self.assertIsInstance(store, BlobPhotoStore)
        self.assertEqual((store.store_id, store.token), ('Env1', 'header-oidc'))
        store = store_from_env(environ=env)
        self.assertEqual((store.store_id, store.token), ('Env1', 'env-oidc'))
        store = store_from_env(environ={'BLOB_READ_WRITE_TOKEN': 'vercel_blob_rw_Tok9_secret'})
        self.assertEqual((store.store_id, store.token), ('Tok9', 'vercel_blob_rw_Tok9_secret'))
        # An OIDC token without a store id cannot address a store; fall back to the RW token.
        store = store_from_env(environ={'VERCEL_OIDC_TOKEN': 'env-oidc', 'BLOB_READ_WRITE_TOKEN': 'vercel_blob_rw_Tok9_secret'})
        self.assertEqual(store.token, 'vercel_blob_rw_Tok9_secret')

    def test_without_blob_credentials_local_dev_uses_a_directory_and_production_has_none(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_from_env(environ={'TRACE_PHOTO_DIR': directory})
            self.assertIsInstance(store, LocalPhotoStore)
            self.assertEqual(store.root, Path(directory).resolve())
            self.assertIsNone(store_from_env(environ={'TRACE_PHOTO_DIR': directory, 'TRACE_ENV': 'production'}))
            self.assertIsNone(store_from_env(environ={'BLOB_STORE_ID': 'store_x', 'TRACE_ENV': 'production'}))


class PhotoLibraryTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = LocalPhotoStore(self.directory.name)
        self.social = SocialStore(self.database)
        self.users = []
        for name in ['photo_alice', 'photo_bob']:
            session, _ = self.social.register({'username': name, 'password': 'synthetic-password-1'})
            self.users.append(session['user']['id'])
        self.photos = PhotoLibrary(self.database, self.store, quota=10_000, max_bytes=5_000)

    def rows(self):
        with self.database.connect() as db:
            return [dict(row) for row in db.execute('SELECT user_id,id,pathname,content_type,bytes,deleted_at FROM photos ORDER BY user_id,id')]

    def test_upload_stores_one_row_and_blob_and_is_idempotent(self):
        alice = self.users[0]
        self.assertEqual(self.photos.upload(alice, digest(JPEG), 'image/jpeg', JPEG), {'id': digest(JPEG)})
        self.assertEqual(self.photos.upload(alice, digest(JPEG), 'image/jpeg', JPEG), {'id': digest(JPEG)})
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['pathname'], f'photos/{alice}/{digest(JPEG)}.jpg')
        self.assertEqual((rows[0]['content_type'], rows[0]['bytes']), ('image/jpeg', len(JPEG)))
        self.assertEqual(self.photos.read(alice, digest(JPEG)), ('image/jpeg', JPEG))

    def test_upload_rejects_bad_type_size_magic_and_hash(self):
        alice = self.users[0]
        cases = [('image/gif', JPEG, digest(JPEG), 415), ('image/jpeg', b'\xff\xd8\xff' + b'x' * 5_000, digest(b'\xff\xd8\xff' + b'x' * 5_000), 413),
                 ('image/png', JPEG, digest(JPEG), 400), ('image/jpeg', JPEG, digest(PNG), 400),
                 ('image/jpeg', JPEG, digest(JPEG).upper(), 400), ('image/jpeg', b'', digest(b''), 400)]
        for content_type, data, photo_id, status in cases:
            with self.assertRaises(PhotoError, msg=(content_type, status)) as caught:
                self.photos.upload(alice, photo_id, content_type, data)
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(Path(self.directory.name).rglob('*.*')), [])

    def test_quota_counts_live_photos_and_uses_storage_full_wording(self):
        alice, bob = self.users
        photos = PhotoLibrary(self.database, self.store, quota=len(JPEG) + len(PNG), max_bytes=5_000)
        photos.upload(alice, digest(JPEG), 'image/jpeg', JPEG)
        photos.upload(alice, digest(PNG), 'image/png', PNG)
        with self.assertRaises(PhotoError) as caught:
            photos.upload(alice, digest(WEBP), 'image/webp', WEBP)
        self.assertEqual((caught.exception.status, str(caught.exception)), (400, STORAGE_FULL))
        photos.upload(alice, digest(JPEG), 'image/jpeg', JPEG)  # idempotent retry is not new usage
        photos.upload(bob, digest(WEBP), 'image/webp', WEBP)  # quotas are per account
        self.assertEqual(len(self.rows()), 3)

    def test_other_accounts_and_unknown_ids_are_missing(self):
        alice, bob = self.users
        self.photos.upload(alice, digest(JPEG), 'image/jpeg', JPEG)
        for user, photo_id in [(bob, digest(JPEG)), (alice, digest(PNG)), (alice, 'not-an-id')]:
            with self.assertRaises(PhotoMissing):
                self.photos.read(user, photo_id)

    def test_unknown_account_cannot_upload(self):
        with self.assertRaises(SocialError) as caught:
            self.photos.upload('missing-user', digest(JPEG), 'image/jpeg', JPEG)
        self.assertEqual(caught.exception.status, 401)

    def age(self, photo_id, seconds):
        with self.database.connect() as db:
            db.execute('UPDATE photos SET created_at=created_at-? WHERE id=?', (seconds, photo_id))

    def reference(self, user, image=None, attachment=None):
        entry = {'id': 'entry-' + (image or attachment)[-6:], 'title': 'Synthetic memory', 'type': 'memory'}
        if image:
            entry['image'] = image
        if attachment:
            entry['attachments'] = [{'type': 'movie', 'title': 'Synthetic film', 'image': attachment}]
        LibraryStore(self.database).sync(user, [{'id': entry['id'], 'entry': entry, 'baseRevision': 0}])

    def test_gc_keeps_referenced_and_recent_photos_and_removes_the_rest(self):
        alice, bob = self.users
        ids = {}
        for name, data in [('entry', JPEG), ('attached', PNG), ('young', WEBP), ('orphan', JPEG + b'1'), ('bob', JPEG + b'2')]:
            owner = bob if name == 'bob' else alice
            ids[name] = digest(data)
            self.photos.upload(owner, ids[name], detect_type(data), data)
        self.reference(alice, image='trace-photo:' + ids['entry'])
        self.reference(alice, attachment='trace-photo:' + ids['attached'])
        # Alice referencing Bob's id does not keep Bob's photo alive.
        self.reference(alice, image='trace-photo:' + ids['bob'])
        for name in ['entry', 'attached', 'orphan', 'bob']:
            self.age(ids[name], 8 * 86400)
        result = maintenance.collect_photos(self.database, self.store)
        self.assertEqual(result, {'deleted': 2, 'failed': 0})
        remaining = {row['id'] for row in self.rows()}
        self.assertEqual(remaining, {ids['entry'], ids['attached'], ids['young']})
        with self.assertRaises(PhotoMissing):
            self.store.get(f'photos/{alice}/{ids["orphan"]}.jpg')
        self.assertEqual(self.photos.read(alice, ids['entry'])[1], JPEG)

    def test_reupload_restarts_the_grace_period(self):
        alice = self.users[0]
        self.photos.upload(alice, digest(JPEG), 'image/jpeg', JPEG)
        self.age(digest(JPEG), 8 * 86400)
        self.photos.upload(alice, digest(JPEG), 'image/jpeg', JPEG)
        self.assertEqual(maintenance.collect_photos(self.database, self.store), {'deleted': 0, 'failed': 0})

    def test_account_deletion_marks_photos_and_gc_removes_them(self):
        alice, bob = self.users
        self.photos.upload(alice, digest(JPEG), 'image/jpeg', JPEG)
        self.photos.upload(bob, digest(PNG), 'image/png', PNG)
        self.reference(alice, image='trace-photo:' + digest(JPEG))
        self.social.delete_account(alice, 'synthetic-password-1')
        marked = [row for row in self.rows() if row['user_id'] == alice]
        self.assertIsNotNone(marked[0]['deleted_at'])
        self.assertEqual(maintenance.collect_photos(self.database, self.store), {'deleted': 1, 'failed': 0})
        self.assertEqual([row['user_id'] for row in self.rows()], [bob])
        with self.assertRaises(PhotoMissing):
            self.store.get(f'photos/{alice}/{digest(JPEG)}.jpg')

    def test_gc_is_bounded_and_a_failed_blob_delete_keeps_the_row(self):
        alice = self.users[0]
        datas = [JPEG + bytes([n]) for n in range(3)]
        for data in datas:
            self.photos.upload(alice, digest(data), 'image/jpeg', data)
            self.age(digest(data), 8 * 86400)
        self.assertEqual(maintenance.collect_photos(self.database, self.store, limit=2)['deleted'], 2)
        self.assertEqual(len(self.rows()), 1)

        class Failing(LocalPhotoStore):
            def delete(self, pathname):
                raise PhotoStoreError('Blob unavailable')
        self.assertEqual(maintenance.collect_photos(self.database, Failing(self.directory.name)), {'deleted': 0, 'failed': 1})
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(maintenance.collect_photos(self.database, self.store, should_stop=lambda: True), {'deleted': 0, 'failed': 0})

    def test_maintenance_run_includes_photo_gc_only_with_a_store(self):
        self.assertNotIn('photos', maintenance.run(self.database))
        self.assertEqual(maintenance.run(self.database, photo_store=self.store)['photos'], {'deleted': 0, 'failed': 0})


if __name__ == '__main__':
    unittest.main()
