"""Adversarial requests and failure recovery found during the full audit."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import environment
import network_guard
import network_transport
import ubuntu_image
import sarif_gate
from session_lock import exclusive


class RequestTests(unittest.TestCase):
    def test_control_characters_and_ambiguous_headers_rejected(self):
        requests = [
            b'GET http://claude.ai/\rX HTTP/1.1\r\n\r\n',
            b'GET http://claude.ai/\tX HTTP/1.1\r\n\r\n',
            b'GET http://claude.ai/#fragment HTTP/1.1\r\n\r\n',
            b'CONNECT @claude.ai:443 HTTP/1.1\r\n\r\n',
            b'GET http://claude.ai/ HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n',
            b'GET http://claude.ai/ HTTP/1.1\r\n Transfer-Encoding: chunked\r\n\r\n',
            b'GET http://claude.ai/ HTTP/1.1\r\nContent-Length : 1\r\n\r\n',
            b'CONNECT claude.ai:443 HTTP/1.1\r\nX: a\nb\r\n\r\n',
            b'GET http://claude.ai/ HTTP/1.1\r\nUpgrade: websocket\r\n\r\n',
        ]
        for request in requests:
            for mode in ('public', 'services'):
                with self.subTest(request=request, mode=mode):
                    self.assertFalse(environment.allowed_request(request, mode))

    def test_multicast_and_reserved_addresses_are_not_public_endpoints(self):
        for ip in ('224.0.0.1', '239.255.255.250', '240.0.0.1', '0.0.0.0', '127.0.0.1'):
            self.assertFalse(network_guard.classify({'ip': ip, 'country': 'US'})['allowed'])
            self.assertFalse(environment.allowed_request(f'CONNECT {ip}:443 HTTP/1.1\r\n\r\n'.encode(), 'public'))
            with patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]), patch.object(socket, 'socket') as create:
                with self.assertRaises(OSError):
                    network_transport.open_public('example.com', 443)
                create.assert_not_called()

    def test_corrupt_lease_shapes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory) / 'lease.json'
            for data in ([], None, 42, {'allowed': True, 'country': ['US']}, {'allowed': True, 'country': 'US', 'ip': '224.0.0.1'}):
                lease.write_text(json.dumps(data))
                self.assertFalse(network_guard.permitted(lease))

    def test_plain_http_preserves_headers_but_never_forwards_pipeline(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as server:
            server.bind(('127.0.0.1', 0)); server.listen(); server.settimeout(5)
            received = []
            errors = []
            def serve():
                try:
                    with server.accept()[0] as client:
                        client.settimeout(5)
                        data = b''
                        while b'\r\n\r\n' not in data:
                            data += client.recv(4096)
                        received.append(data)
                        client.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=serve, daemon=True); worker.start()
            lease = Path(directory) / 'lease.json'
            network_guard.publish(lease, network_guard.classify({'ip': '8.8.8.8', 'country': 'US'}))
            code = ('import sys,socket;sys.path.insert(0,sys.argv[1]);import environment;'
                    'environment.open_public=lambda *a,**k:socket.create_connection(("127.0.0.1",int(sys.argv[2])),3);'
                    'environment.relay(mode="system",web_access="public")')
            request = (b'GET http://example.com/file?q=1 HTTP/1.1\r\nHost: spoofed\r\n'
                       b'Range: bytes=10-\r\nCookie: session=test\r\nUser-Agent: audit\r\n'
                       b'Connection: keep-alive, X-Remove\r\nX-Remove: gone\r\nProxy-Authorization: secret\r\n\r\n'
                       b'GET http://127.0.0.1/private HTTP/1.1\r\n\r\n')
            result = subprocess.run([sys.executable, '-c', code, str(ROOT), str(server.getsockname()[1])],
                                    input=request, capture_output=True, timeout=6,
                                    env=dict(os.environ, CLAUDE_NETWORK_LEASE=str(lease)))
            worker.join(2)
            self.assertFalse(errors)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(received), 1)
            self.assertIn(b'GET /file?q=1 HTTP/1.1\r\nHost: example.com\r\n', received[0])
            for header in (b'Range: bytes=10-', b'Cookie: session=test', b'User-Agent: audit'):
                self.assertIn(header, received[0])
            for forbidden in (b'spoofed', b'X-Remove', b'secret', b'127.0.0.1', b'keep-alive'):
                self.assertNotIn(forbidden, received[0])
            self.assertTrue(result.stdout.endswith(b'ok'))


class InstallationTests(unittest.TestCase):
    def test_failed_prepare_leaves_retryable_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            base = directory / 'base.img'; base.write_bytes(b'image')
            disk, seed = directory / 'disk.qcow2', directory / 'seed.iso'
            cfg = dict(disk=str(disk), seed=str(seed))
            digest = hashlib.sha256(b'image').hexdigest()
            def failed_run(cmd, **kw):
                if cmd[1] == 'convert':
                    Path(cmd[-1]).write_bytes(b'partial')
                else:
                    raise subprocess.CalledProcessError(1, cmd)
            with patch.object(environment, 'tool', return_value='qemu-img'), patch.object(environment.subprocess, 'run', side_effect=failed_run):
                with self.assertRaises(subprocess.CalledProcessError):
                    environment.prepare(cfg, base, digest)
            self.assertFalse(disk.exists()); self.assertFalse(seed.exists())
            self.assertEqual(list(directory.iterdir()), [base])

    def test_seed_failure_preserves_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            base = directory / 'base.img'; base.write_bytes(b'image')
            disk, seed = directory / 'disk.qcow2', directory / 'seed.iso'
            cfg = dict(disk=str(disk), seed=str(seed))
            def run(cmd, **kw):
                if cmd[1] == 'convert':
                    Path(cmd[-1]).write_bytes(b'disk')
                elif cmd[1] == 'makehybrid':
                    Path(cmd[cmd.index('-o') + 1]).write_bytes(b'seed')
                    seed.write_bytes(b'other-installer')
            with patch.object(environment, 'tool', side_effect=lambda n:n), patch.object(environment.platform, 'system', return_value='Darwin'), patch.object(environment.subprocess, 'run', side_effect=run):
                with self.assertRaises(FileExistsError):
                    environment.prepare(cfg, base, hashlib.sha256(b'image').hexdigest())
            self.assertFalse(disk.exists())
            self.assertEqual(seed.read_bytes(), b'other-installer')

    def test_stale_ubuntu_cache_refetched_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            filename = 'noble-server-cloudimg-arm64.img'
            image = directory / filename; image.write_bytes(b'old')
            digest = hashlib.sha256(b'new').hexdigest()
            def fetch(url, target):
                target.write_bytes((digest + ' *' + filename).encode() if target.name == 'SHA256SUMS' else b'new')
            result = Mock(stdout='[GNUPG:] VALIDSIG ' + ubuntu_image.FINGERPRINT + ' date')
            with patch.object(ubuntu_image, 'fetch', side_effect=fetch) as download, patch.object(ubuntu_image.subprocess, 'run', return_value=result):
                path, actual = ubuntu_image.download(directory, 'aarch64', 'gpg')
                self.assertEqual(path.read_bytes(), b'new'); self.assertEqual(actual, digest)
                self.assertEqual(download.call_count, 4)
                download.reset_mock()
                ubuntu_image.download(directory, 'aarch64', 'gpg')
                self.assertEqual(download.call_count, 3)  # valid cached image reused

    def test_bad_ubuntu_signature_never_downloads_or_overwrites_image(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            image = directory / 'noble-server-cloudimg-arm64.img'; image.write_bytes(b'old')
            with patch.object(ubuntu_image, 'fetch') as fetch, patch.object(ubuntu_image.subprocess, 'run', return_value=Mock(stdout='[GNUPG:] VALIDSIG WRONG date')):
                with self.assertRaises(RuntimeError):
                    ubuntu_image.download(directory, 'aarch64', 'gpg')
                self.assertEqual(image.read_bytes(), b'old')
                self.assertEqual(fetch.call_count, 3)

    def test_launch_lock_excludes_other_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lock'
            code = 'from session_lock import exclusive;import sys\nwith exclusive(sys.argv[1]): pass'
            with exclusive(path):
                result = subprocess.run([sys.executable, '-c', code, str(path)], cwd=ROOT, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
            result = subprocess.run([sys.executable, '-c', code, str(path)], cwd=ROOT, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)


class ScanGateTests(unittest.TestCase):
    def test_empty_or_failed_scan_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sarif'
            for report in ({'runs': []}, {'runs': [{'invocations': [{'executionSuccessful': False}]}]}):
                path.write_text(json.dumps(report))
                with self.assertRaises(RuntimeError):
                    sarif_gate.findings(directory)

    def test_extension_severity_and_rule_default_level_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            report = {'runs': [{'tool': {'driver': {}, 'extensions': [{'rules': [
                {'id': 'medium', 'properties': {'security-severity': '5'}},
                {'id': 'error', 'defaultConfiguration': {'level': 'error'}}]}]},
                'results': [{'ruleId': r, 'message': {'text': 'test'}} for r in ('medium', 'error', 'missing')]}]}
            Path(directory, 'test.sarif').write_text(json.dumps(report))
            self.assertEqual(len(sarif_gate.findings(directory)), 3)

@unittest.skipIf(os.name == 'nt', 'POSIX termination handling; Windows runtime remains a preview')
class LifecycleTests(unittest.TestCase):
    def test_termination_stops_owned_child_and_revokes_network(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for name in ('disk', 'seed'):
                (directory / name).touch()
            config = dict(arch='x86_64', memory_mb=3072, cpus=2, network_mode='system',
                          disk=str(directory / 'disk'), seed=str(directory / 'seed'),
                          network_status=str(directory / 'network.json'))
            cfg = directory / 'config.json'; cfg.write_text(json.dumps(config))
            child = directory / 'child.py'
            child.write_text('import signal,sys,time\nfrom pathlib import Path\n'
                             'def stop(*a):\n Path(sys.argv[2]).touch()\n sys.exit(0)\n'
                             'signal.signal(signal.SIGTERM,stop)\nPath(sys.argv[1]).touch()\nwhile True: time.sleep(.1)\n')
            code = ('import environment,sys\n'
                    'environment.platform.system=lambda:"Linux"\n'
                    'environment.command=lambda *a,**k:[sys.executable,sys.argv[2],sys.argv[3],sys.argv[4]]\n'
                    'environment.network_guard.probe=lambda *a,**k:{"allowed":True,"ip":"8.8.8.8","country":"US"}\n'
                    'sys.argv=["environment.py","start","--config",sys.argv[1]]\n'
                    'environment.main()')
            # Bind child arguments before environment.main replaces sys.argv.
            code = code.replace('environment.command=lambda *a,**k:[sys.executable,sys.argv[2],sys.argv[3],sys.argv[4]]',
                                'child_args=[sys.executable,*sys.argv[2:5]]\nenvironment.command=lambda *a,**k:child_args')
            started, stopped = directory / 'started', directory / 'stopped'
            proc = subprocess.Popen([sys.executable, '-c', code, str(cfg), str(child), str(started), str(stopped)],
                                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline and proc.poll() is None:
                    time.sleep(.01)
                self.assertTrue(started.exists())
                proc.terminate()
                proc.communicate(timeout=5)
                self.assertTrue(stopped.exists())
                self.assertTrue((directory / 'network.revoked').exists())
                self.assertFalse(network_guard.permitted(directory / 'network.json'))
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
