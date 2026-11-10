import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_seed import catalog_coverage, load_seed_server, main, run_seed, seed_plan, write_report
from catalog_store import CatalogStore


class CatalogSeedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.catalog = CatalogStore(self.directory / 'public.sqlite3')

    def result(self, kind, query):
        return {'provider': 'Synthetic public provider', 'items': [{
            'type': kind, 'title': 'Public ' + query, 'externalId': query,
            'creator': 'Synthetic author', 'year': '2000', 'source': 'Synthetic',
            'sourceUrl': 'https://example.org/catalog', 'image': 'https://example.org/cover.jpg',
            'description': 'Public description', 'genres': ['Public genre'],
            'rating': 4, 'notes': 'PRIVATE', 'date': '2000-01-01',
            'attachments': [{'name': 'PRIVATE'}], 'history': ['2000-01-01'],
        }]}

    def test_fixed_plan_is_bounded_and_short_run_covers_every_category(self):
        plan = seed_plan()
        self.assertEqual(len(plan), 40)
        self.assertEqual(len(set(plan)), 40)
        self.assertEqual({kind for kind, _ in seed_plan(limit_queries=5)},
                         {'movie', 'tv', 'anime', 'book', 'album'})
        self.assertEqual(len(seed_plan('movie')), 12)
        self.assertEqual(len(seed_plan('album')), 8)
        for limit in (0, 41, True, 2.5):
            with self.assertRaises(ValueError): seed_plan(limit_queries=limit)
        with self.assertRaises(ValueError): seed_plan('memory')

    def test_international_default_includes_algerian_and_world_queries_without_country_guesses(self):
        plan = seed_plan()
        self.assertEqual(plan[0], ('movie', 'Chronicle of the Years of Fire'))
        for query in [('movie', 'The Battle of Algiers'), ('tv', 'Djemai Family'),
                      ('book', 'ذاكرة الجسد'), ('album', 'Souad Massi'),
                      ('movie', 'Pather Panchali'), ('book', 'Han Kang'),
                      ('album', 'Ryuichi Sakamoto'), ('anime', 'Mushishi')]:
            self.assertIn(query, plan)
        self.assertEqual(seed_plan(collection='classic')[0], ('movie', 'Interstellar'))
        self.assertEqual(len(seed_plan(collection='classic')), 40)
        with self.assertRaises(ValueError): seed_plan(collection='unknown')

    def test_dry_run_never_imports_server_or_reads_database(self):
        with patch('catalog_seed.load_seed_server', side_effect=AssertionError('No backend')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--limit-queries', '5']), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report['dryRun'])
        self.assertEqual(report['plannedQueries'], 5)
        self.assertEqual(report['collection'], 'international')

    def test_classic_collection_is_explicitly_selectable_without_backend_access(self):
        with patch('catalog_seed.load_seed_server', side_effect=AssertionError('No backend')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--collection', 'classic', '--kind', 'album', '--limit-queries', '1']), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['collection'], 'classic')
        self.assertEqual(report['queries'][0]['query'], 'Frank Ocean')

    def test_population_is_idempotent_and_persistent_cache_can_resume(self):
        fetched = []
        def search(kind, query):
            saved = self.catalog.cached(kind, query)
            if saved: return saved
            fetched.append((kind, query))
            return self.result(kind, query)
        first = run_seed(search, self.catalog, limit_queries=5)
        second = run_seed(search, self.catalog, limit_queries=5)
        self.assertEqual(len(fetched), 5)
        self.assertEqual(first['addedTitles'], 5)
        self.assertEqual(second['addedTitles'], 0)
        self.assertEqual(second['cachedQueries'], 5)
        self.assertEqual(second['after']['total'], 5)
        reopened = CatalogStore(self.catalog.path)
        self.assertEqual(catalog_coverage(reopened)['total'], 5)

    def test_partial_failures_continue_and_never_print_raw_exception(self):
        events = []
        def search(kind, query):
            if kind == 'tv': raise OSError('SECRET https://provider.example?apikey=PRIVATE')
            return self.result(kind, query)
        report = run_seed(search, self.catalog, limit_queries=5, progress=events.append)
        self.assertEqual(report['failedQueries'], 1)
        self.assertEqual(report['successfulQueries'], 4)
        self.assertEqual(report['after']['total'], 4)
        self.assertEqual(len(events), 5)
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertNotIn('PRIVATE', json.dumps(events))
        self.assertEqual([event for event in report['queries'] if event['kind'] == 'tv'][0]['errorType'], 'OSError')

    def test_private_fields_are_excluded_and_coverage_counts_are_measured(self):
        def search(kind, query):
            result = self.result(kind, query)
            if kind == 'album': result['items'][0].update(image='', description='', genres=[])
            return result
        report = run_seed(search, self.catalog, limit_queries=5)
        album = report['after']['categories']['album']
        self.assertEqual(album['total'], 1)
        self.assertEqual(album['missingCover'], 1)
        self.assertEqual(album['missingDescription'], 1)
        self.assertEqual(album['missingGenres'], 1)
        self.assertEqual(report['after']['categories']['movie']['withCover'], 1)
        for item in self.catalog.browse()['items']:
            for field in ('notes', 'rating', 'date', 'attachments', 'history'):
                self.assertNotIn(field, item)

    def test_invalid_worker_count_and_malformed_results_are_bounded(self):
        with self.assertRaises(ValueError): run_seed(self.result, self.catalog, workers=4)
        report = run_seed(lambda *args: {'items': 'bad'}, self.catalog, limit_queries=1)
        self.assertEqual(report['failedQueries'], 1)
        self.assertEqual(report['after']['total'], 0)

    def test_apply_loader_disables_credentialed_providers(self):
        with patch.dict(os.environ, {'OMDB_API_KEY': 'synthetic', 'MDBLIST_API_KEY': 'synthetic'}), \
                patch.dict(sys.modules, {'server': object()}):
            load_seed_server()
            self.assertNotIn('OMDB_API_KEY', os.environ)
            self.assertNotIn('MDBLIST_API_KEY', os.environ)

    def test_reports_are_public_json_and_never_replace_existing_files(self):
        path = self.directory / 'report.json'
        write_report(path, {'total': 5})
        self.assertEqual(json.loads(path.read_text()), {'total': 5})
        with self.assertRaises(ValueError): write_report(path, {'total': 6})
        self.assertEqual(json.loads(path.read_text()), {'total': 5})
        with self.assertRaises(ValueError): write_report(self.directory / '.env', {})

    def test_invalid_report_is_rejected_before_any_apply_writes(self):
        with patch('catalog_seed.load_seed_server', side_effect=AssertionError('No backend')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--apply', '--report', str(self.directory / '.env')]), 2)
            self.assertEqual(main(['--apply', '--report', str(self.directory / 'missing' / 'report.json')]), 2)


if __name__ == '__main__':
    unittest.main()
