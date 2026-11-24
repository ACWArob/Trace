import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
from release_policy import RequestBudget, client_ip, signup_mode, library_byte_limit
from test_production import request
from scripts.manage_beta import write_invite


class ReleasePolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.social = server.SocialStore(self.directory / 'test.sqlite3')
        self.patches = [patch.object(server, 'SOCIAL_STORE', self.social),
                        patch.object(server, 'CATALOG_STORE', server.CatalogStore(self.social.path)),
                        patch.dict('os.environ', {'TRACE_ENV':'production', 'TRACE_PUBLIC_ORIGIN':'https://trace.example', 'TRACE_TRUST_PROXY':'1', 'TRACE_SIGNUP_MODE':'invite'})]
        for item in self.patches:
            item.start();self.addCleanup(item.stop)
        server.AUTH_ATTEMPTS.clear();server.API_BUDGET.buckets.clear()

    def call(self, path, payload=None, cookie='', csrf='', address='203.0.113.4'):
        return request(path, 'POST' if payload is not None else 'GET', payload,
            {'HTTP_HOST':'trace.example', 'HTTP_ORIGIN':'https://trace.example', 'HTTP_X_TRACE_CLIENT_IP':address,
             'HTTP_COOKIE':cookie, 'HTTP_X_TRACE_CSRF':csrf})

    def test_production_invite_recovery_and_authenticated_catalog_boundaries(self):
        self.assertEqual(signup_mode(), 'invite')
        health = self.call('/api/health')
        self.assertEqual(json.loads(health['body']), {'status':'ok'})
        self.assertEqual(request('/api/health')['status'], 403)
        self.assertEqual(self.call('/api/search?type=book&q=Nedjma')['status'], 401)
        config = json.loads(self.call('/api/auth/config')['body'])
        self.assertEqual(config, {'signupMode':'invite','recovery':'codes'})
        payload = {'username':'release_alice','password':'test-password-synthetic'}
        self.assertEqual(self.call('/api/auth/signup', payload)['status'], 403)
        payload['inviteCode'] = self.social.create_invite()
        response = self.call('/api/auth/signup', payload)
        self.assertEqual(response['status'], 201)
        headers = dict(response['headers']);self.assertIn('Secure', headers['Set-Cookie'])
        account = json.loads(response['body'])
        self.assertEqual(len(account['recoveryCodes']), 8)
        cookie = headers['Set-Cookie'].split(';',1)[0]
        with patch.object(server, 'search', return_value={'items':[]}) as provider:
            self.assertEqual(self.call('/api/search?type=book&q=Nedjma',cookie=cookie)['status'],200)
            provider.assert_called_once()
        self.assertEqual(self.call('/api/auth/recovery-codes', {'password':payload['password']}, cookie=cookie)['status'],403)
        rotated = self.call('/api/auth/recovery-codes', {'password':payload['password']}, cookie=cookie, csrf=account['csrf'])
        self.assertEqual(rotated['status'],200)
        recovery = self.call('/api/auth/recover', {'username':payload['username'],'recoveryCode':json.loads(rotated['body'])['recoveryCodes'][0], 'newPassword':'synthetic-replacement-password'})
        self.assertEqual(recovery['status'],200)
        self.assertIsNone(json.loads(self.call('/api/auth/session', cookie=cookie)['body'])['user'])
        new = json.loads(recovery['body']);new_cookie=dict(recovery['headers'])['Set-Cookie'].split(';',1)[0]
        deleted = self.call('/api/auth/delete', {'password':'synthetic-replacement-password','confirmation':payload['username']}, cookie=new_cookie, csrf=new['csrf'])
        self.assertEqual(deleted['status'],200)
        self.assertEqual(json.loads(deleted['body']),{'deleted':True})

    def test_proxy_identity_does_not_trust_user_supplied_forwarding(self):
        headers={'X-Trace-Client-IP':'203.0.113.9','X-Forwarded-For':'198.51.100.1'}
        self.assertEqual(client_ip('127.0.0.1',headers),'203.0.113.9')
        self.assertEqual(client_ip('198.51.100.5',headers),'198.51.100.5')
        self.assertEqual(client_ip('127.0.0.1',{'X-Trace-Client-IP':'a,b'}),'127.0.0.1')
        with patch.dict('os.environ',{'TRACE_TRUST_PROXY':'0'}):self.assertEqual(client_ip('127.0.0.1',headers),'127.0.0.1')
        for _ in range(15):self.call('/api/auth/login',{},address='203.0.113.8')
        self.assertEqual(self.call('/api/auth/login',{},address='203.0.113.8')['status'],429)
        self.assertEqual(self.call('/api/auth/login',{},address='203.0.113.9')['status'],401)

    def test_budgets_expire_and_bound_memory(self):
        now=[0];budget=RequestBudget(maximum_keys=2,clock=lambda:now[0])
        self.assertTrue(budget.allow('a',1));self.assertFalse(budget.allow('a',1))
        now[0]=61;self.assertTrue(budget.allow('a',1))
        budget.allow('b',1);budget.allow('c',1);self.assertEqual(len(budget.buckets),2)
        self.assertEqual(library_byte_limit(),100*1024*1024)
        with patch.dict('os.environ',{'TRACE_LIBRARY_BYTES':'0'}):
            with self.assertRaises(ValueError):library_byte_limit()

    def test_ipv6_rate_limit_keys_group_addresses_by_subnet(self):
        self.assertEqual(client_ip('2001:db8:1:2::a', {}), '2001:db8:1:2::/64')
        self.assertEqual(client_ip('2001:db8:1:2::b', {}), '2001:db8:1:2::/64')
        self.assertEqual(client_ip('2001:db8:1:3::a', {}), '2001:db8:1:3::/64')
        self.assertEqual(client_ip('::1', {'X-Trace-Client-IP': '2001:db8:1:2::b'}),
                         '2001:db8:1:2::/64')

    def test_ipv4_and_mapped_ipv6_use_the_same_rate_limit_key(self):
        self.assertEqual(client_ip('192.0.2.5', {}), '192.0.2.5')
        self.assertEqual(client_ip('::ffff:192.0.2.5', {}), '192.0.2.5')
        self.assertEqual(client_ip('127.0.0.1', {'X-Trace-Client-IP': '::ffff:192.0.2.5'}),
                         '192.0.2.5')

    def test_invite_cli_writes_owner_only_without_overwriting(self):
        output=self.directory/'invite.txt'
        write_invite(self.social,output)
        self.assertEqual(output.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):write_invite(self.social,output)
        account,_=self.social.register({'username':'invited','password':'synthetic-password','inviteCode':output.read_text().strip()},invite_required=True)
        self.assertEqual(account['user']['username'],'invited')


if __name__=='__main__':unittest.main()
