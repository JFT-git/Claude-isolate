import json
from pathlib import Path
import sys
import subprocess
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import environment


class IsolationTests(unittest.TestCase):
    def test_public_web_keeps_local_addresses_and_non_web_ports_blocked(self):
        for target in ('127.0.0.1:443', '10.0.2.2:443', '[::1]:443',
                       '169.254.169.254:443', 'localhost:443', 'router.local:443',
                       'example.com:22', 'user@example.com:443', 'example.com:443/path'):
            self.assertFalse(environment.allowed_request(
                f'CONNECT {target} HTTP/1.1\r\n\r\n'.encode(), 'public'))
        for host in ('www.google.com', 'example.com', 'www.mozilla.org'):
            self.assertTrue(environment.allowed_request(
                f'CONNECT {host}:443 HTTP/1.1\r\n\r\n'.encode(), 'public'))

    def test_public_web_still_requires_current_ip_permission(self):
        result = subprocess.run([sys.executable, str(environment.ROOT / 'environment.py'),
                                 'relay', '--mode', 'system', '--web-access', 'public'],
                                input=b'CONNECT www.google.com:443 HTTP/1.1\r\n\r\n',
                                capture_output=True, timeout=5)
        self.assertTrue(result.stdout.startswith(b'HTTP/1.1 503'))

    def test_proxy_blocks_host_and_unrelated_destinations(self):
        for host in ('127.0.0.1', '10.0.2.2', '[::1]', 'localhost',
                     'claude.ai.attacker.example', 'attackerclaude.ai', 'example.com'):
            self.assertFalse(environment.allowed_request(
                f'CONNECT {host}:443 HTTP/1.1\r\n\r\n'.encode()))

    def test_known_services_allowed(self):
        for host in ('claude.ai', 'downloads.claude.ai', 'api.anthropic.com'):
            self.assertTrue(environment.allowed_request(
                f'CONNECT {host}:443 HTTP/1.1\r\n\r\n'.encode()))
        self.assertTrue(environment.allowed_request(
            b'GET http://ports.ubuntu.com/ubuntu-ports/dists/noble/InRelease HTTP/1.1\r\n\r\n'))

    def test_proxy_rejects_non_tls_port_and_malformed_authority(self):
        for target in ('claude.ai:22', 'claude.ai:443/path', 'user@claude.ai:443',
                       'claude.ai.:443', 'claude.ai:invalid'):
            self.assertFalse(environment.allowed_request(
                f'CONNECT {target} HTTP/1.1\r\n\r\n'.encode()))

    def test_vm_has_no_direct_network_or_host_shares(self):
        cfg = environment.load_config(environment.ROOT / 'environment.example.json')
        cmd = environment.command(cfg, check=False)
        net = cmd[cmd.index('-netdev') + 1]
        self.assertIn('restrict=on', net)
        self.assertIn('ipv6=off', net)
        for unsafe in ('hostfwd=', 'smb=', '-virtfs', '-fsdev', '-spice', '-qmp'):
            self.assertNotIn(unsafe, ' '.join(cmd))

    def test_cloud_user_has_no_administration_rights(self):
        cfg = json.loads(environment.cloud_config().split('\n', 1)[1])
        self.assertEqual([u['name'] for u in cfg['users']], ['claude'])
        self.assertNotIn('sudo', cfg['users'][0])
        self.assertNotIn('sudo', cfg['users'][0]['groups'])
        self.assertFalse(cfg['ssh_pwauth'])

    def test_firewall_has_no_established_output_bypass(self):
        rules = (environment.ROOT / 'guest/firewall.nft').read_text()
        output = rules.split('chain output {', 1)[1]
        self.assertIn('policy drop', output)
        self.assertNotIn('ct state established', output)
        self.assertIn('ip daddr 10.0.2.100 tcp dport 7890 accept', output)

    def test_relay_rejects_host_request_without_upstream_proxy(self):
        result = subprocess.run(
            [sys.executable, str(environment.ROOT / 'environment.py'),
             'relay', '--port', '1'],
            input=b'CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n',
            capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith(b'HTTP/1.1 403 Forbidden'))
        self.assertEqual(result.stderr, b'')


if __name__ == '__main__':
    unittest.main()
