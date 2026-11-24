import importlib.util
import io
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('package_server', Path(__file__).resolve().parents[1] / 'scripts/package_server.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class ServerPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.root = Path(self.temporary.name) / 'source'
        self.root.mkdir()
        for name in package.SERVER_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('reviewed public source: ' + name).encode())
        self.output = Path(self.temporary.name) / 'trace-beta.tar.gz'

    def tearDown(self):
        self.temporary.cleanup()

    def test_only_reviewed_files_are_packaged_and_hashes_verify(self):
        for name in ['.trace/trace.sqlite3', '.env', 'native/android/signing.keystore',
                     'assets/screenshots/private.png', 'assets/unreviewed.jpg',
                     'node_modules/unexpected.js', 'private-export.json']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private marker')
        result = package.package_server(self.root, self.output)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(result, package.verify_package(self.output))
        with tarfile.open(self.output) as archive:
            self.assertEqual(set(archive.getnames()), {'trace/' + name for name in (*package.SERVER_FILES, package.MANIFEST)})
            for member in archive:
                self.assertTrue(member.isfile())
                self.assertNotIn(b'private marker', archive.extractfile(member).read())

    def test_existing_output_is_not_overwritten(self):
        self.output.write_text('keep me')
        with self.assertRaises(FileExistsError):
            package.package_server(self.root, self.output)
        self.assertEqual(self.output.read_text(), 'keep me')

    def test_input_and_parent_symlinks_are_rejected(self):
        path = self.root / 'server.py'
        path.unlink()
        outside = Path(self.temporary.name) / 'private.py'
        outside.write_text('private marker')
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            package.package_server(self.root, self.output)
        self.assertFalse(self.output.exists())
        alias = Path(self.temporary.name) / 'source-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(package.PackageError):
            package.package_server(alias, self.output)

    def test_output_directory_symlink_and_unsafe_name_are_rejected(self):
        alias = Path(self.temporary.name) / 'output-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(package.PackageError):
            package.package_server(self.root, alias / 'trace.tar.gz')
        with self.assertRaises(package.PackageError):
            package.package_server(self.root, self.output.with_name('trace;unsafe.tar.gz'))

    def test_tampered_content_and_archive_links_are_rejected(self):
        package.package_server(self.root, self.output)
        bad = self.output.with_name('tampered.tar.gz')
        with tarfile.open(self.output) as source, tarfile.open(bad, 'w:gz') as destination:
            for member in source:
                payload = source.extractfile(member).read()
                if member.name == 'trace/server.py':
                    payload = b'changed'
                    member.size = len(payload)
                destination.addfile(member, io.BytesIO(payload))
        with self.assertRaises(package.PackageError):
            package.verify_package(bad)
        linked = self.output.with_name('linked.tar.gz')
        with tarfile.open(linked, 'w:gz') as archive:
            member = tarfile.TarInfo('trace/server.py')
            member.type = tarfile.SYMTYPE
            member.linkname = '/private'
            archive.addfile(member)
        with self.assertRaises(package.PackageError):
            package.verify_package(linked)


if __name__ == '__main__':
    unittest.main()
