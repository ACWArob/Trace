"""Vercel entry point: every /api/* request is rewritten here (see vercel.json).

Vercel keeps the original PATH_INFO under the rewrite.  A rewrite parameter
that the destination does not use is appended to QUERY_STRING (``path=`` for
``/api/:path*``; a numbered key may appear for an unnamed group), so those keys
are removed before Trace sees the request.  Trace reads no such parameter.
"""
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import production  # noqa: E402

_REWRITE_KEY = re.compile(r'path|\d+')


def strip_rewrite_parameters(query):
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if not _REWRITE_KEY.fullmatch(key)]
    if len(kept) == len(pairs):
        return query  # Leave an untouched query byte-for-byte as sent.
    return urlencode(kept)


def app(environ, start_response):
    environ = dict(environ)
    environ['QUERY_STRING'] = strip_rewrite_parameters(environ.get('QUERY_STRING', ''))
    return production.application(environ, start_response)
