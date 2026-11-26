"""Dry-run-first, first-install bootstrap for a fresh Ubuntu Trace server.

This script is an operator tool, not an updater. Run on the VPS after reviewing
the plan; it never changes SSH access or firewall rules.
"""
import argparse
from hashlib import sha256
import hmac
import io
import json
import os
from pathlib import Path
import platform
import pwd
import grp
import re
import stat
import subprocess
import tarfile

from package_server import (MAX_TOTAL_BYTES, SERVER_FILES, MANIFEST,
                            regular_directory, verify_package_data)

CADDY_KEY_URL = 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key'
CADDY_REPO = ('deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] '
              'https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main\n')
PENDING = [
    'Point the domain DNS to this VPS and verify HTTPS from a phone with the laptop off.',
    'Configure provider keys privately and transfer only the reviewed public catalog.',
    'Configure encrypted offsite backups and complete an isolated restore drill.',
    'Create private single-use invites and test accounts, recovery, sync, and photo memories.',
    'Configure native signing, platform distribution, and physical-device release checks.',
]


class BootstrapError(ValueError):
    pass


def configuration(archive, expected_sha256, domain, release):
    if not isinstance(domain, str) or len(domain) > 253 or domain != domain.lower():
        raise BootstrapError('Use a lowercase DNS hostname without scheme or port.')
    labels = domain.split('.')
    if len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in labels):
        raise BootstrapError('Use a DNS hostname without scheme, path, port, or wildcards.')
    if not re.fullmatch(r'[a-z]{2,63}', labels[-1]) or labels[-1] in ('local', 'localhost', 'invalid', 'test') or '.'.join(labels[-2:]) in ('example.com', 'example.net', 'example.org'):
        raise BootstrapError('Use the actual public DNS hostname for Trace.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', release or ''):
        raise BootstrapError('Use a plain release identifier of at most 64 characters.')
    if not re.fullmatch(r'[a-fA-F0-9]{64}', expected_sha256 or ''):
        raise BootstrapError('Provide the expected archive SHA-256 digest.')
    path = Path(archive)
    if not path.is_absolute():
        raise BootstrapError('The archive path must be absolute.')
    regular_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TOTAL_BYTES:
            raise BootstrapError('Use a bounded regular release archive.')
        payload = handle.read(MAX_TOTAL_BYTES + 1)
    actual = sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256.lower()):
        raise BootstrapError('The archive SHA-256 does not match the expected release.')
    verified = verify_package_data(payload)
    return {'archive': str(path), 'sha256': actual, 'domain': domain,
            'release': release, 'files': verified['files']}, payload


def command(argv, *, env=None, data=None, capture=False):
    return subprocess.run(argv, shell=False, check=True, env=env, input=data,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, timeout=900,
                          umask=0o022)


class FirstInstall:
    def __init__(self, config, payload, *, system_root=Path('/'), runner=command,
                 user_lookup=pwd.getpwnam, group_lookup=grp.getgrnam):
        self.config, self.payload = config, payload
        self.system_root = Path(system_root)
        self.runner, self.user_lookup, self.group_lookup = runner, user_lookup, group_lookup
        self.release = self.path('/opt/trace/releases/' + config['release'])

    def path(self, name):
        return self.system_root / name.lstrip('/')

    def preflight(self):
        if platform.system() != 'Linux' or os.geteuid() != 0:
            raise BootstrapError('Apply requires root on a fresh Ubuntu Linux VPS.')
        release_file = self.path('/etc/os-release')
        values = dict(re.findall(r'^([A-Z_]+)=["\']?([^"\'\n]*)', release_file.read_text(), re.M))
        try:
            version = tuple(int(x) for x in values.get('VERSION_ID', '').split('.'))
        except ValueError:
            version = ()
        if values.get('ID') != 'ubuntu' or version < (24, 4):
            raise BootstrapError('Apply supports Ubuntu 24.04 or newer.')
        if not self.path('/run/systemd/system').is_dir() or not hasattr(tarfile, 'data_filter'):
            raise BootstrapError('Apply requires systemd and Python tarfile data filtering.')
        # All conflicts are checked before apt, user creation, or file writes.
        for name in ('/opt/trace', '/etc/trace', '/var/lib/trace', '/var/backups/trace',
                     '/etc/caddy', '/etc/systemd/system/trace.service',
                     '/etc/systemd/system/caddy.service.d/trace-domain.conf',
                     '/usr/share/keyrings/caddy-stable-archive-keyring.gpg',
                     '/etc/apt/sources.list.d/caddy-stable.list'):
            target = self.path(name)
            if target.exists() or target.is_symlink():
                raise BootstrapError('This is not a fresh installation; use the reviewed upgrade runbook.')
        for lookup in (self.user_lookup, self.group_lookup):
            try:
                lookup('trace')
            except KeyError:
                continue
            raise BootstrapError('A trace system user or group already exists; review it manually.')

    def mkdir(self, name, mode=0o755):
        target = self.path(name)
        missing, current = [], target
        while not current.exists():
            if current.is_symlink():
                raise BootstrapError('A destination directory is a symlink.')
            missing.append(current)
            current = current.parent
        regular_directory(current)
        for directory in reversed(missing):
            directory.mkdir(mode=mode)
            directory.chmod(mode)
        regular_directory(target)
        target.chmod(mode)
        return target

    def write(self, name, data, mode=0o644):
        target = self.path(name)
        regular_directory(target.parent)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data if isinstance(data, bytes) else data.encode())
            handle.flush()
            os.fsync(handle.fileno())
        return target

    def extract(self):
        self.mkdir('/opt/trace/releases/' + self.config['release'])
        with tarfile.open(fileobj=io.BytesIO(self.payload), mode='r|gz') as archive:
            for member in archive:
                name = member.name.removeprefix('trace/')
                if name not in (*SERVER_FILES, MANIFEST) or not member.isfile():
                    raise BootstrapError('The verified archive contains an unsafe entry.')
                member.name = name
                safe = tarfile.data_filter(member, str(self.release))
                if safe is None or not safe.isfile():
                    raise BootstrapError('The archive entry failed extraction policy.')
                target = self.release / name
                self.mkdir('/' + str(target.parent.relative_to(self.system_root)))
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
                with os.fdopen(fd, 'wb') as handle:
                    os.fchmod(handle.fileno(), 0o644)
                    handle.write(archive.extractfile(member).read())
        self.path('/opt/trace/current').symlink_to(self.release, target_is_directory=True)

    def apply(self):
        self.preflight()
        env = {**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'}
        self.runner(['apt-get', 'update'], env=env)
        self.runner(['apt-get', 'install', '--yes', 'python3', 'python3-venv',
                     'debian-keyring', 'debian-archive-keyring', 'apt-transport-https',
                     'curl', 'gnupg', 'ca-certificates', 'age', 'openssh-client'], env=env)
        key = self.runner(['curl', '--fail', '--silent', '--show-error', '--location',
                           '--proto', '=https', '--tlsv1.2', '--max-filesize', '1048576',
                           CADDY_KEY_URL], capture=True).stdout
        if not key.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----') or len(key) > 1048576:
            raise BootstrapError('The Caddy signing key response is invalid.')
        self.mkdir('/usr/share/keyrings')
        key_path = self.path('/usr/share/keyrings/caddy-stable-archive-keyring.gpg')
        self.runner(['gpg', '--batch', '--dearmor', '--output', str(key_path)], data=key)
        key_path.chmod(0o644)
        self.mkdir('/etc/apt/sources.list.d')
        self.write('/etc/apt/sources.list.d/caddy-stable.list', CADDY_REPO)
        self.runner(['apt-get', 'update'], env=env)
        self.runner(['apt-get', 'install', '--yes', 'caddy'], env=env)
        self.runner(['useradd', '--system', '--user-group', '--home-dir', '/var/lib/trace',
                     '--shell', '/usr/sbin/nologin', 'trace'])
        account = self.user_lookup('trace')
        for name in ('/var/lib/trace', '/var/backups/trace'):
            target = self.mkdir(name, 0o700)
            os.chown(target, account.pw_uid, account.pw_gid)
        self.mkdir('/etc/trace', 0o700)
        self.extract()
        self.runner(['python3', '-m', 'venv', str(self.path('/opt/trace/venv'))])
        python = str(self.path('/opt/trace/venv/bin/python'))
        self.runner([python, '-m', 'pip', 'install', '-r', str(self.release / 'requirements-runtime.txt')])
        settings = {'TRACE_ENV': 'production', 'TRACE_PUBLIC_ORIGIN': 'https://' + self.config['domain'],
                    'TRACE_TRUST_PROXY': '1', 'TRACE_SIGNUP_MODE': 'invite',
                    'TRACE_DATA_DIR': '/var/lib/trace', 'TRACE_LIBRARY_BYTES': '104857600'}
        self.write('/etc/trace/trace.env', ''.join(k + '=' + v + '\n' for k, v in settings.items()), 0o600)
        self.mkdir('/etc/systemd/system')
        self.write('/etc/systemd/system/trace.service', (self.release / 'deploy/trace.service').read_bytes())
        self.mkdir('/etc/systemd/system/caddy.service.d')
        self.write('/etc/systemd/system/caddy.service.d/trace-domain.conf',
                   '[Service]\nEnvironment=TRACE_DOMAIN=' + self.config['domain'] + '\n')
        self.mkdir('/etc/caddy')
        caddy_file = self.path('/etc/caddy/Caddyfile')
        if caddy_file.exists():
            if caddy_file.is_symlink() or not caddy_file.is_file():
                raise BootstrapError('The newly installed Caddy configuration is unsafe.')
            # Preserve the package's fresh default before installing Trace's file.
            backup = self.write('/etc/caddy/Caddyfile.pre-trace-' + self.config['release'] + '.bak', caddy_file.read_bytes(), 0o600)
            if not backup.is_file():
                raise BootstrapError('Could not preserve the default Caddy configuration.')
            caddy_file.write_bytes((self.release / 'deploy/Caddyfile').read_bytes())
            caddy_file.chmod(0o644)
        else:
            self.write('/etc/caddy/Caddyfile', (self.release / 'deploy/Caddyfile').read_bytes())
        runtime_env = {**os.environ, **settings}
        self.runner(['runuser', '--user', 'trace', '--', python,
                     str(self.release / 'production.py'), '--check'], env=runtime_env)
        self.runner(['caddy', 'validate', '--config', str(caddy_file), '--adapter', 'caddyfile'],
                    env={**os.environ, 'TRACE_DOMAIN': self.config['domain']})
        self.runner(['systemctl', 'daemon-reload'])
        self.runner(['systemctl', 'enable', '--now', 'trace'])
        self.runner(['systemctl', 'enable', 'caddy'])
        self.runner(['systemctl', 'restart', 'caddy'])
        self.health('http://127.0.0.1:5173/api/health', ['--header', 'Host: ' + self.config['domain']])
        try:
            # Caddy may still be issuing the certificate; TLS errors need --retry-all-errors.
            self.health('https://' + self.config['domain'] + '/api/health',
                        retry=('--retry', '12', '--retry-delay', '5', '--retry-all-errors'))
        except (subprocess.SubprocessError, BootstrapError):
            raise BootstrapError('Services were installed, but public HTTPS health did not pass. Check DNS, ports 80/443, and Caddy before inviting anyone.')
        return {**self.config, 'mode': 'applied', 'httpsHealth': 'passed', 'pending': PENDING}

    def health(self, url, extra=(), retry=('--retry', '5', '--retry-delay', '1', '--retry-connrefused')):
        result = self.runner(['curl', '--fail', '--silent', '--show-error', '--max-time', '10',
                              *retry, *extra, url], capture=True)
        try:
            ready = json.loads(result.stdout).get('status') == 'ok'
        except (ValueError, AttributeError):
            ready = False
        if not ready:
            raise BootstrapError('The Trace health endpoint did not report ready.')


def bootstrap(archive, expected_sha256, domain, release, *, apply=False, **options):
    config, payload = configuration(archive, expected_sha256, domain, release)
    if not apply:
        return {**config, 'mode': 'dry-run', 'changesApplied': False,
                'steps': ['Require a fresh Ubuntu 24.04+ root/systemd VPS with no existing Trace/Caddy installation.',
                          'Install Python, age, OpenSSH client, and official signed-repository Caddy packages.',
                          'Create unprivileged trace service, persistent private storage, and a verified release.',
                          'Install pinned virtual-environment runtime and invite-only production configuration.',
                          'Validate and start Trace/Caddy services, then test local and public HTTPS health.'],
                'pending': PENDING}
    return FirstInstall(config, payload, **options).apply()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--apply', action='store_true', help='Apply the reviewed first install on a fresh Ubuntu VPS.')
    args = parser.parse_args(argv)
    try:
        result = bootstrap(args.archive, args.sha256, args.domain, args.release, apply=args.apply)
    except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        message = str(error) if isinstance(error, BootstrapError) else 'Bootstrap failed. No automatic cleanup was performed; inspect the server before retrying.'
        parser.exit(1, message + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
