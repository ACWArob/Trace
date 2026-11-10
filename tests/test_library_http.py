import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import server
from library_store import LibraryStore


class QuietHandler(server.Handler):
    def log_message(self, *args):
        pass


class LibraryHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'test.sqlite3'
        social = server.SocialStore(path)
        for name, store in [('SOCIAL_STORE', social), ('CATALOG_STORE', server.CatalogStore(path)),
                            ('RATINGS_STORE', server.RatingsStore(path)), ('LIBRARY_STORE', LibraryStore(path))]:
            patcher = patch.object(server, name, store)
            patcher.start(); self.addCleanup(patcher.stop)
        self.accounts = []
        for username in ['http_alice', 'http_bob']:
            session, token = social.register({'username': username, 'displayName': username, 'password': 'synthetic-password-for-tests'})
            self.accounts.append((session, token))
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_server)
        self.origin = 'http://127.0.0.1:' + str(self.http.server_port)

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

    def test_private_files_are_not_static_assets_and_mobile_shell_is_served(self):
        for path in ['/.trace/mdblist-api-key', '/.trace/trace.sqlite3', '/library_store.py', '/.env']:
            self.assertEqual(self.request('GET', path)[0], 404)
        for path in ['/manifest.webmanifest', '/sw.js', '/mobile.js', '/api_client.mjs', '/artwork.mjs', '/library_sync.mjs', '/assets/trace-app-192.png']:
            status,headers,_=self.request('GET',path)
            self.assertEqual(status,200);self.assertIn('public',headers['Cache-Control']);self.assertNotIn('no-store',headers['Cache-Control'])


if __name__ == '__main__':
    unittest.main()
