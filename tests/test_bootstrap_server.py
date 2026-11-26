from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import bootstrap_server as bootstrap
import package_server as package


class FakeRunner:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.user_created = False

    def user(self, _name):
        if not self.user_created:
            raise KeyError
        return SimpleNamespace(pw_uid=123, pw_gid=123)

    def group(self, _name):
        if not self.user_created:
            raise KeyError
        return SimpleNamespace(gr_gid=123)

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == 'useradd':
            self.user_created = True
        if argv[:3] == ['apt-get', 'install', '--yes'] and argv[-1] == 'caddy':
            path = self.root / 'etc/caddy/Caddyfile'
            path.parent.mkdir(parents=True)
            path.write_text('fresh package default')
        if argv[0] == 'gpg':
            Path(argv[argv.index('--output') + 1]).write_bytes(b'binary signing key')
        if argv[0] == 'curl' and argv[-1] == bootstrap.CADDY_KEY_URL:
            return SimpleNamespace(stdout=b'-----BEGIN PGP PUBLIC KEY BLOCK-----\npublic test key')
        return SimpleNamespace(stdout=b'{"status":"ok"}')


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        for name in package.SERVER_FILES:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('reviewed input ' + name)
        self.archive = self.base / 'trace-server.tar.gz'
        self.digest = package.package_server(self.source, self.archive)['sha256']
        self.root = self.base / 'vps'
        (self.root / 'etc').mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        (self.root / 'run/systemd/system').mkdir(parents=True)
        self.runner = FakeRunner(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, **kwargs):
        options = {'archive': self.archive, 'expected_sha256': self.digest,
                   'domain': 'trace.acwa.app', 'release': 'beta-20261113'}
        options.update(kwargs)
        return bootstrap.bootstrap(**options)

    def apply(self, **kwargs):
        return self.call(apply=True, system_root=self.root, runner=self.runner,
                         user_lookup=self.runner.user, group_lookup=self.runner.group, **kwargs)

    def test_default_dry_run_validates_archive_without_changes_or_commands(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        result = self.call(runner=self.runner, system_root=self.root)
        self.assertEqual(result['mode'], 'dry-run')
        self.assertFalse(result['changesApplied'])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))
        self.assertTrue(any('offsite' in item for item in result['pending']))

    def test_invalid_inputs_or_checksum_fail_before_any_commands(self):
        for values in ({'domain': 'https://trace.acwa.app'}, {'domain': 'trace.acwa.app;id'},
                       {'domain': '127.0.0.1'}, {'domain': 'trace.example.com'},
                       {'release': '../escape'}, {'expected_sha256': '0' * 64},
                       {'archive': Path('relative.tar.gz')}):
            with self.subTest(values=values), self.assertRaises((bootstrap.BootstrapError, OSError)):
                self.call(runner=self.runner, system_root=self.root, **values)
        self.assertEqual(self.runner.calls, [])

    @patch('bootstrap_server.platform.system', return_value='Linux')
    @patch('bootstrap_server.os.geteuid', return_value=0)
    def test_existing_install_refuses_before_apt(self, *_):
        (self.root / 'etc/trace').mkdir()
        (self.root / 'etc/trace/trace.env').write_text('must remain')
        with self.assertRaises(bootstrap.BootstrapError):
            self.apply()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual((self.root / 'etc/trace/trace.env').read_text(), 'must remain')

    @patch('bootstrap_server.platform.system', return_value='Linux')
    @patch('bootstrap_server.os.geteuid', return_value=0)
    def test_existing_caddy_and_foreign_user_refuse_before_apt(self, *_):
        (self.root / 'etc/caddy').mkdir()
        with self.assertRaises(bootstrap.BootstrapError):
            self.apply()
        self.assertEqual(self.runner.calls, [])

    @patch('bootstrap_server.platform.system', return_value='Darwin')
    def test_apply_cannot_run_on_mac(self, *_):
        with self.assertRaises(bootstrap.BootstrapError):
            self.apply()
        self.assertEqual(self.runner.calls, [])

    @patch('bootstrap_server.platform.system', return_value='Linux')
    @patch('bootstrap_server.os.geteuid', return_value=0)
    @patch('bootstrap_server.os.chown')
    def test_fresh_apply_uses_safe_commands_private_env_and_preserves_caddy_default(self, chown, *_):
        result = self.apply()
        self.assertEqual(result['mode'], 'applied')
        self.assertEqual(result['httpsHealth'], 'passed')
        env = self.root / 'etc/trace/trace.env'
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertIn('TRACE_SIGNUP_MODE=invite', env.read_text())
        self.assertIn('TRACE_PUBLIC_ORIGIN=https://trace.acwa.app', env.read_text())
        self.assertEqual((self.root / 'etc/caddy/Caddyfile.pre-trace-beta-20261113.bak').read_text(), 'fresh package default')
        self.assertEqual((self.root / 'opt/trace/current').resolve(), self.root / 'opt/trace/releases/beta-20261113')
        self.assertEqual(chown.call_count, 2)
        self.assertTrue(all(isinstance(argv, list) for argv, _ in self.runner.calls))
        self.assertFalse(any(argv[0] in ('sh', 'bash', 'rm', 'ufw', 'iptables') for argv, _ in self.runner.calls))
        self.assertTrue(any(argv[:2] == ['caddy', 'validate'] for argv, _ in self.runner.calls))
        self.assertTrue(any(argv[-1] == 'https://trace.acwa.app/api/health' for argv, _ in self.runner.calls))
        public = [argv for argv, _ in self.runner.calls if argv[-1] == 'https://trace.acwa.app/api/health']
        self.assertEqual(len(public), 1)
        retry = public[0].index('--retry')
        self.assertEqual(public[0][retry:retry + 5], ['--retry', '12', '--retry-delay', '5', '--retry-all-errors'])
        self.assertTrue(any('native signing' in item for item in result['pending']))

    @patch('bootstrap_server.platform.system', return_value='Linux')
    @patch('bootstrap_server.os.geteuid', return_value=0)
    def test_existing_account_refuses_before_apt(self, *_):
        self.runner.user_created = True
        with self.assertRaises(bootstrap.BootstrapError):
            self.apply()
        self.assertEqual(self.runner.calls, [])

    @patch('bootstrap_server.platform.system', return_value='Linux')
    @patch('bootstrap_server.os.geteuid', return_value=0)
    @patch('bootstrap_server.os.chown')
    def test_restrictive_root_umask_does_not_hide_code_from_service(self, *_):
        previous = os.umask(0o077)
        try:
            self.apply()
        finally:
            os.umask(previous)
        for name in ['opt/trace', 'opt/trace/releases', 'opt/trace/releases/beta-20261113/assets']:
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.root / 'opt/trace/releases/beta-20261113/server.py').stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.root / 'etc/trace/trace.env').stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
