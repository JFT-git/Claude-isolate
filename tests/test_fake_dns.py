"""VPN synthetic DNS compatibility must not turn into a local-network bypass."""
import json
from pathlib import Path
import socket
import ssl
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import environment
import network_transport as transport


def answer(ip, port=443):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port))


class FakeDnsTests(unittest.TestCase):
    def test_synthetic_dns_replaced_without_connecting_to_fake_address(self):
        for synthetic in ('198.18.0.1', '198.19.255.254'):
            with self.subTest(ip=synthetic), \
                    patch.object(socket, 'getaddrinfo', return_value=[answer(synthetic)]) as dns, \
                    patch.object(transport, '_doh_addresses', return_value=[answer('8.8.8.8')]) as doh, \
                    patch.object(socket, 'socket', return_value=Mock()) as create:
                transport.open_public('claude.ai', 443)
                dns.assert_called_once()
                doh.assert_called_once()
                create.return_value.connect.assert_called_once_with(('8.8.8.8', 443))

    def test_fake_literal_local_names_mixed_and_other_private_answers_fail_closed(self):
        cases = [('198.18.0.1', ['198.18.0.1']), ('router.local', ['198.18.0.1']),
                 ('example.com', ['198.18.0.1', '8.8.8.8']),
                 ('example.com', ['198.18.0.1', '192.168.1.1']),
                 ('example.com', ['127.0.0.1']), ('example.com', ['100.64.0.1']),
                 ('example.com', ['169.254.169.254']), ('example.com', ['192.0.2.1'])]
        for host, ips in cases:
            with self.subTest(host=host, ips=ips), \
                    patch.object(socket, 'getaddrinfo', return_value=[answer(ip) for ip in ips]), \
                    patch.object(transport, '_doh_addresses') as doh, \
                    patch.object(socket, 'socket') as create:
                with self.assertRaises(OSError) as error:
                    transport.open_public(host, 443)
                self.assertIn(host, str(error.exception))
                self.assertIn(ips[0], str(error.exception))
                doh.assert_not_called()
                create.assert_not_called()

    def resolver(self, records, status=0, http_status=200):
        conn = Mock()
        response = conn.getresponse.return_value
        response.status = http_status
        response.read.return_value = json.dumps({'Status': status, 'Answer': records}).encode()
        return conn

    def test_doh_bootstrap_uses_public_literals_verified_tls_and_requested_port(self):
        conn = self.resolver([{'type': 5, 'data': 'alias.example.com'},
                              {'type': 1, 'data': '8.8.8.8'}])
        tls = Mock()
        raw = Mock()
        with patch.object(transport.http.client, 'HTTPConnection', return_value=conn), \
                patch.object(transport, '_connect', return_value=raw) as connect, \
                patch.object(ssl, 'create_default_context', return_value=tls), \
                patch.object(socket, 'getaddrinfo') as dns:
            addresses = transport._doh_addresses('claude.ai', 80, time.monotonic() + 3)
            self.assertEqual(addresses, [answer('8.8.8.8', 80)])
            self.assertEqual([a[4] for a in connect.call_args.args[0]],
                             [('1.1.1.1', 443), ('1.0.0.1', 443)])
            tls.wrap_socket.assert_called_once_with(raw, server_hostname=transport.DOH_HOST)
            self.assertEqual(tls.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertIn('name=claude.ai&type=A', conn.request.call_args.args[1])
            dns.assert_not_called()
            conn.close.assert_called_once()

    def test_doh_untrusted_or_invalid_replies_fail_closed(self):
        records = [[{'type': 1, 'data': ip}] for ip in
                   ('127.0.0.1', '198.18.0.1', '192.168.1.1', '224.0.0.1', '::1', '2606:4700::1111')]
        records += [[], [{'type': 5, 'data': 'alias.example.com'}],
                    [{'type': 1, 'data': '8.8.8.8'}, {'type': 1, 'data': '10.0.0.1'}],
                    [{'type': 1}], [None], [{'type': 1, 'data': 134744072}]]
        cases = [(r, 0, 200) for r in records] + [([], 3, 200), ([], 0, 302), ([], 0, 429)]
        for records, status, http_status in cases:
            conn = self.resolver(records, status, http_status)
            with self.subTest(records=records, status=status, http=http_status), \
                    patch.object(transport.http.client, 'HTTPConnection', return_value=conn), \
                    patch.object(transport, '_connect', return_value=Mock()), \
                    patch.object(ssl, 'create_default_context', return_value=Mock()):
                with self.assertRaises(OSError):
                    transport._doh_addresses('claude.ai', 443, time.monotonic() + 3)
                conn.close.assert_called_once()

    def test_certificate_error_closes_socket_without_returning_addresses(self):
        conn = Mock()
        raw = Mock()
        tls = Mock()
        tls.wrap_socket.side_effect = ssl.SSLCertVerificationError('invalid certificate')
        with patch.object(transport.http.client, 'HTTPConnection', return_value=conn), \
                patch.object(transport, '_connect', return_value=raw), \
                patch.object(ssl, 'create_default_context', return_value=tls):
            with self.assertRaises(OSError):
                transport._doh_addresses('claude.ai', 443, time.monotonic() + 3)
            raw.close.assert_called_once()
            conn.request.assert_not_called()
            conn.close.assert_called_once()

    def test_expired_dns_budget_never_opens_destination_socket(self):
        with patch.object(socket, 'getaddrinfo', return_value=[answer('8.8.8.8')]), \
                patch.object(socket, 'socket', return_value=Mock()) as create:
            with self.assertRaises(TimeoutError):
                transport.open_public('claude.ai', 443, timeout=0)
            create.return_value.connect.assert_not_called()
            create.return_value.close.assert_called_once()

    def test_startup_error_is_machine_readable_for_windows_gui(self):
        with patch.object(sys, 'argv', ['environment', 'start', '--config', 'missing.json']), \
                patch.object(environment, 'load_config', side_effect=OSError('DNS diagnostic')), \
                patch('builtins.print') as output:
            with self.assertRaises(SystemExit) as error:
                environment.main()
            self.assertEqual(error.exception.code, 1)
            messages = [json.loads(call.args[0]) for call in output.call_args_list
                        if isinstance(call.args[0], str) and call.args[0].startswith('{')]
            self.assertEqual(messages[-1], {'message': 'DNS diagnostic', 'error': True})

    def test_relay_resolution_failure_returns_http_error_without_guest_payload(self):
        header = b'CONNECT claude.ai:443 HTTP/1.1\r\n\r\n'
        stdout = Mock()
        stdout.fileno.return_value = sys.stdout.fileno()
        with patch.object(environment.os, 'read', side_effect=[bytes([b]) for b in header]) as read, \
                patch.object(environment.network_guard, 'permitted', return_value=True), \
                patch.object(environment, 'open_public', side_effect=OSError('DNS diagnostic')), \
                patch.object(sys, 'stdout', stdout), patch('builtins.print'), \
                patch('select.select', return_value=([sys.stdin.buffer], [], [])):
            environment.relay(mode='system')
            stdout.buffer.write.assert_called_once_with(
                b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            self.assertEqual(read.call_count, len(header))


if __name__ == '__main__':
    unittest.main()
