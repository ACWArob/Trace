import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse, quote

import server
from cover_sources import (cover_params, cover_title, surnames, tvmaze_cover, kitsu_cover,
                           openlibrary_isbn_cover, openlibrary_search_cover, find_cover, COVER_READ_LIMIT)
from tests.test_production import request
from tests.pg_support import PostgresTestCase, bind_server

TVM = 'https://static.tvmaze.com/uploads/images/medium_portrait/1/{}.jpg'
KITSU = 'https://media.kitsu.app/anime/poster_images/{}/medium.jpg'
OL_ISBN = 'https://covers.openlibrary.org/b/isbn/9780140328721-L.jpg?default=false'


def show(identifier, name, premiered, image=None):
    return {'show': {'id': identifier, 'name': name, 'premiered': premiered,
                     'image': {'medium': image if image is not None else TVM.format(identifier)}}}


def anime(identifier, titles, start, image=None, canonical=''):
    return {'id': str(identifier), 'type': 'anime', 'attributes': {
        'titles': titles, 'canonicalTitle': canonical, 'startDate': start,
        'posterImage': {'medium': image if image is not None else KITSU.format(identifier)}}}


class Recorder:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, url, provider):
        self.calls.append((url, provider))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ParamTests(unittest.TestCase):
    def test_normalizes_valid_parameters(self):
        self.assertEqual(cover_params('tv', '  The Wire ', '2002', 'ignored', ''), ('tv', 'The Wire', '2002', '', ''))
        self.assertEqual(cover_params('book', 'Matilda', '', 'Roald Dahl', '0-14-032872-6'),
                         ('book', 'Matilda', '', 'Roald Dahl', '9780140328721'))
        self.assertEqual(cover_params('anime', 'Mushishi', '', '', '')[0], 'anime')

    def test_rejects_invalid_parameters(self):
        for args in (('movie', 'Heat', '', '', ''), ('album', 'Blue', '', '', ''), ('', 'X', '', '', ''),
                     ('tv', '', '', '', ''), ('tv', '   ', '', '', ''), ('tv', 'x' * 301, '', '', ''),
                     ('tv', 'Bad\x00Title', '', '', ''), ('tv', 'Lost', '20O4', '', ''), ('tv', 'Lost', '04', '', ''),
                     ('book', 'Matilda', '', 'a' * 301, ''), ('book', 'Matilda', '', '', '12345'),
                     ('book', 'Matilda', '', '', '9780140328722'), ('book', 'Matilda', '', 'A\nB', '')):
            with self.subTest(args=args), self.assertRaises(ValueError):
                cover_params(*args)

    def test_title_and_surname_normalization_mirror_core(self):
        self.assertEqual(cover_title('The Wire'), cover_title('wire'))
        self.assertEqual(cover_title('Amélie & Co.'), 'amelieandco')
        self.assertEqual(surnames('Tolkien, J.R.R.'), {'tolkien'})
        self.assertEqual(surnames('Neil Gaiman & Terry Pratchett'), {'gaiman', 'pratchett'})
        self.assertEqual(surnames('Martin Luther King Jr.'), {'king'})


class TvmazeTests(unittest.TestCase):
    def test_strict_title_and_year_match(self):
        fetch = Recorder([show(1, 'The Office', '2001-07-09'), show(2, 'The Office', '2005-03-24'), show(3, 'Office Space', '2005-01-01')])
        self.assertEqual(tvmaze_cover('The Office', '2005', fetch), TVM.format(2))
        url, provider = fetch.calls[0]
        self.assertEqual(provider, 'tvmaze')
        self.assertEqual(urlparse(url).netloc, 'api.tvmaze.com')
        self.assertEqual(parse_qs(urlparse(url).query), {'q': ['The Office']})

    def test_ambiguous_mismatched_and_unsafe_results_are_rejected(self):
        two = [show(1, 'The Office', '2001-07-09'), show(2, 'The Office', '2005-03-24')]
        self.assertEqual(tvmaze_cover('The Office', '', Recorder(two)), '', 'two same-titled shows without a year')
        self.assertEqual(tvmaze_cover('The Office', '1999', Recorder(two)), '')
        self.assertEqual(tvmaze_cover('Office', '2005', Recorder([show(3, 'Office Space', '2005-01-01')])), '')
        unsafe = [show(4, 'Lost', '2004-09-22', 'https://evil.example/lost.jpg')]
        self.assertEqual(tvmaze_cover('Lost', '2004', Recorder(unsafe)), '')
        self.assertEqual(tvmaze_cover('Lost', '2004', Recorder({'not': 'a list'})), '')

    def test_single_title_match_without_year_and_unknown_premiere(self):
        self.assertEqual(tvmaze_cover('Lost', '', Recorder([show(5, 'Lost', '2004-09-22')])), TVM.format(5))
        self.assertEqual(tvmaze_cover('Lost', '2004', Recorder([show(6, 'Lost', None)])), TVM.format(6))


class KitsuTests(unittest.TestCase):
    def test_matches_any_listed_title_and_year(self):
        data = {'data': [anime(1, {'en': 'Attack on Titan', 'en_jp': 'Shingeki no Kyojin'}, '2013-04-07'),
                         anime(2, {'en_jp': 'Mushishi'}, '2005-10-23', canonical='Mushi-Shi')]}
        fetch = Recorder(data, data, data)
        self.assertEqual(kitsu_cover('Shingeki no Kyojin', '2013', fetch), KITSU.format(1))
        self.assertEqual(kitsu_cover('Mushi-shi', '', fetch), KITSU.format(2))
        self.assertEqual(kitsu_cover('Attack on Titan', '2014', fetch), '')
        url, provider = fetch.calls[0]
        self.assertEqual(provider, 'kitsu')
        self.assertIn(urlparse(url).netloc, ('kitsu.io', 'kitsu.app'))

    def test_rejects_other_image_hosts_and_malformed_rows(self):
        data = {'data': [anime(1, {'en': 'Monster'}, '2004-04-07', 'https://evil.example/m.jpg'), 'junk', {'attributes': None}]}
        self.assertEqual(kitsu_cover('Monster', '2004', Recorder(data)), '')
        self.assertEqual(kitsu_cover('Monster', '2004', Recorder({'data': 'x'})), '')


class FakeOpener:
    def __init__(self, result):
        self.result, self.calls = result, []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, req.get_method(), timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class OpenLibraryTests(unittest.TestCase):
    def test_isbn_cover_exists_on_redirect_or_ok_and_404_means_none(self):
        redirect = HTTPError(OL_ISBN, 302, 'Found', {'Location': 'https://archive.org/x.jpg'}, None)
        opener = FakeOpener(redirect)
        self.assertEqual(openlibrary_isbn_cover('9780140328721', None, opener=opener), OL_ISBN)
        url, method, timeout = opener.calls[0]
        self.assertEqual(url, OL_ISBN)
        self.assertEqual(method, 'HEAD')
        self.assertLessEqual(timeout, 9)
        self.assertEqual(openlibrary_isbn_cover('9780140328721', None, opener=FakeOpener(FakeResponse())), OL_ISBN)
        missing = HTTPError(OL_ISBN, 404, 'Not Found', {}, None)
        self.assertEqual(openlibrary_isbn_cover('9780140328721', None, opener=FakeOpener(missing)), '')

    def test_isbn_outage_raises_and_invalid_isbn_is_rejected(self):
        with self.assertRaises(HTTPError):
            openlibrary_isbn_cover('9780140328721', None, opener=FakeOpener(HTTPError(OL_ISBN, 503, 'x', {}, None)))
        with self.assertRaises(ValueError):
            openlibrary_isbn_cover('not-an-isbn', None, opener=FakeOpener(FakeResponse()))

    def test_title_search_matches_title_and_author_surname(self):
        docs = {'docs': [{'key': '/works/OL1W', 'title': 'Matilda', 'author_name': ['Someone Else'], 'cover_i': 11},
                         {'key': '/works/OL2W', 'title': 'Matilda', 'author_name': ['Roald Dahl'], 'cover_i': 22, 'first_publish_year': 1988},
                         {'key': '/works/OL3W', 'title': 'Matilda Returns', 'author_name': ['Roald Dahl'], 'cover_i': 33}]}
        fetch = Recorder(docs)
        self.assertEqual(openlibrary_search_cover('Matilda', 'Dahl, Roald', '2007', fetch), 'https://covers.openlibrary.org/b/id/22-L.jpg')
        url, provider = fetch.calls[0]
        self.assertEqual(provider, 'openlibrary')
        self.assertEqual(urlparse(url).netloc, 'openlibrary.org')
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query['title'], ['Matilda'])
        self.assertEqual(query['author'], ['Dahl, Roald'])
        self.assertIn('cover_i', query['fields'][0])

    def test_title_search_rejects_wrong_authors_missing_covers_and_unanchored_titles(self):
        docs = {'docs': [{'title': 'Matilda', 'author_name': ['Someone Else'], 'cover_i': 11},
                         {'title': 'Matilda', 'author_name': ['Roald Dahl']},
                         {'title': 'Matilda', 'author_name': ['Roald Dahl'], 'cover_i': -1}]}
        self.assertEqual(openlibrary_search_cover('Matilda', 'Roald Dahl', '', Recorder(docs)), '')
        no_creator = {'docs': [{'title': 'Matilda', 'author_name': ['Roald Dahl'], 'cover_i': 22, 'first_publish_year': 1988}]}
        self.assertEqual(openlibrary_search_cover('Matilda', '', '', Recorder(no_creator)), '', 'no author and no year')
        self.assertEqual(openlibrary_search_cover('Matilda', '', '1988', Recorder(no_creator)), 'https://covers.openlibrary.org/b/id/22-L.jpg')
        with self.assertRaises(ValueError):
            openlibrary_search_cover('Matilda', 'Roald Dahl', '', Recorder({'docs': 'x'}))

    def test_find_cover_tries_isbn_then_title_search(self):
        docs = {'docs': [{'title': 'Matilda', 'author_name': ['Roald Dahl'], 'cover_i': 22}]}
        isbn_calls = []
        def isbn_cover(isbn):
            isbn_calls.append(isbn)
            return ''
        self.assertEqual(find_cover('book', 'Matilda', '', 'Roald Dahl', '9780140328721', Recorder(docs), isbn_cover),
                         'https://covers.openlibrary.org/b/id/22-L.jpg')
        self.assertEqual(isbn_calls, ['9780140328721'])
        fetch = Recorder()
        self.assertEqual(find_cover('book', 'Matilda', '', 'Roald Dahl', '9780140328721', fetch, lambda isbn: OL_ISBN), OL_ISBN)
        self.assertEqual(fetch.calls, [], 'an ISBN cover needs no search')


class CoverEndpointTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        server.CACHE.clear()
        server.API_BUDGET.buckets.clear()
        for provider in ('tvmaze', 'kitsu', 'openlibrary', 'openlibrary-covers'):
            server.LAST.pop(provider, None)
        bind_server(self, self.database)

    def get(self, headers=None, **params):
        response = request('/api/cover?' + '&'.join(f'{k}={quote(v, safe="")}' for k, v in params.items()), headers=headers)
        return response['status'], json.loads(response['body'])

    def test_tv_cover_is_returned_and_cached_in_memory_and_durably(self):
        with patch('server.fetch', return_value=[show(2, 'The Office', '2005-03-24')]) as fetch:
            self.assertEqual(self.get(type='tv', title='The Office', year='2005'), (200, {'image': TVM.format(2)}))
            self.assertEqual(self.get(type='tv', title='The Office', year='2005'), (200, {'image': TVM.format(2)}))
            server.CACHE.clear()
            self.assertEqual(self.get(type='tv', title='the office', year='2005'), (200, {'image': TVM.format(2)}))
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.kwargs.get('limit'), COVER_READ_LIMIT, 'provider reads are bounded')
            self.assertEqual(self.get(type='tv', title='The Office', year='2001')[0], 404)
            self.assertEqual(fetch.call_count, 2, 'a different year is a different cache key')

    def test_missing_cover_is_404_and_cached_but_outages_are_not(self):
        with patch('server.fetch', return_value={'data': []}) as fetch:
            self.assertEqual(self.get(type='anime', title='Unknown Anime')[0], 404)
            server.CACHE.clear()
            self.assertEqual(self.get(type='anime', title='Unknown Anime')[0], 404)
            fetch.assert_called_once()
        with patch('server.fetch', side_effect=HTTPError('https://api.tvmaze.com/', 503, 'x', {}, None)) as fetch:
            self.assertEqual(self.get(type='tv', title='Lost')[0], 502)
            self.assertEqual(self.get(type='tv', title='Lost')[0], 502)
            self.assertEqual(fetch.call_count, 2)

    def test_book_isbn_404_falls_back_to_title_and_author_search(self):
        docs = {'docs': [{'title': 'Matilda', 'author_name': ['Roald Dahl'], 'cover_i': 22}]}
        with patch('server.openlibrary_isbn_cover', return_value='') as isbn, patch('server.fetch', return_value=docs) as fetch:
            status, body = self.get(type='book', title='Matilda', creator='Roald Dahl', isbn='0140328726')
            self.assertEqual((status, body), (200, {'image': 'https://covers.openlibrary.org/b/id/22-L.jpg'}))
            self.assertEqual(isbn.call_args.args[0], '9780140328721')
            fetch.assert_called_once()
        server.CACHE.clear()
        with patch('server.openlibrary_isbn_cover', return_value=OL_ISBN), patch('server.fetch') as fetch:
            self.assertEqual(self.get(type='book', title='Matilda', isbn='9780140328721'), (200, {'image': OL_ISBN}))
            fetch.assert_not_called()

    def test_invalid_parameters_are_rejected_without_fetching(self):
        with patch('server.fetch') as fetch, patch('server.openlibrary_isbn_cover') as isbn:
            for params in ({'type': 'movie', 'title': 'Heat'}, {'type': 'tv'}, {'type': 'tv', 'title': 'x' * 301},
                           {'type': 'tv', 'title': 'Lost', 'year': '04'}, {'type': 'book', 'title': 'M', 'isbn': '123'},
                           {'type': 'book', 'title': 'M', 'creator': 'c' * 301}):
                with self.subTest(params=params):
                    self.assertEqual(self.get(**params)[0], 400)
            fetch.assert_not_called()
            isbn.assert_not_called()

    def test_hosted_deployments_require_a_signed_in_user(self):
        env = {'TRACE_ENV': 'production', 'TRACE_PUBLIC_ORIGIN': 'https://trace.example', 'TRACE_TRUST_PROXY': '1', 'TRACE_SIGNUP_MODE': 'invite'}
        hosted = {'HTTP_HOST': 'trace.example', 'HTTP_ORIGIN': 'https://trace.example', 'HTTP_X_TRACE_CLIENT_IP': '203.0.113.8'}
        with patch.dict('os.environ', env), patch('server.fetch', return_value=[show(5, 'Lost', '2004-09-22')]) as fetch:
            server.auth_budget().clear()
            self.assertEqual(self.get(hosted, type='tv', title='Lost')[0], 401)
            fetch.assert_not_called()
            social = server.get_social()
            payload = {'username': 'cover_alice', 'password': 'test-password-synthetic', 'inviteCode': social.create_invite()}
            signup = request('/api/auth/signup', 'POST', payload, hosted)
            cookie = dict(signup['headers'])['Set-Cookie'].split(';', 1)[0]
            self.assertEqual(self.get({'HTTP_COOKIE': cookie, **hosted}, type='tv', title='Lost'), (200, {'image': TVM.format(5)}))


if __name__ == '__main__':
    unittest.main()
