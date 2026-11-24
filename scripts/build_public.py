"""Stage Trace's public web shell into public/ for Vercel's static output.

Copy only, no bundling.  The allowlist is server.PUBLIC_FILES (read from the
source with ``ast``, so the build image needs no Python packages) plus the
flat files in assets/ that server.py would serve.  Nothing else from the
repository root is published.  The output directory is recreated each run.

    python3 scripts/build_public.py [--out public]
"""
import argparse
import ast
import hashlib
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
ASSET_NAME = re.compile(r'[\w.-]+')  # Same rule as server.py's /assets/ route.


def public_files():
    tree = ast.parse((ROOT / 'server.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'PUBLIC_FILES' for t in node.targets):
            # frozenset(( ... )) -> the literal tuple of paths.
            return sorted(ast.literal_eval(node.value.args[0]))
    raise SystemExit('server.py has no PUBLIC_FILES allowlist.')


def staged_paths():
    paths = [path.lstrip('/') for path in public_files() if path != '/']
    assets = ROOT / 'assets'
    paths += sorted('assets/' + item.name for item in assets.iterdir()
                    if item.is_file() and not item.is_symlink() and ASSET_NAME.fullmatch(item.name))
    return paths


def build(out):
    out = Path(out).resolve()
    if out.name != 'public' or out == ROOT:
        raise SystemExit('The output directory must be named public.')
    if out.exists():
        shutil.rmtree(out)
    for path in staged_paths():
        source = ROOT / path
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f'Missing public file: {path}')
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    stamp_service_worker(out)
    return out


def stamp_service_worker(out):
    """Tie the offline cache name to the staged shell, so every deploy that
    changes a public file makes installed apps pick up the new code."""
    digest = hashlib.sha256()
    for path in sorted(p for p in out.rglob('*') if p.is_file() and p.name != 'sw.js'):
        digest.update(path.relative_to(out).as_posix().encode() + b'\0' + path.read_bytes())
    worker = out / 'sw.js'
    source = worker.read_text(encoding='utf-8')
    stamped, count = re.subn(r'(const CACHE_NAME = `\$\{CACHE_PREFIX\}v\d+)`;',
                             lambda m: f'{m.group(1)}-{digest.hexdigest()[:12]}`;', source, count=1)
    if count != 1:
        raise SystemExit('sw.js has no CACHE_NAME line to stamp.')
    worker.write_text(stamped, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', default=str(ROOT / 'public'))
    target = build(parser.parse_args().out)
    print(f'Staged {sum(1 for p in target.rglob("*") if p.is_file())} public files in {target.name}/.')
