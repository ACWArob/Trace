import json
from pathlib import Path
import tempfile
import unittest

from catalog_store import CatalogStore
from movie_catalog import (EXCLUDED_REASON, FILM_CLASSES, WIKIDATA_SOURCE,
                           is_movie_metadata, is_movie_result, reconcile_movie_catalog)


def claim(class_id):
    return {'mainsnak': {'datavalue': {'value': {'id': class_id}}}}


class MovieEligibilityTests(unittest.TestCase):
    def test_film_derived_games_albums_and_other_media_are_not_films(self):
        descriptions = [
            '1994 video game based on the animated film',
            'video-game adaptation of a 1988 film',
            '1999 soundtrack album for the film',
            'film soundtrack composed by John Williams',
            '2010 film score album',
            'American film series',
            'American film franchise',
            'series of six science fiction films',
            'American media franchise based on films',
            'novel based on a 1982 film',
            'book about the making of a film',
            'song from the 2001 film',
            'television series based on the film',
            'TV episode that parodies the film',
            'television miniseries related to the film',
            'annual film festival',
            'French film director',
        ]
        for description in descriptions:
            with self.subTest(description=description):
                self.assertFalse(is_movie_result({'description': description}))

    def test_film_can_mention_a_game_novel_or_soundtrack_after_its_type(self):
        for description in ['2023 film based on a video game', '1999 film based on a novel',
                            'American musical film with a soundtrack album',
                            '2014 science fiction film directed by Christopher Nolan',
                            'animated television film', '2020 film']:
            with self.subTest(description=description):
                self.assertTrue(is_movie_result({'description': description}))

    def test_documented_film_claims_allow_missing_or_non_english_descriptions(self):
        for class_id in FILM_CLASSES:
            with self.subTest(class_id=class_id):
                self.assertTrue(is_movie_result({'description': ''}, {'claims': {'P31': [claim(class_id)]}}))
        self.assertFalse(is_movie_result({'description': ''}, {'claims': {'P31': [claim('Q1')]}}))
        self.assertFalse(is_movie_result({'description': 'video game based on film'},
                                        {'claims': {'P31': [claim('Q11424')]}}))

    def test_wikipedia_titles_and_plot_relations_do_not_become_false_types(self):
        for title, description in [
            ('The Game', 'The Game is a 1997 American thriller film directed by David Fincher.'),
            ('The Album', 'The Album is a 2010 film based on a book.'),
            ('The Soundtrack', 'The Soundtrack is a 2012 documentary film about a song.'),
            ('Film Stars Don’t Die in Liverpool', 'Film Stars Don’t Die in Liverpool is a 2017 romantic drama film.'),
        ]:
            item = {'title': title, 'description': description, 'source': WIKIDATA_SOURCE, 'type': 'movie'}
            with self.subTest(title=title):
                self.assertTrue(is_movie_metadata(item))
                self.assertTrue(is_movie_result({'label': title, 'description': description}))

    def test_only_known_wikidata_non_movies_are_hidden_in_old_metadata(self):
        for description in ['', 'An unusual work.', 'Screen adaptation of a novel',
                            'A film about an album', 'A short feature film released in 1988']:
            self.assertTrue(is_movie_metadata({'type': 'movie', 'source': WIKIDATA_SOURCE, 'description': description}))
        for source, kind in [('IMDb', 'movie'), ('Personal memory', 'memory'), (WIKIDATA_SOURCE, 'book')]:
            self.assertTrue(is_movie_metadata({'type': kind, 'source': source, 'description': 'soundtrack album for the film'}))
        self.assertFalse(is_movie_metadata({'title': 'Arrival', 'type': 'movie', 'source': WIKIDATA_SOURCE,
                                           'description': 'Arrival is a 2016 soundtrack album for the film.'}))

    def test_malformed_provider_rows_do_not_raise(self):
        for hit, entity in [(None, None), ([], {}), ({'description': []}, []),
                            ({}, {'claims': {'P31': [None, [], {'mainsnak': []}]}}),
                            ({}, {'claims': {'P31': [claim(['Q11424'])]}}),
                            ({}, {'claims': []})]:
            self.assertFalse(is_movie_result(hit, entity))

    def test_people_and_organizations_cannot_become_films_via_biography_words(self):
        for class_id in ['Q93204', 'Q20650540', 'Q29168811']:
            self.assertTrue(is_movie_result({'description':'فيلم جزائري'}, {'claims': {'P31':[claim(class_id)]}}))
        for class_id in ['Q5', 'Q43229']:
            self.assertFalse(is_movie_result({'description': '2014 film'}, {'claims': {'P31': [claim(class_id)]}}))
        for description in ['Algerian actor known for films', 'American writer of films',
                            'actor and musician working in film', 'television actor known for films',
                            'film production studio', 'production company known for films',
                            'movie studio that made a film franchise', 'French film director',
                            'ممثل جزائري معروف بأفلامه']:
            with self.subTest(description=description):
                self.assertFalse(is_movie_result({'description': description}))
                self.assertFalse(is_movie_metadata({'type': 'movie', 'source': WIKIDATA_SOURCE,
                                                    'description': description}))
        for description in ['2020 film about an actor', '2016 film by a director',
                            '2023 film about a writer and musician', 'فيلم جزائري عن ممثل']:
            with self.subTest(description=description):
                self.assertTrue(is_movie_result({'description': description}, {'claims': {'P31': [claim('Q11424')]}}))

    def test_proper_titles_do_not_create_person_type_false_positives(self):
        self.assertTrue(is_movie_result({'label': 'The Writer', 'description': 'The Writer is a 2010 film about a writer.'}))
        self.assertTrue(is_movie_metadata({'title': 'The Artist', 'type': 'movie', 'source': WIKIDATA_SOURCE,
                                          'description': 'The Artist is a 2011 film about an actor.'}))


class MovieCatalogReconciliationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.catalog = CatalogStore(Path(directory.name) / 'test.sqlite3')

    def row(self, identifier, description, source=WIKIDATA_SOURCE, kind='movie', marker=None):
        item = {'title': 'A title ' + identifier, 'type': kind, 'source': source, 'externalId': identifier,
                'description': description, 'sourceUrl': 'https://www.wikidata.org/wiki/' + identifier,
                'creator': '', 'genres': [], 'year': '2000'}
        self.catalog.save(kind, identifier, {'items': [item]})
        if marker:
            with self.catalog.connect() as db:
                row = db.execute('SELECT item_key,payload FROM catalog WHERE item_key LIKE ?', ('%:' + identifier,)).fetchone()
                payload = json.loads(row['payload']); payload['catalogExcludedReason'] = marker
                db.execute('UPDATE catalog SET payload=? WHERE item_key=?', (json.dumps(payload), row['item_key']))

    def test_reconciliation_is_reversible_idempotent_and_never_deletes(self):
        self.row('Q1', '2000 video game based on the film')
        self.row('Q2', '2000 film based on a video game', marker=EXCLUDED_REASON)
        self.row('Q3', '2000 film', marker='Other audit owns this marker')
        self.row('Q4', 'soundtrack album for a film', source='Other provider')
        self.row('Q5', 'novel based on a film', kind='book')
        with self.catalog.connect() as db:
            db.execute('CREATE TABLE private_test (payload TEXT)')
            db.execute('INSERT INTO private_test VALUES(?)', ('Synthetic private note',))
            before = db.execute('SELECT count(*) FROM catalog').fetchone()[0]
        counts = reconcile_movie_catalog(self.catalog)
        self.assertEqual(counts, {'checked': 3, 'excluded': 1, 'restored': 1})
        with self.catalog.connect() as db:
            rows = {json.loads(row['payload'])['externalId']: json.loads(row['payload'])
                    for row in db.execute('SELECT payload FROM catalog')}
            self.assertEqual(db.execute('SELECT count(*) FROM catalog').fetchone()[0], before)
            self.assertEqual(db.execute('SELECT payload FROM private_test').fetchone()[0], 'Synthetic private note')
        self.assertEqual(rows['Q1']['catalogExcludedReason'], EXCLUDED_REASON)
        self.assertNotIn('catalogExcludedReason', rows['Q2'])
        self.assertEqual(rows['Q3']['catalogExcludedReason'], 'Other audit owns this marker')
        self.assertNotIn('catalogExcludedReason', rows['Q4'])
        self.assertNotIn('catalogExcludedReason', rows['Q5'])
        self.assertEqual(self.catalog.browse('all')['total'], 3)
        self.assertIsNone(self.catalog.matching('movie', 'A title Q1', '2000'))
        self.assertIsNotNone(self.catalog.matching('movie', 'A title Q2', '2000'))
        self.assertEqual(reconcile_movie_catalog(self.catalog), {'checked': 3, 'excluded': 0, 'restored': 0})
        with self.catalog.connect() as db:
            row = db.execute("SELECT item_key,payload FROM catalog WHERE json_extract(payload,'$.externalId')='Q1'").fetchone()
            item = json.loads(row['payload']); item['description'] = '2000 film'
            db.execute('UPDATE catalog SET payload=? WHERE item_key=?', (json.dumps(item), row['item_key']))
        self.assertEqual(reconcile_movie_catalog(self.catalog), {'checked': 3, 'excluded': 0, 'restored': 1})

    def test_reconciliation_soft_flags_people_without_deleting_metadata(self):
        self.row('Q1', 'Algerian actor known for films')
        self.row('Q2', '2010 film about an Algerian actor')
        self.assertEqual(reconcile_movie_catalog(self.catalog), {'checked': 2, 'excluded': 1, 'restored': 0})
        self.assertEqual(self.catalog.browse('movie')['total'], 1)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM catalog').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
