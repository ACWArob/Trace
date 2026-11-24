from pathlib import Path
import importlib.util
import os
import signal
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('trace_backup', Path(__file__).resolve().parents[1] / 'scripts' / 'backup_database.py')
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'live.sqlite3'
        self.writer = sqlite3.connect(self.source)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT)')
        self.writer.execute('INSERT INTO example(value) VALUES(?)', ('private fixture',))
        self.writer.commit()

    def test_live_wal_snapshot_and_restore_preserve_committed_data_and_private_modes(self):
        self.assertTrue(Path(str(self.source) + '-wal').exists())
        result = backup.create_backup(self.source, self.root / 'backups')
        snapshot = Path(result['backup'])
        self.assertFalse(result['encrypted'])
        self.assertFalse(result['offsite'])
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snapshot.parent.stat().st_mode), 0o700)
        self.writer.execute('INSERT INTO example(value) VALUES(?)', ('later fixture',))
        self.writer.commit()
        restored = self.root / 'restored.sqlite3'
        self.assertEqual(backup.restore_backup(snapshot, restored)['integrity'], 'ok')
        with sqlite3.connect(restored) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM example').fetchone()[0], 1)
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o600)
        self.assertFalse(list(snapshot.parent.glob('*.partial')))

    def test_restore_rejects_existing_database_and_symlink_without_changing_them(self):
        self.assertRaises(backup.BackupError, backup.restore_backup, self.source, self.source)
        link = self.root / 'linked.sqlite3'
        link.symlink_to(self.source)
        self.assertRaises(backup.BackupError, backup.restore_backup, self.source, link)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.writer.execute('SELECT count(*) FROM example').fetchone()[0], 1)

    def test_corrupt_database_is_never_published(self):
        corrupt = self.root / 'corrupt.sqlite3'
        corrupt.write_bytes(b'not a database')
        destination = self.root / 'restore.sqlite3'
        self.assertRaises(backup.BackupError, backup.restore_backup, corrupt, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob('.trace-backup-*')))

    def test_missing_source_never_creates_empty_database(self):
        missing = self.root / 'missing.sqlite3'
        self.assertRaises(backup.BackupError, backup.create_backup, missing, self.root / 'backups')
        self.assertFalse(missing.exists())

    def test_shared_writable_output_directory_is_rejected(self):
        directory = self.root / 'shared'
        directory.mkdir()
        directory.chmod(0o777)
        self.assertRaises(backup.BackupError, backup.create_backup, self.source, directory)
        self.assertEqual(list(directory.iterdir()), [])

    def test_atomic_publication_refuses_collision(self):
        staged = self.root / 'stage'
        existing = self.root / 'existing'
        staged.write_bytes(b'new')
        existing.write_bytes(b'old')
        self.assertRaises(backup.BackupError, backup._publish, staged, existing)
        self.assertEqual(existing.read_bytes(), b'old')

    def test_offsite_requires_encryption_and_job_requires_both_settings(self):
        for args in [{'remote':'user@backup.example:/backups'}, {'require_offsite':True},
                     {'require_offsite':True, 'recipient':'age1' + 'a' * 50}]:
            self.assertRaises(backup.BackupError, backup.create_backup, self.source, self.root / 'backups', **args)
        self.assertFalse((self.root / 'backups').exists())

    def test_remote_targets_reject_shell_options_paths_and_line_breaks(self):
        for target in ['-oProxyCommand=x:/path','user@host:/../path','user@host:relative',
                       'user@host:/path\nquit','user@host:/path;evil','user@host:/a/./b']:
            self.assertRaises(backup.BackupError, backup._remote_destination, target)
        self.assertEqual(backup._remote_destination('trace@backup.example:/trace/backups'), ('trace@backup.example','/trace/backups'))

    def test_encrypted_backup_uploads_only_ciphertext_and_leaves_no_plaintext(self):
        def encrypt(_arguments, _source, output, _timeout):
            output.write_bytes(b'age-encrypted-fixture')
        with patch.object(backup, '_executable', side_effect=lambda x:'/usr/bin/' + x), \
             patch.object(backup, '_age_to_file', side_effect=encrypt), \
             patch.object(backup, 'upload_backup') as upload:
            result = backup.create_backup(self.source, self.root / 'backups', recipient='age1' + 'a' * 50,
                                          remote='trace@backup.example:/backups', require_offsite=True)
        self.assertTrue(result['encrypted'])
        self.assertTrue(result['offsite'])
        self.assertEqual(Path(result['backup']).suffix, '.age')
        self.assertEqual(Path(result['backup']).read_bytes(), b'age-encrypted-fixture')
        self.assertEqual(list(Path(result['backup']).parent.iterdir()), [Path(result['backup'])])
        upload.assert_called_once_with(Path(result['backup']), 'trace@backup.example:/backups', 120)

    def test_encryption_failure_leaves_no_snapshot_or_partial_file(self):
        with patch.object(backup, '_executable', return_value='/usr/bin/age'), \
             patch.object(backup, '_age_to_file', side_effect=backup.BackupError('encryption failed')):
            self.assertRaises(backup.BackupError, backup.create_backup, self.source, self.root / 'backups', recipient='age1' + 'a' * 50)
        self.assertEqual(list((self.root / 'backups').iterdir()), [])

    def test_backup_command_removes_plaintext_partial_on_sigterm(self):
        directory = self.root / 'backups'
        snapshot = backup._snapshot

        def interrupt_after_snapshot(source, destination, timeout):
            snapshot(source, destination, timeout)
            self.assertTrue(destination.is_file())
            signal.raise_signal(signal.SIGTERM)

        # Keep the test runner alive if the command fails to install its handler.
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with patch.object(backup, '_snapshot', side_effect=interrupt_after_snapshot):
            with self.assertRaises(SystemExit):
                backup.main(['backup', '--source', str(self.source), '--output-dir', str(directory)])
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)

    def test_sftp_uses_batch_and_verified_host_keys_with_remote_atomic_promotion(self):
        path = self.root / 'a backup.sqlite3.age'
        path.write_bytes(b'encrypted')
        with patch.object(backup, '_executable', return_value='/usr/bin/sftp'), \
             patch.object(backup.subprocess, 'run') as run:
            run.return_value.returncode = 0
            backup.upload_backup(path, 'trace@backup.example:/backups')
        args, kwargs = run.call_args
        self.assertIn('-oBatchMode=yes', args[0])
        self.assertIn('-oStrictHostKeyChecking=yes', args[0])
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs['input'].decode().splitlines()[-1], 'rename "/backups/a backup.sqlite3.age.partial" "/backups/a backup.sqlite3.age"')

    def test_upload_failure_keeps_local_encrypted_backup(self):
        def encrypt(_arguments, _source, output, _timeout):
            output.write_bytes(b'encrypted')
        with patch.object(backup, '_executable', side_effect=lambda x:'/usr/bin/' + x), \
             patch.object(backup, '_age_to_file', side_effect=encrypt), \
             patch.object(backup, 'upload_backup', side_effect=backup.BackupError('upload failed')):
            self.assertRaises(backup.BackupError, backup.create_backup, self.source, self.root / 'backups',
                              recipient='age1' + 'a' * 50, remote='trace@backup.example:/backups')
        self.assertEqual(len(list((self.root / 'backups').glob('*.age'))), 1)
        self.assertFalse(list((self.root / 'backups').glob('*.sqlite3')))

    def test_private_age_identity_permissions_are_enforced_before_decryption(self):
        encrypted = self.root / 'snapshot.sqlite3.age'
        encrypted.write_bytes(b'ciphertext')
        key = self.root / 'identity.txt'
        key.write_text('fixture')
        key.chmod(0o644)
        with patch.object(backup, '_age_to_file') as decrypt:
            self.assertRaises(backup.BackupError, backup.restore_backup, encrypted, self.root / 'restored', identity=str(key))
            decrypt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
