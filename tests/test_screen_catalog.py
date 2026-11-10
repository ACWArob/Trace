import unittest
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from screen_catalog import query_language, search_wikidata


def claims(**properties):
    return {key: [{'mainsnak': {'datavalue': {'value': value}}} for value in values]
            for key, values in properties.items()}


class ScreenCatalogTests(unittest.TestCase):
    def test_arabic_algerian_tv_uses_claims_labels_dates_and_arabic_wikipedia(self):
        calls = []
        entity = {'claims': claims(P31=[{'id': 'Q5398426'}], P170=[{'id': 'Q2'}], P136=[{'id': 'Q3'}],
                                   P580=[{'time': '+2023-01-01T00:00:00Z'}], P345=['tt1234567']),
                  'labels': {'ar': {'value': 'دار لفشوش'}, 'en': {'value': 'Dar Lefchouch'}},
                  'descriptions': {'ar': {'value': 'مسلسل تلفزيوني جزائري'}},
                  'sitelinks': {'arwiki': {'title': 'دار لفشوش'}}}
        def fetch(url, provider):
            calls.append((url, provider)); args = parse_qs(urlparse(url).query)
            if args.get('action') == ['wbsearchentities']:
                self.assertEqual(args['language'], ['ar'])
                return {'search': [{'id': 'Q1', 'label': 'دار لفشوش', 'description': 'مسلسل جزائري'}]}
            if args.get('props') == ['claims|sitelinks|labels|descriptions']:
                self.assertEqual(args['languages'], ['ar|en'])
                return {'entities': {'Q1': entity}}
            if args.get('props') == ['labels']:
                return {'entities': {'Q2': {'labels': {'ar': {'value': 'مبدع العمل'}}},
                                     'Q3': {'labels': {'ar': {'value': 'كوميديا'}}}}}
            self.assertEqual(urlparse(url).hostname, 'ar.wikipedia.org')
            return {'query': {'pages': {'1': {'title': 'دار لفشوش', 'extract': 'وصف المسلسل من ويكيبيديا.',
                   'thumbnail': {'source': 'https://upload.wikimedia.org/example.jpg'}}}}}
        result = search_wikidata('دار لفشوش', 'tv', fetch)
        self.assertEqual(len(calls), 4)
        item = result[0]
        self.assertEqual((item['title'], item['type'], item['year']), ('دار لفشوش', 'tv', '2023'))
        self.assertEqual(item['creator'], 'مبدع العمل')
        self.assertEqual(item['genres'], ['كوميديا'])
        self.assertEqual(item['description'], 'وصف المسلسل من ويكيبيديا.')
        self.assertEqual(item['imdbId'], 'tt1234567')
        self.assertNotIn('country', item)

    def test_arabic_movie_needs_no_english_film_word_and_optional_outages_preserve_it(self):
        calls = []
        def fetch(url, provider):
            calls.append(url); args = parse_qs(urlparse(url).query)
            if args.get('action') == ['wbsearchentities']:
                return {'search': [{'id': 'Q1', 'label': 'وقائع سنين الجمر', 'description': 'فيلم جزائري'}]}
            if args.get('props') == ['claims|sitelinks|labels|descriptions']:
                return {'entities': {'Q1': {'claims': claims(P31=[{'id': 'Q11424'}], P57=[{'id': 'Q2'}],
                    P577=[{'time': '+1975-01-01T00:00:00Z'}], P18=['Poster.jpg']),
                    'sitelinks': {'arwiki': {'title': 'وقائع سنين الجمر'}}}}}
            raise URLError('Optional provider outage')
        result = search_wikidata('وقائع سنين الجمر', 'movie', fetch)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result[0]['year'], '1975')
        self.assertEqual(result[0]['description'], 'فيلم جزائري')
        self.assertIn('commons.wikimedia.org/wiki/Special:FilePath/Poster.jpg', result[0]['image'])

    def test_tv_rejects_films_episodes_games_and_characters(self):
        descriptions = ['television series based on a film', 'episode of a television series',
                        'video game based on a television series', 'character in a television series', '2014 film']
        entities = {f'Q{i + 1}': {'claims': claims(P31=[{'id': kind}])} for i, kind in enumerate(
                    ['Q5398426', 'Q21191270', 'Q1', 'Q1', 'Q11424'])}
        def fetch(url, provider):
            if parse_qs(urlparse(url).query).get('action') == ['wbsearchentities']:
                return {'search': [{'id': f'Q{i + 1}', 'label': f'Title {i}', 'description': description}
                                   for i, description in enumerate(descriptions)]}
            return {'entities': entities}
        result = search_wikidata('A show', 'tv', fetch)
        self.assertEqual([item['externalId'] for item in result], ['Q1'])

    def test_tv_rejects_humans_and_person_typing_before_series_words(self):
        descriptions = ['Algerian television series about a writer', 'writer of television series',
                        'actor known for television series', 'television actor',
                        'studio that produces television series', 'television series franchise',
                        'مسلسل جزائري', 'ممثل جزائري معروف بالمسلسلات']
        classes = ['Q5398426', 'Q5', 'Q1', 'Q5', 'Q43229', 'Q1', 'Q5', 'Q1']
        def fetch(url, provider):
            if parse_qs(urlparse(url).query).get('action') == ['wbsearchentities']:
                return {'search': [{'id': f'Q{i + 1}', 'label': f'Entry {i}', 'description': value}
                                   for i, value in enumerate(descriptions)]}
            return {'entities': {f'Q{i + 1}': {'claims': claims(P31=[{'id': value}])}
                                 for i, value in enumerate(classes)}}
        result = search_wikidata('A show', 'tv', fetch)
        self.assertEqual([item['externalId'] for item in result], ['Q1'])

    def test_english_movie_year_director_genres_and_synopsis_are_preserved(self):
        def fetch(url, provider):
            args = parse_qs(urlparse(url).query)
            if args.get('action') == ['wbsearchentities']:
                return {'search': [{'id': 'Q1', 'label': 'A Film', 'description': '2020 film directed by A Director'}]}
            if args.get('props') == ['claims|sitelinks|labels|descriptions']:
                return {'entities': {'Q1': {'claims': claims(P136=[{'id': 'Q9'}]),
                                            'sitelinks': {'enwiki': {'title': 'A Film'}}}}}
            if args.get('props') == ['labels']:
                return {'entities': {'Q9': {'labels': {'en': {'value': 'science fiction film'}}}}}
            return {'query': {'pages': {'1': {'title': 'A Film', 'extract': 'An actual synopsis.'}}}}
        item = search_wikidata('A Film', 'movie', fetch)[0]
        self.assertEqual((item['year'], item['creator']), ('2020', 'A Director'))
        self.assertEqual(item['genres'], ['Science Fiction'])
        self.assertEqual(item['description'], 'An actual synopsis.')

    def test_script_detection_and_fixed_hosts_do_not_infer_nationality(self):
        for query, language in [('دار لفشوش', 'ar'), ('フリーレン', 'ja'), ('한국', 'ko'), ('霸王别姬', 'zh'),
                                ('שלום', 'he'), ('भारत', 'hi'), ('Москва', 'ru'), ('Djemai Family', 'en')]:
            self.assertEqual(query_language(query), language)
        calls = []
        result = search_wikidata('A query', 'movie', lambda url, provider: calls.append(url) or {'search': []})
        self.assertEqual(result, [])
        self.assertEqual(urlparse(calls[0]).hostname, 'www.wikidata.org')

    def test_english_wikipedia_fallback_fits_remaining_request_budget(self):
        calls = []
        def fetch(url, provider):
            calls.append(url); args = parse_qs(urlparse(url).query)
            if args.get('action') == ['wbsearchentities']:
                return {'search': [{'id': 'Q1', 'label': 'عنوان', 'description': 'مسلسل تلفزيوني'}]}
            if args.get('action') == ['wbgetentities']:
                return {'entities': {'Q1': {'claims': claims(P31=[{'id': 'Q5398426'}]),
                    'sitelinks': {'arwiki': {'title': 'عنوان'}, 'enwiki': {'title': 'An English article'}}}}}
            if urlparse(url).hostname == 'ar.wikipedia.org':
                return {'query': {'pages': {'-1': {'title': 'عنوان', 'missing': ''}}}}
            return {'query': {'pages': {'1': {'title': 'An English article', 'extract': 'An available synopsis.'}}}}
        result = search_wikidata('عنوان', 'tv', fetch)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result[0]['description'], 'An available synopsis.')

    def test_malformed_rows_do_not_leak_unsafe_claims_or_links(self):
        def fetch(url, provider):
            if parse_qs(urlparse(url).query).get('action') == ['wbsearchentities']:
                return {'search': [None, {'id': 'https://evil.example'}, {'id': 'Q1', 'label': 'Film', 'description': '2000 film'}]}
            return {'entities': {'Q1': {'claims': claims(P345=['javascript:alert(1)']), 'sitelinks': []}}}
        item = search_wikidata('Film', 'movie', fetch)[0]
        self.assertEqual(item['imdbId'], '')
        self.assertEqual(item['sourceUrl'], 'https://www.wikidata.org/wiki/Q1')
        for query, kind in [('x', 'movie'), ('A title', 'album'), (None, 'tv')]:
            with self.assertRaises(ValueError):
                search_wikidata(query, kind, lambda *_: self.fail('Invalid search reached provider'))


if __name__ == '__main__':
    unittest.main()
