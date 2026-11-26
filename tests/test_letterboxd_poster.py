import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import server
from profile_feeds import (letterboxd_film_url, letterboxd_poster_from_html, fetch_letterboxd_poster,
                           LetterboxdRedirectHandler, LETTERBOXD_PAGE_LIMIT)
from tests.test_production import request
from tests.pg_support import PostgresTestCase, bind_server

POSTER = 'https://a.ltrbxd.com/resized/film-poster/5/1/5/1/5/51515-heat-0-230-0-345-crop.jpg?v=abc'
BACKDROP = 'https://a.ltrbxd.com/resized/sm/upload/aa/bb/heat-1200-1200-675-675-crop-000000.jpg'
JSON_LD = ('<script type="application/ld+json">\n/* <![CDATA[ */\n'
           + json.dumps({'@type': 'Movie', 'name': 'Heat', 'image': POSTER}) + '\n/* ]]> */\n</script>')


def page(head='', body=''):
    return f'<html><head>{head}</head><body>{body}</body></html>'.encode()


class FilmUrlTests(unittest.TestCase):
    def test_accepts_short_links_and_film_pages(self):
        self.assertEqual(letterboxd_film_url('https://boxd.it/AbCd'), 'https://boxd.it/AbCd')
        self.assertEqual(letterboxd_film_url('https://letterboxd.com/film/heat-1995/'), 'https://letterboxd.com/film/heat-1995/')
        self.assertEqual(letterboxd_film_url('https://letterboxd.com/film/heat-1995'), 'https://letterboxd.com/film/heat-1995/')
        self.assertEqual(letterboxd_film_url('https://letterboxd.com/some_user/film/heat-1995/'), 'https://letterboxd.com/film/heat-1995/')

    def test_rejects_other_hosts_schemes_and_paths(self):
        for value in ('http://boxd.it/AbCd', 'https://evil.example/film/heat/', 'https://www.letterboxd.com/film/heat/',
                      'https://letterboxd.com.evil.example/film/heat/', 'https://boxd.it.evil.example/AbCd',
                      'https://user@letterboxd.com/film/heat/', 'https://letterboxd.com:8443/film/heat/',
                      'https://boxd.it/AbCd/extra', 'https://boxd.it/', 'https://boxd.it/Ab-Cd',
                      'https://letterboxd.com/film/heat/reviews/', 'https://letterboxd.com/user/list/films/',
                      'https://letterboxd.com/film/heat/?x=1', 'https://letterboxd.com/film/heat/#top',
                      'https://letterboxd.com/film/../settings/', 'https://letterboxd.com/a/b/film/heat/',
                      'javascript:alert(1)', '', None, 'https://boxd.it/' + 'a' * 40, ' https://boxd.it/AbCd'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                letterboxd_film_url(value)

    def test_redirects_stay_on_letterboxd_film_pages(self):
        handler, req = LetterboxdRedirectHandler(), Request('https://boxd.it/AbCd')
        followed = handler.redirect_request(req, None, 301, 'Moved', {}, 'https://letterboxd.com/film/heat-1995/')
        self.assertEqual(followed.full_url, 'https://letterboxd.com/film/heat-1995/')
        member = handler.redirect_request(req, None, 302, 'Found', {}, 'https://letterboxd.com/someone/film/heat-1995/1/')
        self.assertEqual(member.full_url, 'https://letterboxd.com/film/heat-1995/')
        for target in ('https://evil.example/film/heat/', 'http://letterboxd.com/film/heat/', 'https://boxd.it/Other',
                       'https://letterboxd.com/someone/list/heat/', 'https://a.ltrbxd.com/x.jpg'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                handler.redirect_request(req, None, 302, 'Found', {}, target)


class ExtractionTests(unittest.TestCase):
    def test_prefers_json_ld_image_over_og_image(self):
        html = page(f'<meta property="og:image" content="{BACKDROP}">', JSON_LD)
        self.assertEqual(letterboxd_poster_from_html(html), POSTER)

    def test_falls_back_to_og_image_in_either_attribute_order(self):
        self.assertEqual(letterboxd_poster_from_html(page(f'<meta property="og:image" content="{POSTER.replace("&", "&amp;")}" />')), POSTER)
        self.assertEqual(letterboxd_poster_from_html(page(f'<meta content="{POSTER}" property="og:image">')), POSTER)

    def test_rejects_placeholders_other_hosts_and_malformed_data(self):
        placeholder = 'https://a.ltrbxd.com/resized/film-poster/empty-poster-230-0-345-crop.png'
        for html in (page(f'<meta property="og:image" content="{placeholder}">'),
                     page(f'<meta property="og:image" content="https://s.ltrbxd.com/static/img/empty-poster-1000.png">'),
                     page('<meta property="og:image" content="https://evil.example/a.jpg">'),
                     page('<meta property="og:image" content="http://a.ltrbxd.com/a.jpg">'),
                     page('<meta property="og:image" content="https://a.ltrbxd.com.evil.example/a.jpg">'),
                     page('', '<script type="application/ld+json">{not json</script>'),
                     page('', '<script type="application/ld+json">' + json.dumps({'image': placeholder}) + '</script>'),
                     b'', b'\xff\xfe'):
            with self.subTest(html=html[:120]):
                self.assertEqual(letterboxd_poster_from_html(html), '')
        broken_ld = page(f'<meta property="og:image" content="{POSTER}">', '<script type="application/ld+json">[1,</script>')
        self.assertEqual(letterboxd_poster_from_html(broken_ld), POSTER)


class FakeResponse:
    def __init__(self, body, url):
        self.body, self.url, self.reads = body, url, []

    def read(self, limit):
        self.reads.append(limit)
        return self.body[:limit]

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, result):
        self.result, self.calls = result, []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FetchTests(unittest.TestCase):
    def test_bounded_short_timeout_fetch_returns_poster(self):
        response = FakeResponse(page('', JSON_LD), 'https://letterboxd.com/film/heat-1995/')
        opener = FakeOpener(response)
        self.assertEqual(fetch_letterboxd_poster('https://boxd.it/AbCd', None, opener=opener), POSTER)
        self.assertEqual(opener.calls[0][0], 'https://boxd.it/AbCd')
        self.assertLessEqual(opener.calls[0][1], 8)
        self.assertEqual(response.reads, [LETTERBOXD_PAGE_LIMIT])
        self.assertLessEqual(LETTERBOXD_PAGE_LIMIT, 600_000)

    def test_off_site_redirect_and_missing_film_mean_no_poster(self):
        self.assertEqual(fetch_letterboxd_poster('https://boxd.it/AbCd', None, opener=FakeOpener(ValueError('off-site redirect'))), '')
        missing = HTTPError('https://letterboxd.com/film/x/', 404, 'Not Found', {}, None)
        self.assertEqual(fetch_letterboxd_poster('https://letterboxd.com/film/x/', None, opener=FakeOpener(missing)), '')
        landed = FakeResponse(page('', JSON_LD), 'https://evil.example/film/heat/')
        self.assertEqual(fetch_letterboxd_poster('https://boxd.it/AbCd', None, opener=FakeOpener(landed)), '')

    def test_provider_outage_is_raised_not_cached_as_missing(self):
        outage = HTTPError('https://letterboxd.com/film/x/', 503, 'Unavailable', {}, None)
        with self.assertRaises(HTTPError):
            fetch_letterboxd_poster('https://letterboxd.com/film/x/', None, opener=FakeOpener(outage))


class PosterEndpointTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        server.CACHE.clear()
        server.API_BUDGET.buckets.clear()
        server.LAST.pop('letterboxd', None)
        bind_server(self, self.database)

    def get(self, url, **headers):
        from urllib.parse import quote
        response = request('/api/letterboxd-poster?url=' + quote(url, safe=''), headers=headers)
        return response['status'], json.loads(response['body'])

    @patch('server.fetch_letterboxd_poster', return_value=POSTER)
    def test_returns_image_and_caches_by_url(self, fetch):
        self.assertEqual(self.get('https://boxd.it/AbCd'), (200, {'image': POSTER}))
        self.assertEqual(self.get('https://boxd.it/AbCd'), (200, {'image': POSTER}))
        server.CACHE.clear()  # The durable catalog query cache also answers.
        self.assertEqual(self.get('https://boxd.it/AbCd'), (200, {'image': POSTER}))
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args[0][0], 'https://boxd.it/AbCd')
        fetch.return_value = POSTER.replace('heat', 'other')
        self.assertEqual(self.get('https://boxd.it/abcd')[1], {'image': POSTER.replace('heat', 'other')},
                         'short codes are case-sensitive cache keys')
        self.assertEqual(fetch.call_count, 2)

    @patch('server.fetch_letterboxd_poster', return_value='')
    def test_missing_poster_is_404_and_cached(self, fetch):
        self.assertEqual(self.get('https://letterboxd.com/film/unknown/')[0], 404)
        server.CACHE.clear()
        self.assertEqual(self.get('https://letterboxd.com/someone/film/unknown/')[0], 404)
        fetch.assert_called_once()

    @patch('server.fetch_letterboxd_poster', side_effect=HTTPError('https://letterboxd.com/film/x/', 503, 'x', {}, None))
    def test_provider_failure_is_502_and_not_cached(self, fetch):
        self.assertEqual(self.get('https://letterboxd.com/film/x/')[0], 502)
        self.assertEqual(self.get('https://letterboxd.com/film/x/')[0], 502)
        self.assertEqual(fetch.call_count, 2)

    @patch('server.fetch_letterboxd_poster', return_value=POSTER)
    def test_invalid_urls_are_rejected_without_fetching(self, fetch):
        for url in ('https://evil.example/film/heat/', 'http://boxd.it/AbCd', 'https://letterboxd.com/film/heat/reviews/'):
            with self.subTest(url=url):
                self.assertEqual(self.get(url)[0], 400)
        fetch.assert_not_called()

    @patch('server.fetch_letterboxd_poster', return_value=POSTER)
    def test_hosted_deployments_require_a_signed_in_user(self, fetch):
        env = {'TRACE_ENV': 'production', 'TRACE_PUBLIC_ORIGIN': 'https://trace.example', 'TRACE_TRUST_PROXY': '1', 'TRACE_SIGNUP_MODE': 'invite'}
        hosted = {'HTTP_HOST': 'trace.example', 'HTTP_ORIGIN': 'https://trace.example', 'HTTP_X_TRACE_CLIENT_IP': '203.0.113.7'}
        with patch.dict('os.environ', env):
            server.auth_budget().clear()
            self.assertEqual(self.get('https://boxd.it/AbCd', **hosted)[0], 401)
            fetch.assert_not_called()
            social = server.get_social()
            payload = {'username': 'poster_alice', 'password': 'test-password-synthetic', 'inviteCode': social.create_invite()}
            signup = request('/api/auth/signup', 'POST', payload, hosted)
            cookie = dict(signup['headers'])['Set-Cookie'].split(';', 1)[0]
            self.assertEqual(self.get('https://boxd.it/AbCd', HTTP_COOKIE=cookie, **hosted), (200, {'image': POSTER}))


if __name__ == '__main__':
    unittest.main()
