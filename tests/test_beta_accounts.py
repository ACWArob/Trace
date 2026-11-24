"""Friends-beta account tests use isolated synthetic databases only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from social import SocialStore, SocialError
from library_store import LibraryStore
from tests.pg_support import PostgresTestCase

PASSWORD = 'synthetic-test-passphrase'
NEW_PASSWORD = 'synthetic-replacement-passphrase'


class BetaAccountTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.store = SocialStore(self.database)

    def register(self, name='alice', **kwargs):
        return self.store.register({'username': name, 'password': PASSWORD, **kwargs})

    def test_invites_are_hashed_one_use_expiring_and_optional_for_local_development(self):
        invite = self.store.create_invite()
        with self.store.db() as db:
            row = db.execute('SELECT * FROM beta_invites').fetchone()
            self.assertNotEqual(invite, row['invite_hash'])
            self.assertNotIn(invite, str(tuple(row)))
        for bad in ['', None, [], 'x' * 43]:
            with self.assertRaisesRegex(SocialError, 'invitation'):
                self.store.register({'username': 'alice', 'password': PASSWORD, 'inviteCode': bad}, invite_required=True)
        account, _ = self.store.register({'username': 'alice', 'password': PASSWORD, 'inviteCode': invite}, invite_required=True)
        self.assertEqual(account['user']['username'], 'alice')
        with self.assertRaisesRegex(SocialError, 'invitation'):
            self.store.register({'username': 'bob', 'password': PASSWORD, 'inviteCode': invite}, invite_required=True)
        expired = self.store.create_invite(expires_in=60)
        with patch('social.time.time', return_value=10**12):
            with self.assertRaisesRegex(SocialError, 'invitation'):
                self.store.register({'username': 'carol', 'password': PASSWORD, 'inviteCode': expired}, invite_required=True)
        self.assertEqual(self.register('local')[0]['user']['username'], 'local')
        for expires_in in [True, 0, -1, float('nan'), float('inf'), '60', 31 * 86400]:
            with self.assertRaises(SocialError):
                self.store.create_invite(expires_in)

    def test_failed_registration_does_not_consume_invite(self):
        self.register()
        invite = self.store.create_invite()
        with self.assertRaises(SocialError):
            self.store.register({'username': 'alice', 'password': PASSWORD, 'inviteCode': invite}, invite_required=True)
        result, _ = self.store.register({'username': 'bob', 'password': PASSWORD, 'inviteCode': invite}, invite_required=True)
        self.assertEqual(result['user']['username'], 'bob')

    def test_concurrent_invite_registration_has_one_winner(self):
        invite = self.store.create_invite()
        barrier = threading.Barrier(2)
        def attempt(name):
            barrier.wait(timeout=10)
            try:
                self.store.register({'username': name, 'password': PASSWORD, 'inviteCode': invite}, invite_required=True)
                return True
            except SocialError as exc:
                self.assertEqual(exc.status, 403)
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, ['alice', 'bob'])), 1)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 1)
            claimed = db.execute('SELECT claimed_by FROM beta_invites').fetchone()[0]
            self.assertEqual(claimed, db.execute('SELECT id FROM users').fetchone()[0])

    def test_username_is_case_insensitive_for_signup_and_recovery(self):
        with self.assertRaises(SocialError) as caught:
            self.register('ALICE')
            self.register('alice')
        self.assertEqual(caught.exception.status, 409)
        # A legacy mixed-case row (inserted directly) still conflicts and still recovers.
        account, _ = self.register('carol')
        with self.store.db() as db:
            db.execute('UPDATE users SET username=? WHERE id=?', ('Carol_Mixed', account['user']['id']))
        with self.assertRaises(SocialError) as caught:
            self.register('carol_mixed')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.profile('CAROL_MIXED')['id'], account['user']['id'])
        result, _ = self.store.recover({'username': 'CAROL_MIXED', 'recoveryCode': account['recoveryCodes'][0], 'newPassword': NEW_PASSWORD})
        self.assertEqual(result['user']['id'], account['user']['id'])

    def test_recovery_rotates_codes_and_revokes_every_old_session(self):
        account, token = self.register()
        _, second = self.store.login({'username': 'alice', 'password': PASSWORD})
        codes = account['recoveryCodes']
        self.assertEqual(len(set(codes)), 8)
        with self.store.db() as db:
            saved = [row[0] for row in db.execute('SELECT code_hash FROM account_recovery_codes')]
        self.assertEqual(len(saved), 8)
        self.assertTrue(all(code not in saved for code in codes))
        result, fresh = self.store.recover({'username': 'ALICE', 'recoveryCode': codes[0].lower().replace('-', ''), 'newPassword': NEW_PASSWORD})
        self.assertEqual(result['user']['id'], account['user']['id'])
        self.assertNotEqual(result['recoveryCodes'], codes)
        self.assertIsNone(self.store.session(token)['user'])
        self.assertIsNone(self.store.session(second)['user'])
        self.assertEqual(self.store.session(fresh)['user']['username'], 'alice')
        with self.assertRaises(SocialError):
            self.store.login({'username': 'alice', 'password': PASSWORD})
        self.assertEqual(self.store.login({'username': 'alice', 'password': NEW_PASSWORD})[0]['user']['username'], 'alice')
        for code in codes:
            with self.assertRaisesRegex(SocialError, 'Username or recovery code is incorrect'):
                self.store.recover({'username': 'alice', 'recoveryCode': code, 'newPassword': PASSWORD})

    def test_bad_recovery_inputs_are_generic_and_do_not_change_account(self):
        account, token = self.register()
        for name, code in [('missing', account['recoveryCodes'][0]), ('alice', 'F' * 24), ('alice', {}), ('alice', 'x' * 100), (None, account['recoveryCodes'][0])]:
            with self.assertRaisesRegex(SocialError, 'Username or recovery code is incorrect') as caught:
                self.store.recover({'username': name, 'recoveryCode': code, 'newPassword': NEW_PASSWORD})
            self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.store.session(token)['user']['username'], 'alice')
        self.assertEqual(self.store.login({'username': 'alice', 'password': PASSWORD})[0]['user']['username'], 'alice')

    def test_recovery_code_rotation_requires_password_and_preserves_other_accounts(self):
        alice, _ = self.register()
        bob, _ = self.register('bob')
        with self.assertRaises(SocialError):
            self.store.issue_recovery_codes(alice['user']['id'], 'incorrect-password')
        new = self.store.issue_recovery_codes(alice['user']['id'], PASSWORD)['recoveryCodes']
        self.assertNotEqual(new, alice['recoveryCodes'])
        with self.assertRaises(SocialError):
            self.store.recover({'username': 'alice', 'recoveryCode': alice['recoveryCodes'][0], 'newPassword': NEW_PASSWORD})
        self.assertEqual(self.store.recover({'username': 'bob', 'recoveryCode': bob['recoveryCodes'][0], 'newPassword': NEW_PASSWORD})[0]['user']['id'], bob['user']['id'])

    def test_account_deletion_removes_only_owners_rows_and_foreign_dependents(self):
        alice, old_token = self.register()
        bob, bob_token = self.register('bob')
        a, b = alice['user']['id'], bob['user']['id']
        library = LibraryStore(self.database)
        entry = {'id': 'one', 'type': 'book', 'title': 'Synthetic title', 'status': 'want', 'rating': 0, 'date': ''}
        library.sync(a, [{'id': 'one', 'entry': entry, 'baseRevision': 0}])
        library.sync(b, [{'id': 'one', 'entry': entry, 'baseRevision': 0}])
        item = {'type': 'book', 'title': 'Synthetic title'}
        own_review = self.store.publish(a, {'item': item, 'text': 'Owner review'})['id']
        other_review = self.store.publish(b, {'item': item, 'text': 'Other review'})['id']
        self.store.follow(a, 'bob', True)
        self.store.follow(b, 'alice', True)
        self.store.comment(a, other_review, 'Owner comment')
        self.store.comment(b, own_review, 'Dependent comment')
        self.store.comment(b, other_review, 'Retained comment')
        self.store.like(a, other_review, True)
        self.store.like(b, own_review, True)
        with self.store.db() as db:
            for owner, photo in [(a, 'a' * 64), (b, 'b' * 64)]:
                db.execute('INSERT INTO photos VALUES(?,?,?,?,?,?,NULL)', (owner, photo, f'photos/{owner}/{photo}', 'image/jpeg', 10, 1.0))
        with self.assertRaises(SocialError):
            self.store.delete_account(a, PASSWORD, 'wrong')
        with self.assertRaises(SocialError):
            self.store.delete_account(a, 'incorrect-password', 'alice')
        self.assertEqual(self.store.delete_account(a, PASSWORD, 'alice'), {'deleted': True})
        self.assertIsNone(self.store.session(old_token)['user'])
        self.assertEqual(self.store.session(bob_token)['user']['id'], b)
        self.assertEqual(len(library.pull(b)['changes']), 1)
        self.assertEqual(len(self.store.feed()['items']), 1)
        self.assertEqual(self.store.review(other_review)['commentCount'], 1)
        with self.store.db() as db:
            for table in ['users', 'sessions', 'account_recovery_codes', 'private_library_entries', 'private_library_state', 'reviews', 'likes', 'comments']:
                column = 'id' if table == 'users' else 'user_id'
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE {column}=?', (a,)).fetchone()[0], 0)
            photos = {row[0]: row[1] for row in db.execute('SELECT user_id,deleted_at FROM photos')}
            self.assertIsNotNone(photos[a])
            self.assertIsNone(photos[b])
            self.assertEqual(db.execute('SELECT count(*) FROM private_library_entries WHERE user_id=?', (b,)).fetchone()[0], 1)

    def test_account_deletion_before_library_initialization_and_used_invite_remains_used(self):
        invite = self.store.create_invite()
        account, _ = self.store.register({'username': 'alice', 'password': PASSWORD, 'inviteCode': invite}, invite_required=True)
        self.store.delete_account(account['user']['id'], PASSWORD)
        with self.assertRaisesRegex(SocialError, 'invitation'):
            self.store.register({'username': 'bob', 'password': PASSWORD, 'inviteCode': invite}, invite_required=True)

    def test_surrogate_passwords_are_rejected_as_validation_errors(self):
        with self.assertRaises(SocialError):
            self.store.register({'username': 'alice', 'password': 'invalid\ud800password'})
        account, _ = self.register()
        with self.assertRaises(SocialError):
            self.store.issue_recovery_codes(account['user']['id'], 'invalid\ud800password')
        with self.assertRaisesRegex(SocialError, 'Username or password is incorrect'):
            self.store.login({'username': 'alice', 'password': 'invalid\ud800password'})


if __name__ == '__main__':
    unittest.main()
