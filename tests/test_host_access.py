"""macOS and Linux get the same localhost SSH, shared folder and clipboard port."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import environment
import host_access


class HostAccessTests(unittest.TestCase):
    def test_darwin_and_linux_forward_ssh_and_clipboard(self):
        cfg = environment.load_config(environment.ROOT / 'environment.example.json')
        cfg['qmp_socket'] = '/private/tmp/claude-env-test/control.sock'
        for system in ('Darwin', 'Linux'):
            with patch.object(environment.platform, 'system', return_value=system), \
                 patch.object(environment.platform, 'machine', return_value='arm64'):
                cmd = environment.command(cfg, check=False)
            net = cmd[cmd.index('-netdev') + 1]
            self.assertIn('restrict=on', net)
            self.assertIn('hostfwd=tcp:127.0.0.1:2222-:22', net)
            self.assertEqual(net.count('hostfwd='), 1)
            self.assertNotIn('hostfwd=tcp:0.0.0.0', net)
            self.assertIn('guestfwd=', net)
            self.assertIn('virtserialport,chardev=clipboard,name=claude.clipboard,id=clipboardport', cmd)
            chardev = next(item for item in cmd if item.startswith('socket,id=clipboard,'))
            self.assertIn('server=on,wait=off', chardev)
            self.assertIn('claude-env-test', chardev)
            self.assertIn('clipboard.sock', chardev)
            self.assertNotIn('-virtfs', cmd)

    def test_prepare_access_creates_shared_dir_and_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            cfg = {}
            pubkey = host_access.prepare_access(cfg, data)
            self.assertTrue((data / 'shared').is_dir())
            self.assertTrue((data / 'tools' / 'ssh_host_key').is_file())
            self.assertTrue(pubkey.startswith('ssh-ed25519 '))
            self.assertEqual(cfg['ssh_pubkey'], pubkey)
            again = host_access.prepare_access(cfg, data)
            self.assertEqual(again, pubkey)


if __name__ == '__main__':
    unittest.main()
