"""Consistent Trace SQLite snapshots and explicit restore-to-new-file operations.

Uses only the Python standard library. Optional encrypted/offsite backups use the
system age and OpenSSH sftp executables; no credentials are read or logged here.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid


class BackupError(ValueError):
    pass


def default_source():
    directory = Path(os.environ.get('TRACE_DATA_DIR', Path(__file__).resolve().parents[1] / '.trace'))
    return directory / 'trace.sqlite3'


def _regular_source(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise BackupError('Source must be an existing regular file, not a symlink.')
    return path


def _directory(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise BackupError('Destination directory must not be a symlink.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise BackupError('Destination must be a directory.')
    if stat.S_IMODE(path.stat().st_mode) & 0o022:
        raise BackupError('Destination directory must not be writable by other users.')
    return path


def _temporary(directory):
    fd, name = tempfile.mkstemp(prefix='.trace-backup-', suffix='.partial', dir=directory)
    os.fchmod(fd, 0o600)
    os.close(fd)
    return Path(name)


def _publish(temporary, destination):
    """Hard-link promotion is atomic and refuses all pre-existing destinations."""
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise BackupError('Destination already exists; no file was replaced.') from exc
    with Path(destination).open('rb') as handle:
        os.fsync(handle.fileno())
    directory_fd = os.open(Path(destination).parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def verify_database(path):
    path = _regular_source(path)
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            rows = db.execute('PRAGMA integrity_check').fetchall()
            if rows != [('ok',)]:
                raise BackupError('Database integrity check failed; no restore was published.')
    except sqlite3.Error as exc:
        raise BackupError('Source is not a readable, valid SQLite database.') from exc


def _snapshot(source, destination, timeout=120):
    if timeout <= 0:
        raise BackupError('Backup timeout must be positive.')
    source = _regular_source(source)
    deadline = time.monotonic() + timeout
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise BackupError('Database snapshot timed out; no backup was published.')
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=min(10, timeout))) as incoming:
            with closing(sqlite3.connect(destination)) as outgoing:
                incoming.backup(outgoing, pages=256, progress=progress, sleep=0.05)
                outgoing.execute('PRAGMA journal_mode=DELETE')
        verify_database(destination)
    except sqlite3.Error as exc:
        raise BackupError('Database snapshot failed; no backup was published.') from exc


def _executable(name):
    executable = shutil.which(name)
    if not executable:
        raise BackupError(f'{name} is required for encrypted/offsite backups.')
    return executable


def _age_to_file(arguments, source, output, timeout):
    with output.open('wb') as handle:
        result = subprocess.run([_executable('age'), *arguments, str(source)], stdout=handle,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
        handle.flush()
        os.fsync(handle.fileno())
    if result.returncode:
        raise BackupError('age could not encrypt/decrypt the backup; no output was published.')


def _remote_destination(value):
    if not isinstance(value, str) or ':' not in value:
        raise BackupError('Offsite target must be user@host:/absolute/remote/directory.')
    host, directory = value.split(':', 1)
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*', host):
        raise BackupError('Offsite target must use a plain SSH user and hostname.')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', directory) or any(x in ('.', '..') for x in directory.split('/')):
        raise BackupError('Offsite target must use a simple absolute directory without dot segments.')
    return host, directory.rstrip('/')


def _sftp_quote(value):
    value = str(value)
    if '\n' in value or '\r' in value:
        raise BackupError('Backup paths must not contain line breaks.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def upload_backup(path, remote, timeout=120):
    """Authenticated SFTP, with remote promotion only after a completed upload."""
    host, directory = _remote_destination(remote)
    final = directory + '/' + Path(path).name
    partial = final + '.partial'
    batch = f'put {_sftp_quote(path)} {_sftp_quote(partial)}\nrename {_sftp_quote(partial)} {_sftp_quote(final)}\n'
    result = subprocess.run([_executable('sftp'), '-oBatchMode=yes', '-oStrictHostKeyChecking=yes',
                             '-oConnectTimeout=15', '-b', '-', host], input=batch.encode(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if result.returncode:
        raise BackupError('Offsite upload failed. The local encrypted backup remains available.')


def create_backup(source, output_dir, *, recipient='', remote='', require_offsite=False, timeout=120):
    source = _regular_source(source)
    if require_offsite and (not recipient or not remote):
        raise BackupError('Scheduled backups require an age recipient and offsite SSH target.')
    if remote and not recipient:
        raise BackupError('Only encrypted backups may be sent offsite.')
    if recipient:
        if not isinstance(recipient, str) or not re.fullmatch(r'age1[0-9a-z]{20,100}', recipient):
            raise BackupError('Use an age public recipient key, never a private identity.')
        _executable('age')
    if remote:
        _remote_destination(remote)
        _executable('sftp')
    directory = _directory(output_dir)
    if shutil.disk_usage(directory).free < 128 * 1024 * 1024:
        raise BackupError('Less than 128 MiB is free in the backup directory.')
    name = 'trace-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.sqlite3'
    destination = directory / (name + ('.age' if recipient else ''))
    plain = _temporary(directory)
    encrypted = None
    try:
        _snapshot(source, plain, timeout)
        output = plain
        if recipient:
            encrypted = _temporary(directory)
            _age_to_file(['--encrypt', '--recipient', recipient], plain, encrypted, timeout)
            output = encrypted
        _publish(output, destination)
    finally:
        plain.unlink(missing_ok=True)
        if encrypted:
            encrypted.unlink(missing_ok=True)
    if remote:
        upload_backup(destination, remote, timeout)
    return {'backup': str(destination), 'encrypted': bool(recipient), 'offsite': bool(remote)}


def restore_backup(source, destination, *, identity='', timeout=120):
    source = _regular_source(source)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError('Restore destination must be new; existing databases are never replaced.')
    directory = _directory(destination.parent)
    temporary = _temporary(directory)
    try:
        if source.suffix == '.age':
            if not identity:
                raise BackupError('Encrypted restore requires a local age identity file.')
            identity_path = _regular_source(identity)
            if stat.S_IMODE(identity_path.stat().st_mode) & 0o077:
                raise BackupError('The age identity file must be readable only by its owner.')
            _age_to_file(['--decrypt', '--identity', str(identity_path)], source, temporary, timeout)
            verify_database(temporary)
        else:
            if identity:
                raise BackupError('An age identity is only used with .age backups.')
            _snapshot(source, temporary, timeout)
        _publish(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'restored': str(destination), 'integrity': 'ok'}


def _exit_on_sigterm(signum, _frame):
    raise SystemExit(128 + signum)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    backup = sub.add_parser('backup', help='Snapshot live SQLite; optionally encrypt and upload.')
    backup.add_argument('--source', type=Path, default=default_source())
    backup.add_argument('--output-dir', type=Path, required=True)
    backup.add_argument('--recipient', default=os.environ.get('TRACE_BACKUP_AGE_RECIPIENT', ''))
    backup.add_argument('--remote', default=os.environ.get('TRACE_BACKUP_REMOTE', ''))
    backup.add_argument('--require-offsite', action='store_true')
    backup.add_argument('--timeout', type=int, default=120)
    restore = sub.add_parser('restore', help='Restore only to a new path and verify integrity.')
    restore.add_argument('--source', type=Path, required=True)
    restore.add_argument('--destination', type=Path, required=True)
    restore.add_argument('--identity', default='')
    restore.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args(argv)
    try:
        # Exit through finally blocks so a stopped job leaves no plaintext staging file.
        previous_handler = signal.signal(signal.SIGTERM, _exit_on_sigterm)
        try:
            if args.command == 'backup':
                result = create_backup(args.source, args.output_dir, recipient=args.recipient,
                                       remote=args.remote, require_offsite=args.require_offsite, timeout=args.timeout)
            else:
                result = restore_backup(args.source, args.destination, identity=args.identity, timeout=args.timeout)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
    except (BackupError, OSError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, BackupError) else 'Backup operation failed; check file access and configured tools.'
        parser.exit(1, message + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
