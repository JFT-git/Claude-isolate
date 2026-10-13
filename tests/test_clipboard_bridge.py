import threading
import unittest
from unittest.mock import patch

from windows.clipboard_bridge import ClipboardBridge


class _Pipe:
    def __init__(self):
        self.closed = False

    def peek(self):
        return False

    def read(self, _size):
        return b''

    def write(self, data):
        return len(data)

    def close(self):
        self.closed = True


class ClipboardBridgeTests(unittest.TestCase):
    def test_clipboard_api_error_does_not_drop_the_qemu_pipe(self):
        bridge = ClipboardBridge.__new__(ClipboardBridge)
        bridge.stop = threading.Event()
        bridge.last_text = ''
        bridge.last_png = ''
        bridge.cfg = {'qmp_pipe': 'test'}
        pipe = _Pipe()
        seen = {}

        def broken_text():
            seen['open_during_error'] = not pipe.closed
            bridge.stop.set()
            raise AttributeError("function 'GlobalLock' not found")

        bridge.get_text = broken_text
        bridge.get_png = lambda: None
        with patch('windows.clipboard_bridge.gateway_pipe', return_value=pipe):
            bridge.run()
        self.assertTrue(seen['open_during_error'])
        self.assertTrue(pipe.closed)


if __name__ == '__main__':
    unittest.main()


class PortNameTests(unittest.TestCase):
    def test_guest_port_path_matches_qemu_device_name(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        guest = (root / 'guest/clipboard-bridge.py').read_text()
        qemu = (root / 'environment.py').read_text()
        name = re.search(r"virtserialport,chardev=clipboard,name=([\w.-]+)", qemu).group(1)
        self.assertIn("/dev/virtio-ports/" + name + "'", guest)
        self.assertIn("DISPLAY = ':0'", guest)
        self.assertIn("XAUTHORITY = '/home/claude/.Xauthority'", guest)
        self.assertIn("runuser', '-u', 'claude'", guest)

    def test_setting_clipboard_does_not_wait_for_xclip_to_exit(self):
        import importlib.util
        import subprocess
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / 'guest/clipboard-bridge.py'
        spec = importlib.util.spec_from_file_location('guest_clipboard_bridge_under_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Proc:
            def __init__(self):
                self.stdin = self
                self.returncode = None
                self.data = b''

            def write(self, data):
                self.data += data

            def close(self):
                pass

            def wait(self, timeout=None):
                if timeout is None:
                    raise AssertionError('xclip set must not wait forever')
                raise subprocess.TimeoutExpired(cmd='xclip', timeout=timeout)

            def poll(self):
                return None

        seen = {}

        def popen(cmd, **kwargs):
            seen['cmd'] = cmd
            seen['kwargs'] = kwargs
            return Proc()

        with patch.object(module.subprocess, 'Popen', popen):
            self.assertIsNone(module.xclip([], b'hello'))
        self.assertIn('DISPLAY=:0', seen['cmd'])
        self.assertEqual(seen['cmd'][-3:], ['xclip', '-selection', 'clipboard'])
        self.assertTrue(seen['kwargs'].get('start_new_session'))
        self.assertIsNotNone(module._selection_owner)
