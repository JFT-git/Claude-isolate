"""Real Windows full-duplex pipe operations, including a blocked reader."""
import ctypes
import importlib.util
from ctypes import wintypes
import os
import socket
import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from windows.pipe import DrainedPipe, NamedPipe, Overlapped, SETTLE_SECONDS, api, gateway_pipe, is_wine, WinePipe


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
            self.assertFalse(client._closed)
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
                    offset = 0
                    while offset < len(payload):
                        count = client.write(payload[offset:])
                        self.assertGreater(count, 0)
                        self.assertLessEqual(count, 2048)
                        offset += count
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
                self.assertTrue(client._closed)
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
                    payload = b'x' * (2 * 1024 * 1024)
                    offset = 0
                    while offset < len(payload):
                        offset += client.write(payload[offset:])
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


class GatewayPipeSelectionTests(unittest.TestCase):
    def test_native_windows_never_uses_the_synchronous_wine_adapter(self):
        for wine in (False, True):
            with self.subTest(wine=wine), patch('windows.pipe.is_wine', return_value=wine), \
                 patch('windows.pipe.NamedPipe') as native, patch('windows.pipe.WinePipe') as compatible:
                result = gateway_pipe('private-pipe')
                selected, unused = (compatible, native) if wine else (native, compatible)
                selected.assert_called_once_with('private-pipe')
                unused.assert_not_called()
                self.assertIs(result, selected.return_value)

    def test_wine_detection_uses_an_actual_ntdll_export(self):
        try:
            for library, expected in ((object(), False), (SimpleNamespace(wine_get_version=object()), True)):
                is_wine.cache_clear()
                with patch('windows.pipe.ctypes.WinDLL', return_value=library, create=True) as load:
                    self.assertIs(is_wine(), expected)
                    self.assertIs(is_wine(), expected)
                    load.assert_called_once_with('ntdll')
        finally:
            is_wine.cache_clear()


class PipeRaceTests(unittest.TestCase):
    """QEMU hangs if a client connects between CreateNamedPipe and ConnectNamedPipe."""

    def test_client_lets_qemu_start_waiting_before_it_connects(self):
        calls = []
        library = SimpleNamespace(WaitNamedPipeW=lambda path, timeout: calls.append(('wait', timeout)) or True,
                                  CreateFileW=lambda *arguments: calls.append(('connect',)) or 1234)
        with patch('windows.pipe.api', return_value=library), \
             patch('windows.pipe.time.sleep', side_effect=lambda seconds: calls.append(('sleep', seconds))):
            pipe = NamedPipe('\\\\.\\pipe\\claude-isolate-test')
        self.assertEqual(calls, [('wait', 1), ('sleep', SETTLE_SECONDS), ('connect',)])
        self.assertEqual(pipe.handle, 1234)

    def test_missing_pipe_is_not_opened(self):
        library = SimpleNamespace(WaitNamedPipeW=lambda path, timeout: False, CreateFileW=Mock())
        with patch('windows.pipe.api', return_value=library), \
             patch('windows.pipe.ctypes.get_last_error', return_value=2, create=True), \
             patch('windows.pipe.ctypes.WinError', side_effect=FileNotFoundError, create=True), \
             patch('windows.pipe.time.sleep') as sleep:
            with self.assertRaises(OSError):
                NamedPipe('\\\\.\\pipe\\claude-isolate-test')
        library.CreateFileW.assert_not_called()
        sleep.assert_not_called()


class PipePacketTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('paramiko'), 'SSH dependency not installed')
    def test_paramiko_retries_bounded_partial_sends_without_losing_binary_data(self):
        from paramiko.packet import Packetizer
        received = bytearray()
        class BoundedPeer:
            def write(self, data):
                if len(data) > 2048:
                    raise BufferError('QEMU pipe frame exceeds the safe bound')
                received.extend(data)
                return len(data)
        stream = WinePipe.__new__(WinePipe)
        stream.writer = BoundedPeer()
        payload = bytes(range(256)) * 513
        Packetizer(stream).write_all(payload)
        self.assertEqual(received, payload)


class SocketPipe:
    """A full-duplex stream with small buffers, like QEMU's pipe."""
    def __init__(self, sock):
        self.sock, self.closed = sock, False

    def recv(self, count):
        return self.sock.recv(count)

    def send(self, data):
        return self.sock.send(data)

    def close(self):
        self.closed = True
        # Linux does not wake a blocked recv() when another thread closes.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class DrainedPipeTests(unittest.TestCase):
    """QEMU blocks while it writes and reads only between writes."""
    def pair(self):
        ours, theirs = socket.socketpair()
        for sock in (ours, theirs):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.addCleanup(theirs.close)
        return ours, theirs

    def test_peer_write_completes_while_the_reader_is_sending(self):
        ours, theirs = self.pair()
        payload = os.urandom(4 * 1024 * 1024)
        echoed = bytearray()
        def qemu():
            theirs.sendall(payload)
            while len(echoed) < len(payload):
                chunk = theirs.recv(65536)
                if not chunk:
                    break
                echoed.extend(chunk)
        peer = threading.Thread(target=qemu, daemon=True)
        pipe = DrainedPipe(SocketPipe(ours))
        self.addCleanup(pipe.close)
        peer.start()
        def transport():
            # paramiko's transport thread sends before it reads again.
            data = payload
            while data:
                data = data[pipe.send(data):]
        sender = threading.Thread(target=transport, daemon=True)
        sender.start()
        sender.join(20)
        self.assertFalse(sender.is_alive(), 'Sending waited for the peer, which waited for a reader')
        received = bytearray()
        pipe.settimeout(10)
        while len(received) < len(payload):
            received.extend(pipe.recv(1 << 20))
        peer.join(20)
        self.assertEqual(received, payload)
        self.assertEqual(echoed, payload)

    def test_recv_times_out_like_a_socket_and_ends_with_the_pipe(self):
        ours, theirs = self.pair()
        pipe = DrainedPipe(SocketPipe(ours))
        pipe.settimeout(.05)
        with self.assertRaises(socket.timeout):
            pipe.recv(10)
        self.assertEqual(pipe.peek(), 0)
        theirs.sendall(b'tail')
        theirs.close()
        pipe.thread.join(5)
        self.assertEqual(pipe.peek(), 4)
        self.assertEqual(pipe.read(10), b'tail')
        self.assertIsNone(pipe.peek())
        self.assertEqual(pipe.recv(10), b'')
        pipe.close()
        self.assertTrue(pipe.closed and pipe._closed)

    def test_reading_stops_at_the_memory_limit_and_resumes(self):
        ours, theirs = self.pair()
        with patch.object(DrainedPipe, 'LIMIT', 8):
            pipe = DrainedPipe(SocketPipe(ours))
            self.addCleanup(pipe.close)
            pipe.settimeout(5)
            theirs.sendall(b'a' * 8)
            pipe.recv(0)  # Waits for the first bytes.
            theirs.sendall(b'b' * 8)
            threading.Event().wait(.2)
            self.assertEqual(pipe.peek(), 8)
            self.assertEqual(pipe.recv(8), b'a' * 8)
            self.assertEqual(pipe.recv(8), b'b' * 8)

    def test_close_ends_the_reader_thread(self):
        ours, _ = self.pair()
        pipe = DrainedPipe(SocketPipe(ours))
        pipe.close()
        pipe.thread.join(5)
        self.assertFalse(pipe.thread.is_alive())
        self.assertEqual(pipe.recv(10), b'')
