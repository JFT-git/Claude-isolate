import unittest
from unittest import mock

from windows.shared_sync import _LocalhostKeys


class HostKeyPolicyTests(unittest.TestCase):
    def test_accepts_paramiko_name_for_forwarded_port(self):
        client = mock.Mock()
        key = mock.Mock()
        key.get_name.return_value = 'ssh-ed25519'
        _LocalhostKeys().missing_host_key(client, '[127.0.0.1]:2222', key)
        client.get_host_keys().add.assert_called_once_with('[127.0.0.1]:2222', 'ssh-ed25519', key)

    def test_rejects_other_hosts(self):
        with self.assertRaises(OSError):
            _LocalhostKeys().missing_host_key(mock.Mock(), '[10.0.0.5]:2222', mock.Mock())


if __name__ == '__main__':
    unittest.main()
