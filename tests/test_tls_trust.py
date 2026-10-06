"""Fresh Windows root stores must work without weakening TLS verification."""
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tls_trust


class TlsTrustTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows bundles additional trust roots')
    def test_windows_with_empty_os_roots_loads_verified_bundle(self):
        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(empty.cert_store_stats()['x509_ca'], 0)
        with patch.object(ssl, 'create_default_context', return_value=empty):
            context = tls_trust.client_context()
        self.assertGreater(context.cert_store_stats()['x509_ca'], 100)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_system_context_keeps_hostname_and_certificate_verification(self):
        context = tls_trust.client_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
