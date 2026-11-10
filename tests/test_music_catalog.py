import unittest
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from music_catalog import musicbrainz_query, search_albums


MBID = '0da340a0-6ad7-4fc2-a272-6f94393a7831'
ARTIST_ID = '11111111-2222-3333-4444-555555555555'


def apple(**changes):
    return {'collectionType': 'Album', 'wrapperType': 'collection', 'collectionName': 'Blonde',
            'collectionId': 1146195596, 'artistName': 'Frank Ocean', 'releaseDate': '2016-08-20T00:00:00Z',
            'artworkUrl100': 'https://is1-ssl.mzstatic.com/image/100x100bb.jpg',
            'collectionViewUrl': 'https://music.apple.com/us/album/blonde/1146195596',
            'primaryGenreName': 'R&B/Soul', **changes}


def mb(**changes):
    return {'id': MBID, 'title': 'Blonde', 'primary-type': 'Album', 'first-release-date': '2016-08-20',
            'artist-credit': [{'name': 'Frank Ocean', 'artist': {'name': 'Frank Ocean'}}],
            'tags': [{'name': 'alternative r&b', 'count': 5}], **changes}


class MusicCatalogTests(unittest.TestCase):
    def run_search(self, apple_data=None, mb_data=None, query='Frank Ocean Blonde'):
        calls = []
        def fetch(url, provider):
            calls.append((url, provider))
            data = apple_data if provider == 'apple' else mb_data
            if isinstance(data, BaseException):
                raise data
            return data
        result = search_albums(query, fetch)
        return result, calls

    def test_musicbrainz_finds_album_missing_from_apple_with_fixed_hosts(self):
        result, calls = self.run_search({'results': []}, {'release-groups': [mb()]})
        item = result['items'][0]
        self.assertEqual((item['title'], item['creator'], item['type']), ('Blonde', 'Frank Ocean', 'album'))
        self.assertEqual(item['sourceUrl'], 'https://musicbrainz.org/release-group/' + MBID)
        self.assertEqual(item['image'], 'https://coverartarchive.org/release-group/' + MBID + '/front-500')
        self.assertEqual({urlparse(url).hostname for url, _ in calls}, {'itunes.apple.com', 'musicbrainz.org'})
        self.assertEqual(result['providerStatus'], {'apple': 'ok', 'musicbrainz': 'ok'})
        self.assertEqual(result['catalogVersion'], 3)

    def test_user_field_boolean_and_wildcard_syntax_remains_literal(self):
        query = 'Björk OR primarytype:single "live" a\\b *'
        result, calls = self.run_search({'results': []}, {'release-groups': []}, query)
        sent = parse_qs(urlparse(calls[1][0]).query)['query'][0]
        self.assertEqual(sent, musicbrainz_query(query))
        self.assertIn('"OR"', sent)
        self.assertIn('"primarytype:single"', sent)
        self.assertIn('"\\"live\\""', sent)
        self.assertIn('"a\\\\b"', sent)
        self.assertTrue(sent.endswith(') AND primarytype:album'))
        self.assertEqual(result['items'], [])

    def test_duplicate_album_retains_both_sources_and_apple_artwork(self):
        result, _ = self.run_search({'results': [apple()]}, {'release-groups': [mb()]})
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['externalId'], '1146195596')
        self.assertIn('600x600bb', item['image'])
        self.assertEqual({source['source'] for source in item['catalogSources']}, {'Apple Music', 'MusicBrainz'})
        self.assertEqual(set(item['genres']), {'alternative r&b', 'R&B/Soul'})

    def test_different_editions_and_known_years_remain_separate(self):
        result, _ = self.run_search({'results': [apple(), apple(collectionName='Blonde (Deluxe)', collectionId=2),
                                                 apple(releaseDate='2026-01-01', collectionId=3)]},
                                    {'release-groups': [mb()]})
        self.assertEqual(len(result['items']), 3)

    def test_tracks_singles_and_eps_are_excluded(self):
        result, _ = self.run_search({'results': [apple(), apple(wrapperType='track', collectionId=2),
                                                  apple(collectionName='Pink + White - Single', collectionId=3),
                                                  apple(collectionName='Blonde (EP)', collectionId=4)]},
                                    {'release-groups': [mb(), mb(**{'primary-type': 'Single'}),
                                                        mb(**{'primary-type': 'EP'})]})
        self.assertEqual(len(result['items']), 1)

    def test_malformed_rows_do_not_crash_or_create_unsafe_links(self):
        result, _ = self.run_search({'results': [None, [], 7, apple(collectionId={'bad': 1}),
                                                  apple(collectionViewUrl='https://music.apple.com:invalid/a',
                                                        artworkUrl100='javascript:alert(1)')]},
                                    {'release-groups': [None, [], 7, mb(id='../../bad'),
                                                        mb(id='https://evil.example'), mb(id='not-a-uuid')]})
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['image'], '')
        self.assertEqual(result['items'][0]['sourceUrl'], 'https://music.apple.com/us/album/1146195596')

    def test_each_provider_failure_preserves_other_results_without_error_details(self):
        for apple_data, mb_data in [(URLError('private-token=secret'), {'release-groups': [mb()]}),
                                    ({'results': [apple()]}, URLError('private-token=secret'))]:
            with self.subTest(apple_failure=isinstance(apple_data, BaseException)):
                result, _ = self.run_search(apple_data, mb_data)
                self.assertEqual(len(result['items']), 1)
                self.assertEqual(len(result['warnings']), 1)
                self.assertNotIn('secret', str(result))

    def test_json_primitives_are_provider_failures_and_two_failures_raise(self):
        result, _ = self.run_search(3, {'release-groups': [mb()]})
        self.assertEqual(result['providerStatus']['apple'], 'unavailable')
        with self.assertRaisesRegex(URLError, 'Album catalogs are unavailable'):
            self.run_search(None, ['not-an-object'])

    def test_precise_artist_and_album_match_precedes_unrelated_word_matches(self):
        result, _ = self.run_search({'results': [apple(collectionName='Ocean Waves Relaxing Music',
                                                     artistName='Frank Blonde', collectionId=2)]},
                                    {'release-groups': [mb()]})
        self.assertEqual(result['items'][0]['creator'], 'Frank Ocean')

    def test_results_and_query_lengths_are_bounded(self):
        rows = [apple(collectionName='Album ' + str(i), collectionId=i + 1) for i in range(50)]
        result, calls = self.run_search({'results': rows}, {'release-groups': []}, 'Album')
        self.assertEqual(len(result['items']), 30)
        self.assertEqual(parse_qs(urlparse(calls[0][0]).query)['entity'], ['album'])
        self.assertEqual(parse_qs(urlparse(calls[1][0]).query)['limit'], ['30'])
        for query in [None, 'x', 'x' * 201]:
            with self.assertRaises(ValueError):
                search_albums(query, lambda *_: self.fail('Invalid query reached network'))

    def alias_search(self, artists=None, albums=None, query='سعاد ماسي', initial=None):
        calls = []
        artist_data = {'artists':[{'id':ARTIST_ID,'name':'Souad Massi','score':'100','aliases':[{'name':'سعاد ماسي'}]}]} if artists is None else artists
        album_data = {'release-groups':[mb(title='راوي',**{'artist-credit':[{'name':'Souad Massi','artist':{'id':ARTIST_ID,'name':'Souad Massi'}}]})]} if albums is None else albums
        initial_data = {'release-groups':[mb(id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',title='سعاد ماسي unrelated',
                                            **{'artist-credit':[{'name':'Another artist'}]})]} if initial is None else initial
        def fetch(url, provider):
            calls.append((url,provider))
            if provider == 'apple':
                return {'results':[]}
            if urlparse(url).path == '/ws/2/artist/':
                data = artist_data
            else:
                data = album_data if 'artist' in parse_qs(urlparse(url).query) else initial_data
            if isinstance(data, BaseException):
                raise data
            return data
        return search_albums(query,fetch), calls

    def test_non_latin_artist_alias_resolves_and_boosts_the_correct_album(self):
        result, calls = self.alias_search()
        self.assertEqual((result['items'][0]['title'],result['items'][0]['creator']),('راوي','Souad Massi'))
        self.assertEqual(len(calls),4)
        artist_query=parse_qs(urlparse(calls[2][0]).query)
        self.assertEqual(artist_query['query'],['("سعاد" "ماسي")'])
        self.assertEqual(artist_query['limit'],['3'])
        browse=parse_qs(urlparse(calls[3][0]).query)
        self.assertEqual(browse['artist'],[ARTIST_ID])
        self.assertEqual(browse['type'],['album'])
        self.assertEqual(browse['inc'],['artist-credits+tags'])
        self.assertEqual(browse['limit'],['30'])
        self.assertNotIn('country',browse)
        self.assertEqual(result['warnings'],[])

    def test_alias_resolution_is_skipped_for_latin_or_direct_artist_matches(self):
        _, calls = self.alias_search(query='Souad Massi')
        self.assertEqual(len(calls),2)
        result, calls = self.alias_search(initial={'release-groups':[mb(**{'artist-credit':[{'name':'سعاد ماسي'}]})]})
        self.assertEqual(len(calls),2)
        self.assertEqual(result['items'][0]['creator'],'سعاد ماسي')

    def test_unrelated_aliases_invalid_ids_and_ambiguous_artists_are_not_browsed(self):
        wrong={'id':ARTIST_ID,'name':'Another artist','score':100,'aliases':[{'name':'سعاد أخرى'}]}
        malformed={**wrong,'id':'../../bad','aliases':[{'name':'سعاد ماسي'}]}
        exact={**wrong,'aliases':[{'name':'سعاد ماسي'}]}
        ambiguous={**exact,'id':'22222222-2222-3333-4444-555555555555'}
        for artists in [[wrong],[malformed],[exact,ambiguous]]:
            with self.subTest(artists=artists):
                result,calls=self.alias_search(artists={'artists':artists})
                self.assertEqual(len(calls),3)
                self.assertEqual(len(result['items']),1)
                self.assertEqual(result['items'][0]['creator'],'Another artist')

    def test_alias_browse_excludes_other_artists_singles_and_eps(self):
        credit=[{'name':'Souad Massi','artist':{'id':ARTIST_ID}}]
        albums={'release-groups':[mb(title='Deb',**{'artist-credit':credit}),
                                  mb(title='A single',**{'primary-type':'Single','artist-credit':credit}),
                                  mb(title='An EP',**{'primary-type':'EP','artist-credit':credit}),
                                  mb(title='Wrong artist',**{'artist-credit':[{'name':'Different artist','artist':{'id':'22222222-2222-3333-4444-555555555555'}}]})]}
        result,_=self.alias_search(albums=albums)
        self.assertEqual([item['title'] for item in result['items']],['Deb','سعاد ماسي unrelated'])

    def test_alias_timeout_keeps_existing_results_and_warns_without_retries_or_details(self):
        for part in ['artists','albums']:
            with self.subTest(part=part):
                result,calls=self.alias_search(**{part:TimeoutError('private-token=secret')})
                self.assertEqual(len(calls),3 if part=='artists' else 4)
                self.assertEqual(len(result['items']),1)
                self.assertEqual(result['providerStatus'],{'apple':'ok','musicbrainz':'ok'})
                self.assertEqual(len(result['warnings']),1)
                self.assertNotIn('secret',str(result))


if __name__ == '__main__':
    unittest.main()
