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
