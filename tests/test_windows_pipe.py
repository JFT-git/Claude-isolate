"""Real Windows full-duplex pipe operations, including a blocked reader."""
import ctypes
from ctypes import wintypes
import os
import threading
import unittest
import uuid

from windows.pipe import NamedPipe, Overlapped, api


@unittest.skipUnless(os.name == 'nt', 'Native Windows named-pipe test')
class NativePipeTests(unittest.TestCase):
    def test_pending_read_does_not_block_a_large_write(self):
        library = api()
        library.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        library.CreateNamedPipeW.restype = wintypes.HANDLE
        library.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
        library.ConnectNamedPipe.restype = wintypes.BOOL
        path = '\\\\.\\pipe\\claude-test-' + uuid.uuid4().hex
        handle = library.CreateNamedPipeW(path, 0x40000003, 0, 1, 4096, 4096, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        server = NamedPipe(handle=handle)
        client = None
        payload = b'isolated full duplex\x00' * 8192
        failures, replies = [], []
        waiting = threading.Event()
        acknowledged = threading.Event()
        def echo():
            event = library.CreateEventW(None, True, False, None)
            overlapped = Overlapped(hEvent=event)
            try:
                if not library.ConnectNamedPipe(handle, ctypes.byref(overlapped)):
                    error = ctypes.get_last_error()
                    if error == 997:
                        count = wintypes.DWORD()
                        if not library.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(count), True):
                            raise ctypes.WinError(ctypes.get_last_error())
                    elif error != 535:  # Client connected before ConnectNamedPipe.
                        raise ctypes.WinError(error)
                data = bytearray()
                while len(data) < len(payload):
                    block = server.read(32768)
                    if not block:
                        raise RuntimeError('Pipe closed before all data arrived')
                    data.extend(block)
                self.assertEqual(bytes(data), payload)
                server.write(b'full-duplex-ok\n')
                self.assertTrue(acknowledged.wait(5), 'A short read waited for the server to close')
            except Exception as error:
                failures.append(error)
            finally:
                library.CloseHandle(event)
                server.close()
        peer = threading.Thread(target=echo, daemon=True)
        peer.start()
        try:
            client = NamedPipe(path)
            self.assertEqual(client.read(0), b'')
            self.assertEqual(client.write(b''), 0)
            def receive():
                try:
                    waiting.set()
                    replies.append(client.readline())
                    acknowledged.set()
                except Exception as error:
                    failures.append(error)
            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            self.assertTrue(waiting.wait(2))
            # The peer sends nothing until the entire large write is received.
            # A synchronous pipe serializing read/write would deadlock here.
            def transmit():
                try:
                    self.assertEqual(client.write(payload), len(payload))
                except Exception as error:
                    failures.append(error)
            writer = threading.Thread(target=transmit, daemon=True)
            writer.start()
            writer.join(10)
            self.assertFalse(writer.is_alive(), 'A pending read blocked pipe writes')
            reader.join(10)
            self.assertFalse(reader.is_alive())
            self.assertEqual(replies, [b'full-duplex-ok\n'])
        finally:
            if client:
                client.close()
            server.close()
            peer.join(5)
        self.assertFalse(failures)

    def test_close_cancels_an_idle_reader_and_a_blocked_writer(self):
        library = api()
        library.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        library.CreateNamedPipeW.restype = wintypes.HANDLE
        path = '\\\\.\\pipe\\claude-close-' + uuid.uuid4().hex
        handle = library.CreateNamedPipeW(path, 0x40000003, 0, 1, 4096, 4096, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        server = NamedPipe(handle=handle)
        client = NamedPipe(path)
        failures = []
        def operation(writing):
            try:
                if writing:
                    client.write(b'x' * (2 * 1024 * 1024))
                else:
                    client.read(1)
            except OSError:
                pass  # Closing the client must interrupt pending operations.
            except Exception as error:
                failures.append(error)
        reader = threading.Thread(target=operation, args=(False,), daemon=True)
        writer = threading.Thread(target=operation, args=(True,), daemon=True)
        closer = threading.Thread(target=client.close, daemon=True)
        try:
            reader.start()
            writer.start()
            writer.join(.05)
            self.assertTrue(writer.is_alive(), 'The peer must leave the large write pending')
            closer.start()
            closer.join(2)
            self.assertFalse(closer.is_alive(), 'close() did not cancel pending I/O')
            reader.join(2)
            writer.join(2)
            self.assertFalse(reader.is_alive())
            self.assertFalse(writer.is_alive())
            self.assertFalse(failures)
        finally:
            server.close()
            if closer.ident is not None:
                closer.join(2)
            client.close()
