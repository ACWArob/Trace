"""Vercel hosting configuration: rewrite, headers, cron, static staging, entry point."""
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import production  # noqa: E402
import server  # noqa: E402

CONFIG = json.loads((ROOT / 'vercel.json').read_text(encoding='utf-8'))
SHELL_CACHE = 'public, max-age=0, must-revalidate'


def sw_shell_files():
    source = (ROOT / 'sw.js').read_text(encoding='utf-8')
    block = re.search(r'const SHELL_FILES = \[(.*?)\];', source, re.S)[1]
    return re.findall(r"'([^']+)'", block)


def headers_for(path):
    """Headers vercel.json applies to ``path`` (sources here are plain regexes)."""
    result = {}
    for rule in CONFIG['headers']:
        if re.fullmatch(rule['source'], path):
            for header in rule['headers']:
                result.setdefault(header['key'], []).append(header['value'])
    return result


def stage(target):
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'build_public.py'), '--out', str(target)],
                   check=True, capture_output=True, cwd=ROOT)
    return {'/' + path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()}


class VercelConfigTests(unittest.TestCase):
    def test_api_rewrite_has_no_named_parameter(self):
        self.assertEqual(CONFIG['rewrites'], [{'source': '/api/(.*)', 'destination': '/api/index'}])

    def test_static_output_is_the_staged_public_directory(self):
        self.assertIsNone(CONFIG['framework'])
        self.assertEqual(CONFIG['outputDirectory'], 'public')
        self.assertIn('scripts/build_public.py', CONFIG['buildCommand'])
        self.assertIn('public/', (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines())

    def test_function_is_bounded(self):
        function = CONFIG['functions']['api/index.py']
        self.assertTrue(10 <= function['maxDuration'] <= 60)
        self.assertNotIn('runtime', function)  # The official Python runtime is detected from the file.

    def test_every_path_gets_security_headers(self):
        for path in ['/', '/index.html', '/api/health', '/core.mjs', '/assets/trace-icon.svg']:
            headers = headers_for(path)
            self.assertEqual(headers['X-Content-Type-Options'], ['nosniff'], path)
            self.assertEqual(headers['Referrer-Policy'], ['strict-origin-when-cross-origin'], path)

    def test_api_is_never_cached_and_shell_files_revalidate(self):
        self.assertEqual(headers_for('/api/library')['Cache-Control'], ['no-store'])
        for path in sorted(server.PUBLIC_FILES | {'/assets/trace-icon.svg'}):
            self.assertEqual(headers_for(path).get('Cache-Control'), [SHELL_CACHE], path)
        for path in ['/server.py', '/api/index', '/assets/a/b.png']:
            self.assertNotIn(SHELL_CACHE, headers_for(path).get('Cache-Control', []), path)

    def test_module_scripts_are_served_as_javascript(self):
        for path in ['/core.mjs', '/library_sync.mjs']:
            self.assertEqual(headers_for(path)['Content-Type'], ['text/javascript; charset=utf-8'])
        self.assertNotIn('Content-Type', headers_for('/app.js'))

    def test_maintenance_cron_runs_daily(self):
        self.assertEqual(sorted(cron['path'] for cron in CONFIG['crons']), ['/api/cron/covers', '/api/cron/maintenance'])
        cron = next(cron for cron in CONFIG['crons'] if cron['path'] == '/api/cron/maintenance')
        minute, hour, day, month, weekday = cron['schedule'].split()
        self.assertTrue(minute.isdigit() and hour.isdigit(), cron['schedule'])
        self.assertEqual((day, month, weekday), ('*', '*', '*'))
        self.assertNotIn('/api/cron/maintenance', server.PUBLIC_FILES)

    def test_cover_cron_runs_hourly(self):
        cron = next(cron for cron in CONFIG['crons'] if cron['path'] == '/api/cron/covers')
        minute, hour, day, month, weekday = cron['schedule'].split()
        self.assertTrue(minute.isdigit() and int(minute) < 60, cron['schedule'])
        self.assertEqual((hour, day, month, weekday), ('*', '*', '*', '*'))
        self.assertNotIn('/api/cron/covers', server.PUBLIC_FILES)

    def test_build_stages_only_the_allowlisted_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            staged = stage(Path(folder) / 'public')
        assets = {'/assets/' + path.name for path in (ROOT / 'assets').iterdir()
                  if path.is_file() and re.fullmatch(r'[\w.-]+', path.name)}
        self.assertEqual(staged, (server.PUBLIC_FILES - {'/'}) | assets)
        self.assertLessEqual(set(sw_shell_files()), staged)
        self.assertFalse({path for path in staged if path.endswith(('.py', '.md', '.sql', '.sqlite3'))})

    def test_build_stamps_the_offline_cache_with_the_shell_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'public'
            stage(target)
            first = (target / 'sw.js').read_text(encoding='utf-8')
            self.assertRegex(first, r'const CACHE_NAME = `\$\{CACHE_PREFIX\}v\d+-[0-9a-f]{12}`;')
            stage(target)
            self.assertEqual((target / 'sw.js').read_text(encoding='utf-8'), first)  # Stable for the same shell.

    def test_build_replaces_a_stale_output_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'public'
            target.mkdir()
            (target / 'server.py').write_text('stale')
            self.assertNotIn('/server.py', stage(target))

    def test_upload_excludes_private_and_development_files(self):
        ignored = (ROOT / '.vercelignore').read_text(encoding='utf-8').splitlines()
        for entry in ['tests/', 'docs/', 'deploy/', 'native/', '.trace/', '*.sqlite3', '.env*', 'scripts/*']:
            self.assertIn(entry, ignored)
        self.assertIn('!scripts/build_public.py', ignored)
        self.assertNotIn('requirements.txt', ignored)


class EntryPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('trace_vercel_entry', ROOT / 'api' / 'index.py')
        cls.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.entry)

    def call(self, query):
        seen = {}

        def application(environ, start_response):
            seen.update(environ)
            start_response('204 No Content', [])
            return []

        environ = {'REQUEST_METHOD': 'GET', 'PATH_INFO': '/api/library', 'QUERY_STRING': query,
                   'wsgi.input': io.BytesIO()}
        with patch.object(production, 'application', application):
            self.entry.app(environ, lambda status, headers, exc_info=None: None)
        return seen['QUERY_STRING']

    def test_exports_the_production_application(self):
        self.assertTrue(callable(self.entry.app))
        self.assertEqual(self.call('after=3&limit=200'), 'after=3&limit=200')

    def test_rewrite_parameters_are_stripped_from_the_query(self):
        self.assertEqual(self.call('path=library'), '')
        self.assertEqual(self.call('after=3&path=library&limit=2'), 'after=3&limit=2')
        self.assertEqual(self.call('0=library&q=a%26b'), 'q=a%26b')


if __name__ == '__main__':
    unittest.main()
