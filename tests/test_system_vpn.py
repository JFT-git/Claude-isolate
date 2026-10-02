from pathlib import Path
import socket
import sys
import os
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import environment
import network_guard
import network_transport


def answer(ip):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))


class SystemVpnTests(unittest.TestCase):
    def test_system_mode_needs_no_local_proxy_port(self):
        cfg = environment.load_config(environment.ROOT / 'environment.example.json')
        self.assertNotIn('proxy_port', cfg)
        cmd = environment.command(cfg, check=False)
        net = cmd[cmd.index('-netdev') + 1]
        self.assertIn('--mode system', net)
        self.assertNotIn('--port', net)
        self.assertIn('restrict=on', net)

    def test_proxy_mode_remains_optional(self):
        cfg = environment.load_config(environment.ROOT / 'environment.proxy.example.json')
        net = environment.command(cfg, check=False)
        net = net[net.index('-netdev') + 1]
        self.assertIn('--mode proxy', net)
        self.assertIn('--port 7890', net)

    def test_dns_rebinding_to_local_ip_blocked_before_connect(self):
        for ip in ('127.0.0.1', '192.168.1.1', '10.0.2.2', '169.254.169.254'):
            with patch.object(socket, 'getaddrinfo', return_value=[answer(ip)]), \
                 patch.object(socket, 'socket') as create:
                with self.assertRaises(OSError):
                    network_transport.open_public('claude.ai', 443)
                create.assert_not_called()

    def test_connect_uses_validated_public_ip_without_second_lookup(self):
        sock = Mock()
        with patch.object(socket, 'getaddrinfo', return_value=[answer('8.8.8.8')]) as resolve, \
             patch.object(socket, 'socket', return_value=sock):
            self.assertIs(network_transport.open_public('claude.ai', 443), sock)
            resolve.assert_called_once_with('claude.ai', 443, socket.AF_INET, socket.SOCK_STREAM)
            sock.connect.assert_called_once_with(('8.8.8.8', 443))

    def test_country_probe_uses_system_transport_without_proxy(self):
        conn = Mock()
        response = conn.getresponse.return_value
        response.status = 200
        response.read.return_value = b'ip=8.8.8.8\nloc=US\ncolo=EWR\n'
        tls = Mock()
        with patch.dict('os.environ', {}, clear=True), \
             patch.object(network_guard.http.client, 'HTTPConnection', return_value=conn), \
             patch.object(network_guard, 'open_public', return_value=Mock()) as outbound, \
             patch.object(network_guard.ssl, 'create_default_context', return_value=tls):
            self.assertTrue(network_guard.probe(mode='system')['allowed'])
            outbound.assert_called_once_with('www.cloudflare.com', 443, timeout=3)
            conn.set_tunnel.assert_not_called()
            conn.connect.assert_not_called()
            tls.wrap_socket.assert_called_once()
            self.assertEqual(tls.minimum_version, network_guard.ssl.TLSVersion.TLSv1_2)

    def test_system_relay_tunnels_guest_payload_without_external_proxy(self):
        # Substitute a local server only for the transport under test. The
        # production transport's public-address checks are tested separately.
        with tempfile.TemporaryDirectory() as directory, socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            server.settimeout(4)
            received = []
            finished = threading.Event()
            def serve():
                with server.accept()[0] as client:
                    client.settimeout(4)
                    received.append(client.recv(4096))
                    client.sendall(b'reply')
                    finished.set()
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            lease = Path(directory) / 'lease.json'
            network_guard.publish(lease, network_guard.classify({'ip': '8.8.8.8', 'country': 'US'}))
            code = ('import sys,socket;sys.path.insert(0,sys.argv[1]);import environment;'
                    'environment.open_public=lambda host,port,timeout: '
                    'socket.create_connection(("127.0.0.1",int(sys.argv[2])),timeout);'
                    'environment.relay(mode="system")')
            env = dict(os.environ, CLAUDE_NETWORK_LEASE=str(lease))
            request = b'CONNECT claude.ai:443 HTTP/1.1\r\n\r\n' + b'guest-tls-bytes'
            result = subprocess.run([sys.executable, '-c', code, str(environment.ROOT),
                                     str(server.getsockname()[1])], input=request,
                                    capture_output=True, env=env, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(finished.wait(1))
            self.assertEqual(received, [b'guest-tls-bytes'])
            self.assertTrue(result.stdout.startswith(b'HTTP/1.1 200'))
            self.assertTrue(result.stdout.endswith(b'reply'))
            worker.join(timeout=1)

    def test_probe_rate_limit_reports_retry_without_accepting_location(self):
        conn = Mock()
        response = conn.getresponse.return_value
        response.status = 429
        response.getheader.return_value = '120'
        with patch.dict('os.environ', {}, clear=True), \
             patch.object(network_guard.http.client, 'HTTPConnection', return_value=conn), \
             patch.object(network_guard, 'open_public', return_value=Mock()), \
             patch.object(network_guard.ssl, 'create_default_context', return_value=Mock()):
            with self.assertRaises(network_guard.ProbeUnavailable) as result:
                network_guard.probe(mode='system')
            self.assertEqual(result.exception.retry_after, 120)
            response.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
