"""Host-controller regressions; skip on Windows where fcntl is unavailable."""
import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'macos'))


@unittest.skipIf(os.name == 'nt', 'macOS/POSIX backend')
class BackendTests(unittest.TestCase):
    def setUp(self):
        import backend
        self.backend = backend

    def test_status_read_is_bounded_for_large_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'boot.log'
            path.write_bytes(b'x' * 300000 + b'\nCLAUDE-ISOLATION: desktop-ready\n')
            result = self.backend.boot_tail(path)
            self.assertLessEqual(len(result), 262144)
            self.assertIn('desktop-ready', result)

    def test_corrupt_network_status_does_not_crash_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            for value in ([], None, 42):
                path.write_text(json.dumps(value))
                self.assertFalse(self.backend.network_state({'network_status': str(path)})['allowed'])

    def test_config_write_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'environment.json'
            self.backend.write_config(path, {'value': 1})
            with patch.object(self.backend.os, 'replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    self.backend.write_config(path, {'value': 2})
            self.assertEqual(json.loads(path.read_text()), {'value': 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_permission_denial_is_explicit_so_ui_stops_polling(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['backend.py', 'status', '--data', '/unused']), \
             patch.object(self.backend, 'config', side_effect=PermissionError('Access denied')), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit):
                self.backend.main()
        self.assertTrue(json.loads(output.getvalue())['permission_error'])

    @unittest.skipUnless(sys.platform == "darwin", "Native macOS runtime path")
    def test_economy_profile_saved_for_next_start(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / 'macos/backend.py'), 'economy', '--data', directory], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            config = json.loads(Path(directory, 'environment.json').read_text())
            self.assertEqual((config['memory_mb'], config['cpus']), (3072, 2))


if __name__ == '__main__':
    unittest.main()
