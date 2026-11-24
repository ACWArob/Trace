import base64
from hashlib import sha256
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import production
import server
from library_store import STORAGE_FULL
from tests.pg_support import PostgresTestCase, bind_server


JPEG = b'\xff\xd8\xff\xe0' + b'synthetic jpeg body'
PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic png body'
WEBP = b'RIFF\x10\x00\x00\x00WEBPVP8 ' + b'synthetic webp body'


class QuietHandler(server.Handler):
    def log_message(self, *args):
        pass


class LibraryHTTPTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        bind_server(self, self.database)
        social = server.get_social()
        self.accounts = []
        for username in ['http_alice', 'http_bob']:
            session, token = social.register({'username': username, 'displayName': username, 'password': 'synthetic-password-for-tests'})
            self.accounts.append((session, token))
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_server)
        self.origin = 'http://127.0.0.1:' + str(self.http.server_port)
        photos = tempfile.TemporaryDirectory()
        self.addCleanup(photos.cleanup)
        self.photo_dir = photos.name
        # patch.dict restores the whole environment on stop, including the removed Blob keys.
        env = patch.dict('os.environ', {'TRACE_PHOTO_DIR': photos.name})
        env.start()
        self.addCleanup(env.stop)
        for name in ('BLOB_STORE_ID', 'VERCEL_OIDC_TOKEN', 'BLOB_READ_WRITE_TOKEN', 'TRACE_PHOTO_BYTES', 'TRACE_PHOTO_BYTES_TOTAL'):
            os.environ.pop(name, None)

    def stop_server(self):
        self.http.shutdown(); self.http.server_close()

    def request(self, method, path, payload=None, account=None, origin=None, csrf=True, host=None):
        headers = {}
        if host: headers['Host'] = host
        if account is not None:
            session, token = self.accounts[account]
            headers['Cookie'] = 'trace_session=' + token
            if csrf: headers['X-Trace-CSRF'] = session['csrf']
        if payload is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': origin or self.origin})
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        status, response_headers, raw = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, json.loads(raw) if response_headers.get('Content-Type', '').startswith('application/json') else None

    def test_private_library_requires_session_origin_and_csrf(self):
        payload = {'changes': []}
        self.assertEqual(self.request('GET', '/api/library')[0], 401)
        self.assertEqual(self.request('POST', '/api/library/sync', payload)[0], 401)
        self.assertEqual(self.request('POST', '/api/library/sync', payload, 0, origin='https://untrusted.example')[0], 403)
        self.assertEqual(self.request('POST', '/api/library/sync', payload, 0, csrf=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/library/sync', payload, 0)[0], 200)
        status, headers, _ = self.request('GET', '/api/library', account=0)
        self.assertEqual(status, 200); self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request('GET', '/api/library?after=not-a-number', account=0)[0], 400)
        self.assertEqual(self.request('POST', '/api/library/sync', {'changes': 'bad'}, 0)[0], 400)

    def test_https_reverse_proxy_uses_configured_origin_and_secure_cookie(self):
        with patch.dict('os.environ',{'TRACE_PUBLIC_ORIGIN':'https://trace.example'}):
            self.assertEqual(self.request('POST','/api/library/sync',{'changes':[]},0,origin='https://trace.example',host='trace.example')[0],200)
            self.assertEqual(self.request('POST','/api/library/sync',{'changes':[]},0,origin='http://trace.example',host='trace.example')[0],403)
            status,headers,_=self.request('POST','/api/auth/login',{'username':'http_alice','password':'synthetic-password-for-tests'},origin='https://trace.example',host='trace.example')
            self.assertEqual(status,200);self.assertIn('; Secure',headers['Set-Cookie'])

    def test_account_id_is_derived_from_session_and_never_payload(self):
        item = {'id': 'same-id', 'title': 'Synthetic private entry', 'type': 'movie', 'notes': 'Synthetic private note'}
        payload = {'changes': [{'id': item['id'], 'entry': item, 'baseRevision': 0}], 'user_id': self.accounts[1][0]['user']['id']}
        self.assertEqual(self.request('POST', '/api/library/sync', payload, 0)[0], 200)
        self.assertEqual(self.request('GET', '/api/library', account=0)[2]['changes'][0]['entry'], item)
        self.assertEqual(self.request('GET', '/api/library', account=1)[2]['changes'], [])
        bob = {**item, 'notes': 'Different synthetic note'}
        payload['changes'][0]['entry'] = bob
        self.assertEqual(self.request('POST', '/api/library/sync', payload, 1)[0], 200)
        self.assertEqual(self.request('GET', '/api/library', account=0)[2]['changes'][0]['entry'], item)
        self.assertEqual(self.request('GET', '/api/library', account=1)[2]['changes'][0]['entry'], bob)
        self.assertEqual(server.get_social().feed()['items'], [])

    def photo(self, body, content_type='image/jpeg', account=0, photo_id=None, origin=None, csrf=True, path='/api/photos'):
        headers = {'Content-Type': content_type, 'Origin': origin or self.origin,
                   'X-Trace-Photo-Id': sha256(body).hexdigest() if photo_id is None else photo_id}
        if account is not None:
            session, token = self.accounts[account]
            headers['Cookie'] = 'trace_session=' + token
            if csrf: headers['X-Trace-CSRF'] = session['csrf']
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        connection.request('POST', path, body, headers)
        response = connection.getresponse()
        status, raw = response.status, response.read()
        connection.close()
        return status, json.loads(raw)

    def native_photo(self, data, content_type='image/jpeg', account=0, photo_id=None, encoded=None):
        body = json.dumps({'contentType': content_type, 'data': base64.b64encode(data).decode() if encoded is None else encoded}).encode()
        return self.photo(body, 'application/json', account, sha256(data).hexdigest() if photo_id is None else photo_id)

    def get_photo(self, photo_id, account=0, query=''):
        headers = {}
        if account is not None:
            headers['Cookie'] = 'trace_session=' + self.accounts[account][1]
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        connection.request('GET', '/api/photos/' + photo_id + query, None, headers)
        response = connection.getresponse()
        status, headers, raw = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, headers, raw

    def photo_rows(self):
        with self.database.connect() as db:
            return [tuple(row) for row in db.execute('SELECT user_id,id FROM photos')]

    def test_photo_upload_and_owner_read(self):
        status, body = self.photo(JPEG)
        self.assertEqual((status, body), (200, {'id': sha256(JPEG).hexdigest()}))
        status, headers, raw = self.get_photo(body['id'])
        self.assertEqual((status, raw), (200, JPEG))
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        self.assertEqual(headers['Cache-Control'], 'private, max-age=86400')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        status, headers, raw = self.get_photo(body['id'], query='?encoding=base64')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {'contentType': 'image/jpeg', 'data': base64.b64encode(JPEG).decode()})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        status, body = self.photo(PNG, 'image/png; charset=binary')
        self.assertEqual(status, 200)
        self.assertEqual(self.get_photo(body['id'])[1]['Content-Type'], 'image/png')

    def test_photo_reupload_is_idempotent(self):
        self.assertEqual(self.photo(JPEG)[0], 200)
        self.assertEqual(self.photo(JPEG), (200, {'id': sha256(JPEG).hexdigest()}))
        self.assertEqual(self.native_photo(JPEG)[0], 200)
        self.assertEqual(len(self.photo_rows()), 1)

    def test_native_base64_upload_runs_the_same_checks(self):
        status, body = self.native_photo(WEBP, 'image/webp')
        self.assertEqual((status, body), (200, {'id': sha256(WEBP).hexdigest()}))
        self.assertEqual(self.get_photo(body['id'])[2], WEBP)
        self.assertEqual(self.native_photo(JPEG, 'image/png')[0], 400)        # magic bytes
        self.assertEqual(self.native_photo(JPEG, photo_id=sha256(PNG).hexdigest())[0], 400)  # hash
        self.assertEqual(self.native_photo(JPEG, encoded='%%%')[0], 400)      # base64
        self.assertEqual(self.native_photo(JPEG, 'image/gif')[0], 415)
        self.assertEqual(self.photo(b'{"contentType":"image/jpeg"', 'application/json')[0], 400)
        with patch.dict('os.environ', {'TRACE_PHOTO_BYTES': '1000'}):
            self.assertEqual(self.native_photo(b'\xff\xd8\xff' + b'x' * 1000)[0], 413)
        self.assertEqual(len(self.photo_rows()), 1)

    def test_photo_rejections(self):
        self.assertEqual(self.photo(PNG, 'image/jpeg')[0], 400)                        # magic bytes
        self.assertEqual(self.photo(JPEG, photo_id=sha256(PNG).hexdigest())[0], 400)   # hash mismatch
        self.assertEqual(self.photo(JPEG, photo_id='abc')[0], 400)                     # malformed id
        self.assertEqual(self.photo(JPEG, photo_id=sha256(JPEG).hexdigest().upper())[0], 400)
        self.assertEqual(self.photo(JPEG, 'image/gif')[0], 415)
        self.assertEqual(self.photo(JPEG, 'text/plain')[0], 415)
        with patch.dict('os.environ', {'TRACE_PHOTO_BYTES': '1000'}):
            self.assertEqual(self.photo(b'\xff\xd8\xff' + b'x' * 1000)[0], 413)
        self.assertEqual(self.photo_rows(), [])

    def test_raw_five_megabyte_photo_is_too_large(self):
        body = b'\xff\xd8\xff' + b'x' * (5 * 1024 * 1024)
        session, token = self.accounts[0]
        environ = {'REQUEST_METHOD': 'POST', 'PATH_INFO': '/api/photos', 'QUERY_STRING': '', 'SCRIPT_NAME': '',
                   'SERVER_NAME': 'localhost', 'SERVER_PORT': '5173', 'SERVER_PROTOCOL': 'HTTP/1.1',
                   'HTTP_HOST': 'localhost:5173', 'REMOTE_ADDR': '127.0.0.1', 'wsgi.url_scheme': 'http',
                   'CONTENT_LENGTH': str(len(body)), 'CONTENT_TYPE': 'image/jpeg', 'wsgi.input': io.BytesIO(body),
                   'HTTP_ORIGIN': 'http://localhost:5173', 'HTTP_COOKIE': 'trace_session=' + token,
                   'HTTP_X_TRACE_CSRF': session['csrf'], 'HTTP_X_TRACE_PHOTO_ID': sha256(body).hexdigest()}
        statuses = []
        b''.join(production.application(environ, lambda status, headers, exc_info=None: statuses.append(status)))
        self.assertEqual(int(statuses[0].split()[0]), 413)
        self.assertEqual(self.photo_rows(), [])

    def test_photo_requires_session_origin_and_csrf(self):
        self.assertEqual(self.photo(JPEG, account=None)[0], 401)
        self.assertEqual(self.photo(JPEG, origin='https://untrusted.example')[0], 403)
        self.assertEqual(self.photo(JPEG, csrf=False)[0], 403)
        self.assertEqual(self.photo_rows(), [])
        photo_id = self.photo(JPEG)[1]['id']
        self.assertEqual(self.get_photo(photo_id, account=None)[0], 401)

    def test_other_accounts_and_unknown_photos_are_not_found(self):
        photo_id = self.photo(JPEG)[1]['id']
        missing = self.get_photo(photo_id, account=1)
        self.assertEqual(missing[0], 404)
        self.assertEqual(missing, self.get_photo(sha256(b'unknown').hexdigest(), account=1))
        self.assertEqual(self.get_photo(photo_id, account=1, query='?encoding=base64')[0], 404)
        self.assertEqual(self.get_photo('not-a-photo-id')[0], 404)

    def test_photo_quota_uses_storage_full_wording(self):
        with patch.dict('os.environ', {'TRACE_PHOTO_BYTES_TOTAL': str(len(JPEG) + 1)}):
            self.assertEqual(self.photo(JPEG)[0], 200)
            self.assertEqual(self.photo(PNG, 'image/png'), (400, {'error': STORAGE_FULL}))
            self.assertEqual(self.photo(JPEG)[0], 200)

    def test_library_accepts_photo_refs_in_entry_and_attachments(self):
        ref = 'trace-photo:' + sha256(JPEG).hexdigest()
        item = {'id': 'memory-1', 'title': 'Synthetic memory', 'type': 'memory', 'image': ref,
                'attachments': [{'type': 'movie', 'title': 'Synthetic film', 'image': ref}]}
        status, _, body = self.request('POST', '/api/library/sync', {'changes': [{'id': 'memory-1', 'entry': item, 'baseRevision': 0}]}, 0)
        self.assertEqual((status, body['accepted'][0]['entry']), (200, item))
        for bad in ['trace-photo:' + sha256(JPEG).hexdigest().upper(), 'trace-photo:abc', 'trace-photo:' + 'a' * 65]:
            bad_item = {**item, 'id': 'memory-2', 'image': bad, 'attachments': []}
            self.assertEqual(self.request('POST', '/api/library/sync', {'changes': [{'id': 'memory-2', 'entry': bad_item, 'baseRevision': 0}]}, 0)[0], 400, bad)
            bad_item = {**item, 'id': 'memory-2', 'attachments': [{'type': 'movie', 'title': 'Synthetic film', 'image': bad}]}
            self.assertEqual(self.request('POST', '/api/library/sync', {'changes': [{'id': 'memory-2', 'entry': bad_item, 'baseRevision': 0}]}, 0)[0], 400, bad)

    def test_private_files_are_not_static_assets_and_mobile_shell_is_served(self):
        for path in ['/.trace/mdblist-api-key', '/.trace/trace.sqlite3', '/library_store.py', '/.env']:
            self.assertEqual(self.request('GET', path)[0], 404)
        for path in ['/manifest.webmanifest', '/sw.js', '/mobile.js', '/api_client.mjs', '/artwork.mjs', '/library_sync.mjs', '/assets/trace-app-192.png']:
            status,headers,_=self.request('GET',path)
            self.assertEqual(status,200);self.assertIn('public',headers['Cache-Control']);self.assertNotIn('no-store',headers['Cache-Control'])


if __name__ == '__main__':
    unittest.main()
