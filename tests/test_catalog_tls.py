import os
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server


class CatalogTLSTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_partial_python_trust_uses_an_existing_system_bundle(self):
        context = Mock()
        context.get_ca_certs.return_value = ['existing-root']
        with patch('server.ssl.create_default_context', return_value=context), \
             patch('server.ssl.get_default_verify_paths', return_value=SimpleNamespace(cafile=None)), \
             patch('server.Path.is_file', return_value=True):
            self.assertIs(server.create_tls_context(), context)
        context.load_verify_locations.assert_called_once_with('/etc/ssl/cert.pem')
        self.assertEqual(context.method_calls, [unittest.mock.call.load_verify_locations('/etc/ssl/cert.pem')])

    @patch.dict(os.environ, {'SSL_CERT_FILE': '/custom/trusted-ca.pem'}, clear=True)
    def test_explicit_ca_configuration_is_not_expanded(self):
        context = Mock()
        with patch('server.ssl.create_default_context', return_value=context):
            server.create_tls_context()
        context.load_verify_locations.assert_not_called()

    def test_real_context_keeps_certificate_and_hostname_verification(self):
        context = server.create_tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
