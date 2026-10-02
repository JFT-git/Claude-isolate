import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import environment
import network_guard as guard
from route_guard import RouteWatcher


class GuardTests(unittest.TestCase):
    def test_trace_country_uses_client_location_not_datacentre(self):
        result = guard.classify_trace(b'ip=8.8.8.8\nloc=RU\ncolo=EWR\n')
        self.assertFalse(result['allowed'])
        self.assertEqual(result['country'], 'RU')

    def test_trace_rejects_missing_unknown_or_duplicate_fields(self):
        for body in (b'ip=8.8.8.8\ncolo=US\n', b'ip=8.8.8.8\nloc=XX\n',
                     b'ip=8.8.8.8\nloc=RU\nloc=US\n',
                     b'ip=127.0.0.1\nloc=US\n', b'loc=US\n'):
            self.assertFalse(guard.classify_trace(body)['allowed'])

    def test_ru_is_blocked(self):
        self.assertFalse(guard.classify({'ip': '8.8.8.8', 'country': 'RU'})['allowed'])

    def test_non_ru_public_address_allowed(self):
        self.assertTrue(guard.classify({'ip': '8.8.8.8', 'country_code': 'US'})['allowed'])

    def test_unknown_malformed_private_addresses_blocked(self):
        for data in ({}, [], {'ip': '8.8.8.8'}, {'ip': '8.8.8.8', 'country': []},
                     {'ip': '8.8.8.8', 'country': 'ZZ'},
                     {'ip': '127.0.0.1', 'country': 'US'},
                     {'ip': 'invalid', 'country': 'US'}):
            self.assertFalse(guard.classify(data)['allowed'])

    def test_missing_corrupt_and_expired_lease_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'lease'
            self.assertFalse(guard.permitted(p))
            p.write_text('not json')
            self.assertFalse(guard.permitted(p))
            guard.publish(p, guard.classify({'ip': '8.8.8.8', 'country': 'US'}))
            self.assertTrue(guard.permitted(p))
            self.assertFalse(guard.permitted(p, now=time.monotonic() + 7))
            guard.publish(p, {'allowed': False})
            self.assertFalse(guard.permitted(p))

    def test_relay_cannot_start_without_guard_lease(self):
        env = dict(os.environ)
        env.pop('CLAUDE_NETWORK_LEASE', None)
        result = subprocess.run([sys.executable, str(environment.ROOT / 'environment.py'),
                                 'relay', '--port', '1'], env=env,
                                input=b'CONNECT claude.ai:443 HTTP/1.1\r\n\r\n',
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith(b'HTTP/1.1 503'))

    def test_active_connection_closes_after_foreign_ip_changes(self):
        self.check_active_connection_closes("ip")

    def test_route_event_closes_active_connection(self):
        self.check_active_connection_closes("route")

    def check_active_connection_closes(self, trigger):
        # Local fake HTTP proxy; no real VPN or external service is used.
        with tempfile.TemporaryDirectory() as directory, socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            server.settimeout(4)
            closed = threading.Event()
            def serve():
                with server.accept()[0] as client:
                    client.settimeout(4)
                    data = b''
                    while b'\r\n\r\n' not in data:
                        data += client.recv(4096)
                    client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                    if client.recv(1) == b'':
                        closed.set()
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            lease = Path(directory) / 'lease.json'
            guard.publish(lease, guard.classify({'ip': '8.8.8.8', 'country': 'US'}))
            env = dict(os.environ, CLAUDE_NETWORK_LEASE=str(lease))
            proc = subprocess.Popen([sys.executable, str(environment.ROOT / 'environment.py'),
                                     'relay', '--port', str(server.getsockname()[1])],
                                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            try:
                proc.stdin.write(b'CONNECT claude.ai:443 HTTP/1.1\r\n\r\n')
                proc.stdin.flush()
                # Wait for proxy establishment through a reader with a deadline.
                established = threading.Event()
                def read_status():
                    if proc.stdout.readline().startswith(b'HTTP/1.1 200'):
                        established.set()
                reader = threading.Thread(target=read_status, daemon=True)
                reader.start()
                self.assertTrue(established.wait(3))
                started = time.monotonic()
                if trigger == 'route':
                    source, sender = socket.socketpair()
                    watcher = RouteWatcher(lambda reason: guard.revoke(lease, reason=reason), source)
                    watcher.start()
                    try:
                        sender.sendall(struct.pack('=HBBII', 12, 5, 2, 0, 0))
                        self.assertTrue(closed.wait(.5))
                        print(f'Route event to active connection closed: {(time.monotonic() - started) * 1000:.1f} ms')
                    finally:
                        watcher.close()
                        sender.close()
                else:
                    policy = guard.SessionPolicy(guard.classify({'ip': '8.8.8.8', 'country': 'US'}))
                    guard.publish(lease, policy.evaluate(guard.classify({'ip': '1.1.1.1', 'country': 'NL'})))
                    self.assertTrue(closed.wait(3))
                proc.wait(timeout=3)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    stream.close()
                worker.join(timeout=1)


if __name__ == '__main__':
    unittest.main()
