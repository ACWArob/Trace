"""Create an expiring, single-use invite in a private file, never in logs."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import Database
from social import SocialStore


def write_invite(store, output, *, hours=168):
    if type(hours) is not int or not 1 <= hours <= 720:
        raise ValueError('Invite validity must be 1–720 hours.')
    path = Path(output)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(store.create_invite(expires_in=hours * 3600) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--hours', type=int, default=168)
    args = parser.parse_args()
    try:
        write_invite(SocialStore(Database.from_env()), args.output, hours=args.hours)
    except (ValueError, OSError) as error:
        parser.exit(1, 'Invite creation failed. Use a new writable private output path and valid duration.\n')
    print('Invite saved to the requested private file. It expires and can be used once.')


if __name__ == '__main__':
    main()
