import io
import json
from contextlib import redirect_stdout
import tempfile
import unittest
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import production
import server
from tests.pg_support import PostgresTestCase, bind_server


def request(path='/', method='GET', body=None, headers=None, validate=True):
    environ = {}
    setup_testing_defaults(environ)
    route, _, query = path.partition('?')
    raw = json.dumps(body).encode() if body is not None else b''
    environ.update(REQUEST_METHOD=method, PATH_INFO=route, QUERY_STRING=query,
                   HTTP_HOST='localhost:5173', REMOTE_ADDR='127.0.0.1',
                   CONTENT_LENGTH=str(len(raw)), CONTENT_TYPE='application/json',
                   **{'wsgi.input': io.BytesIO(raw)})
    environ.update(headers or {})
    response = {}

    def start_response(status, response_headers, exc_info=None):
        response['status'] = int(status.split()[0])
        response['headers'] = response_headers

    app = validator(production.application) if validate else production.application
    iterator = app(environ, start_response)
    try:
        response['body'] = b''.join(iterator)
    finally:
        if hasattr(iterator, 'close'):
            iterator.close()
    return response


class ProductionAdapterTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        bind_server(self, self.database)
        self.env = patch.dict('os.environ', {'TRACE_ENV': 'development',
                                            'TRACE_PUBLIC_ORIGIN': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_get_and_head_use_identical_public_allowlist(self):
        public = request('/core.mjs')
        head = request('/core.mjs', 'HEAD')
        self.assertEqual(public['status'], 200)
        self.assertIn(b'export ', public['body'])
        self.assertEqual(head['status'], 200)
        self.assertEqual(head['body'], b'')
        self.assertEqual(dict(head['headers'])['Content-Length'], str(len(public['body'])))
        for path in ['/server.py', '/.trace/catalog.sqlite3', '/.git/config',
                     '/assets/../server.py', '/assets/%2e%2e/server.py']:
            self.assertEqual(request(path)['status'], 404, path)
            self.assertEqual(request(path, 'HEAD')['status'], 404, path)

    def test_json_post_and_cookie_headers_preserve_authentication(self):
        result = request('/api/auth/signup', 'POST',
                         {'username': 'wsgi_test', 'password': 'example-password-123'},
                         {'HTTP_ORIGIN': 'http://localhost:5173'})
        self.assertEqual(result['status'], 201)
        cookie = dict(result['headers'])['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        session = request('/api/auth/session', headers={'HTTP_COOKIE': cookie.split(';', 1)[0]})
        self.assertEqual(session['status'], 200)
        self.assertEqual(json.loads(session['body'])['user']['username'], 'wsgi_test')
        self.assertEqual(dict(session['headers'])['Cache-Control'], 'no-store')

    def test_wrong_origin_and_host_stay_rejected(self):
        result = request('/api/auth/signup', 'POST',
                         {'username': 'wsgi_test', 'password': 'example-password-123'},
                         {'HTTP_ORIGIN': 'https://other.example'})
        self.assertEqual(result['status'], 403)
        self.assertEqual(request('/', headers={'HTTP_HOST': 'other.example'})['status'], 403)

    def test_limits_and_unsupported_methods_are_rejected_before_dispatch(self):
        self.assertEqual(request('/', 'PUT')['status'], 405)
        self.assertEqual(production.MAX_BODY_BYTES, 4_100_000)
        self.assertEqual(request('/', headers={'CONTENT_LENGTH': '4100001'})['status'], 413)
        self.assertEqual(request('/', headers={'CONTENT_LENGTH': '4100000'}, validate=False)['status'], 200)
        self.assertEqual(request('/', headers={'CONTENT_LENGTH': '-1'}, validate=False)['status'], 400)
        self.assertEqual(request('/', headers={'SCRIPT_NAME': '/trace'})['status'], 400)

    def test_runtime_requires_hosted_origin_and_persistent_data(self):
        with self.assertRaises(SystemExit):
            production.run()
        with patch.dict('os.environ', {'TRACE_PUBLIC_ORIGIN': 'https://trace.example',
                                      'TRACE_ENV': 'production', 'TRACE_TRUST_PROXY': '1',
                                      'TRACE_DATA_DIR': ''}):
            with self.assertRaises(SystemExit):
                production.run()

    def test_vercel_runtime_needs_no_data_directory_or_waitress(self):
        output = io.StringIO()
        vercel = {'TRACE_PUBLIC_ORIGIN': 'https://trace.example', 'TRACE_ENV': 'production',
                  'TRACE_TRUST_PROXY': 'vercel', 'TRACE_DATA_DIR': '',
                  'DATABASE_URL': 'postgresql://user@db.example/trace', 'TRACE_DATABASE_URL': ''}
        with patch.dict('os.environ', vercel), redirect_stdout(output), \
                patch.dict('sys.modules', {'waitress': None}):
            production.run(check=True)
            production.run()
        self.assertIn('configuration is valid', output.getvalue())
        with patch.dict('os.environ', {**vercel, 'DATABASE_URL': ''}), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                production.run(check=True)

    def test_preflight_does_not_open_a_socket_or_database(self):
        output = io.StringIO()
        with patch.dict('os.environ', {'TRACE_PUBLIC_ORIGIN': 'https://trace.example',
                                      'TRACE_ENV': 'production', 'TRACE_TRUST_PROXY': '1',
                                      'TRACE_DATA_DIR': self.directory.name}), \
                patch.object(production, 'load_local_rating_key') as load_key, redirect_stdout(output):
            production.run(check=True)
            load_key.assert_not_called()
        self.assertIn('configuration is valid', output.getvalue())


if __name__ == '__main__':
    unittest.main()
