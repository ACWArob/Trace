"""Package the reviewed server and public app files without copying user data."""
import argparse
import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

BACKEND_FILES = (
    'production.py', 'server.py', 'release_policy.py', 'catalog_store.py',
    'social.py', 'library_store.py', 'ratings.py', 'score_service.py',
    'profile_feeds.py', 'book_catalog.py', 'music_catalog.py',
    'movie_catalog.py', 'screen_catalog.py', 'catalog_seed.py',
    'requirements-runtime.txt',
)
PUBLIC_FILES = (
    'index.html', 'styles.css', 'app.js', 'core.mjs', 'data.js', 'views.mjs',
    'social.js', 'library_sync.mjs', 'api_client.mjs', 'artwork.mjs',
    'letterboxd_zip.mjs', 'tv_starters.mjs', 'collection_starters.mjs', 'cover_backfill.mjs',
    'mobile.js', 'sw.js', 'manifest.webmanifest', 'privacy.html', 'support.html',
)
ASSET_FILES = (
    'assets/trace-icon.svg', 'assets/trace-wordmark.png',
    'assets/trace-app-192.png', 'assets/trace-app-512.png', 'assets/sources.json',
) + tuple('assets/' + name + '.jpg' for name in (
    '1984', 'abbey-road', 'alchemist', 'amelie', 'arrival', 'bell-jar', 'blonde',
    'blue', 'breaking-bad', 'cafe', 'catcher', 'coast', 'coffee', 'cowboy-bebop',
    'creative-act', 'crying-hmart', 'dune-book', 'evangelion', 'fleabag',
    'grand-budapest', 'hobbit', 'in-rainbows', 'just-kids', 'kitchen', 'kyoto',
    'little-prince', 'melodrama', 'midnight-library', 'moonlight', 'museum',
    'normal-people', 'norwegian-wood', 'past-lives', 'perfect-days', 'rumours',
    'secret-history', 'severance', 'spirited-away', 'stranger', 'the-bear',
    'battle-of-algiers', 'papicha', 'berlin', 'frieren', 'nedjma',
    'memory-in-the-flesh', 'raoui', 'diwan-2', 'algiers-casbah', 'hamma-garden',
))
OPERATIONS_FILES = (
    'deploy/Caddyfile', 'deploy/trace.service', 'deploy/trace.env.example',
    'deploy/trace-backup.service', 'deploy/trace-backup.timer',
    'scripts/backup_database.py', 'scripts/manage_beta.py',
    'scripts/public_catalog_transfer.py', 'scripts/package_server.py',
    'scripts/bootstrap_server.py',
    'docs/deployment.md', 'docs/backup-restore.md', 'docs/friends-beta.md',
    'docs/native-apps.md',
)
SERVER_FILES = tuple(sorted(BACKEND_FILES + PUBLIC_FILES + ASSET_FILES + OPERATIONS_FILES))
MANIFEST = 'SHA256SUMS.json'
MAX_FILE_BYTES = 30 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024


class PackageError(ValueError):
    pass


def regular_directory(path):
    path = Path(os.path.abspath(path))
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise PackageError('Package paths must not contain symlinks.')
    return path


def read_input(root, name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in ('.', '..') for part in relative.parts) or not re.fullmatch(r'[A-Za-z0-9_./-]+', name):
        raise PackageError('Unsafe package filename.')
    path = root.joinpath(*relative.parts)
    regular_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise PackageError('Package inputs must be bounded regular files.')
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise PackageError('A package input exceeds the size limit.')
    return data


def _tar_file(archive, name, payload):
    info = tarfile.TarInfo('trace/' + name)
    info.size = len(payload)
    info.mode = 0o644
    info.mtime = 0
    archive.addfile(info, io.BytesIO(payload))


def package_server(root, output):
    root = regular_directory(root)
    output = Path(os.path.abspath(output))
    regular_directory(output.parent)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz', output.name):
        raise PackageError('Use a plain .tar.gz output filename.')
    # Validate every allowlisted input before creating an output. No directory
    # walk, Git operation, dependency directory, or private-data read is needed.
    payloads, total = {}, 0
    for name in SERVER_FILES:
        payloads[name] = read_input(root, name)
        total += len(payloads[name])
        if total > MAX_TOTAL_BYTES:
            raise PackageError('The package exceeds the total size limit.')
    manifest = {'format': 1, 'files': [
        {'path': name, 'bytes': len(payloads[name]), 'sha256': sha256(payloads[name]).hexdigest()}
        for name in SERVER_FILES
    ]}
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as handle:
        with gzip.GzipFile(filename='', mode='wb', fileobj=handle, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.USTAR_FORMAT) as archive:
                for name, payload in payloads.items():
                    _tar_file(archive, name, payload)
                _tar_file(archive, MANIFEST, json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
        handle.flush()
        os.fsync(handle.fileno())
    return {'archive': str(output), 'files': len(SERVER_FILES),
            'sha256': sha256(output.read_bytes()).hexdigest()}


def verify_package_data(data):
    if not isinstance(data, bytes) or len(data) > MAX_TOTAL_BYTES:
        raise PackageError('The archive exceeds the size limit.')
    archive_digest = sha256(data).hexdigest()
    seen, total, manifest = {}, 0, None
    with tarfile.open(fileobj=io.BytesIO(data), mode='r|gz') as archive:
        for member in archive:
            name = member.name.removeprefix('trace/')
            if member.name != 'trace/' + name or name not in (*SERVER_FILES, MANIFEST) or name in seen:
                raise PackageError('The archive contains an unexpected or duplicate path.')
            if not member.isfile() or not 0 <= member.size <= MAX_FILE_BYTES:
                raise PackageError('The archive contains an unsafe entry.')
            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise PackageError('The archive exceeds the size limit.')
            handle = archive.extractfile(member)
            data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) != member.size:
                raise PackageError('The archive contains an incomplete entry.')
            seen[name] = {'path': name, 'bytes': len(data), 'sha256': sha256(data).hexdigest()}
            if name == MANIFEST:
                manifest = json.loads(data)
    expected = [seen.get(name) for name in SERVER_FILES]
    if set(seen) != set(SERVER_FILES) | {MANIFEST} or manifest != {'format': 1, 'files': expected}:
        raise PackageError('The archive manifest does not match its contents.')
    return {'files': len(SERVER_FILES), 'sha256': archive_digest}


def verify_package(path):
    path = Path(os.path.abspath(path))
    regular_directory(path.parent)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TOTAL_BYTES:
        raise PackageError('The archive must be a bounded regular file.')
    return {'archive': str(path), **verify_package_data(path.read_bytes())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output', type=Path)
    group.add_argument('--verify', type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_package(args.verify) if args.verify else package_server(args.root, args.output)
    except (PackageError, OSError, tarfile.TarError, ValueError):
        parser.exit(1, 'Package operation failed. Check the reviewed inputs, output path, and archive integrity.\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
