"""Bounded WSGI hosting for Trace, behind the deployment's local HTTPS proxy.

Waitress parses HTTP. This adapter calls Trace's existing route methods directly;
it never passes a reconstructed HTTP request through BaseHTTPRequestHandler.
"""
from email.message import Message
from http import HTTPStatus
import argparse
import logging
import os
import re
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from server import Handler, ROOT, public_origin
from ratings import load_local_rating_key
from release_policy import signup_mode, library_byte_limit

MAX_BODY_BYTES = 10_000_000
HOP_HEADERS = frozenset(('connection', 'keep-alive', 'proxy-authenticate',
                         'proxy-authorization', 'te', 'trailer',
                         'transfer-encoding', 'upgrade'))
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class WsgiHandler(Handler):
    def __init__(self, environ, output):
        self.directory = str(ROOT)
        self.command = environ['REQUEST_METHOD']
        self.request_version = 'HTTP/1.1'
        self.path = quote(environ.get('PATH_INFO', '/').encode('latin-1'), safe='/')
        if environ.get('QUERY_STRING'):
            self.path += '?' + environ['QUERY_STRING']
        self.client_address = (environ.get('REMOTE_ADDR', ''), 0)
        self.headers = Message()
        for name, value in environ.items():
            if name.startswith('HTTP_'):
                header = name[5:].replace('_', '-')
            elif name in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
                header = name.replace('_', '-')
            else:
                continue
            if not isinstance(value, str) or '\r' in value or '\n' in value:
                raise ValueError('Invalid request headers.')
            self.headers[header] = value
        self.rfile = environ['wsgi.input']
        self.wfile = output
        self.response_status = None
        self.response_headers = []
        self._headers_buffer = []

    def send_response(self, code, message=None):
        self.response_status = int(code)
        self.send_header('Date', self.date_time_string())

    def send_response_only(self, code, message=None):
        self.response_status = int(code)

    def send_header(self, keyword, value):
        value = str(value)
        if not HEADER_NAME.fullmatch(keyword) or '\r' in value or '\n' in value:
            raise ValueError('Invalid response header.')
        if keyword.lower() not in HOP_HEADERS and keyword.lower() != 'server':
            self.response_headers.append((keyword, value))

    def end_headers(self):
        # Handler supplies its normal cache and security headers here.
        return super().end_headers()

    def flush_headers(self):
        # WSGI owns serialization and socket writes.
        return None

    def log_message(self, format, *args):
        # Request URLs may contain searches or private identifiers.
        return None


def _small_response(start_response, status, message, extra=()):
    body = message.encode('utf-8')
    start_response(f'{status} {HTTPStatus(status).phrase}', [
        ('Content-Type', 'text/plain; charset=utf-8'),
        ('Content-Length', str(len(body))),
        ('Cache-Control', 'no-store'),
        ('X-Content-Type-Options', 'nosniff'), *extra,
    ])
    return [body]


class FileBody:
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        return self

    def __next__(self):
        if self.stream.closed:
            raise StopIteration
        data = self.stream.read(65536)
        if not data:
            self.close()
            raise StopIteration
        return data

    def close(self):
        self.stream.close()


def application(environ, start_response):
    method = environ.get('REQUEST_METHOD', '')
    if method not in ('GET', 'HEAD', 'POST'):
        return _small_response(start_response, 405, 'Method not allowed.',
                               [('Allow', 'GET, HEAD, POST')])
    try:
        raw_length = environ.get('CONTENT_LENGTH', '')
        if raw_length and not re.fullmatch(r'[0-9]{1,12}', raw_length):
            return _small_response(start_response, 400, 'Invalid request size.')
        if int(raw_length or 0) > MAX_BODY_BYTES:
            return _small_response(start_response, 413, 'Request is too large.')
        if environ.get('SCRIPT_NAME'):
            return _small_response(start_response, 400, 'Trace must be hosted at the origin root.')
        output = SpooledTemporaryFile(max_size=1_000_000, mode='w+b')
        try:
            handler = WsgiHandler(environ, output)
            # Dispatch HEAD through the same allowlist as GET. Inherited
            # SimpleHTTPRequestHandler.do_HEAD bypasses Trace's public allowlist.
            if method in ('GET', 'HEAD'):
                handler.do_GET()
            else:
                handler.do_POST()
            status = handler.response_status or 500
            reason = HTTPStatus(status).phrase
            output.seek(0)
            start_response(f'{status} {reason}', handler.response_headers)
        except Exception:
            output.close()
            raise
        if method == 'HEAD':
            output.close()
            return []

        return FileBody(output)
    except Exception as error:
        logging.getLogger('trace').error('Request failed (%s)', type(error).__name__)
        return _small_response(start_response, 500, 'Trace could not complete this request.')


def run(*, check=False):
    if os.environ.get('TRACE_ENV') != 'production':
        raise SystemExit('Set TRACE_ENV=production before starting the hosted runtime.')
    if not public_origin():
        raise SystemExit('Set TRACE_PUBLIC_ORIGIN to the hosted HTTPS origin before starting Trace.')
    if not os.path.isabs(os.environ.get('TRACE_DATA_DIR', '')):
        raise SystemExit('Set TRACE_DATA_DIR to an absolute persistent private data directory.')
    if os.environ.get('TRACE_TRUST_PROXY') != '1':
        raise SystemExit('Set TRACE_TRUST_PROXY=1 for the local Caddy proxy.')
    signup_mode()
    library_byte_limit()
    if check:
        print('Trace production configuration is valid.')
        return
    load_local_rating_key()
    try:
        from waitress import serve
    except ImportError:
        raise SystemExit('Install requirements-runtime.txt in the deployment virtual environment.')
    serve(application, host='127.0.0.1', port=5173, threads=6,
          connection_limit=100, backlog=128, channel_timeout=30,
          cleanup_interval=5, max_request_header_size=16384,
          max_request_body_size=MAX_BODY_BYTES, expose_tracebacks=False,
          clear_untrusted_proxy_headers=True, channel_request_lookahead=0,
          ident='Trace')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Host Trace behind the local HTTPS proxy.')
    parser.add_argument('--check', action='store_true', help='Validate configuration without opening a socket or database.')
    run(check=parser.parse_args().check)
