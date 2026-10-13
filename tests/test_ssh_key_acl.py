import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows import ssh_keys


class KeyAclTests(unittest.TestCase):
    def test_windows_key_is_restricted_to_owner(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout='"DOM\\\\user","S-1-5-21-1-2-3-1001"\n')

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(ssh_keys.sys, 'platform', 'win32'), \
                mock.patch.object(ssh_keys.subprocess, 'run', fake_run):
            ssh_keys.ensure_client_keypair(Path(folder) / 'ssh_host_key')
            ssh_keys.ensure_client_keypair(Path(folder) / 'ssh_host_key')
        icacls = [c for c in calls if c[0] == 'icacls']
        self.assertEqual(len(icacls), 2)
        self.assertIn('/inheritance:r', icacls[0])
        self.assertIn('*S-1-5-21-1-2-3-1001:F', icacls[0])


if __name__ == '__main__':
    unittest.main()
