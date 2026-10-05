"""Full-duplex Windows named-pipe I/O without synchronous CRT descriptor locks."""
import ctypes
from ctypes import wintypes
from functools import lru_cache
import threading


class Overlapped(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                ('Offset', ctypes.c_uint32), ('OffsetHigh', ctypes.c_uint32),
                ('hEvent', wintypes.HANDLE)]


@lru_cache(maxsize=1)
def api():
    library = ctypes.WinDLL('kernel32', use_last_error=True)
    handle, pointer, dword = wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD
    library.CreateFileW.argtypes = [wintypes.LPCWSTR, dword, dword, pointer, dword, dword, handle]
    library.CreateFileW.restype = handle
    library.CreateEventW.argtypes = [pointer, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    library.CreateEventW.restype = handle
    for name in ('ReadFile', 'WriteFile'):
        function = getattr(library, name)
        function.argtypes = [handle, pointer, dword, ctypes.POINTER(dword), ctypes.POINTER(Overlapped)]
        function.restype = wintypes.BOOL
    library.GetOverlappedResult.argtypes = [handle, ctypes.POINTER(Overlapped), ctypes.POINTER(dword), wintypes.BOOL]
    library.GetOverlappedResult.restype = wintypes.BOOL
    library.CancelIoEx.argtypes = [handle, ctypes.POINTER(Overlapped)]
    library.CancelIoEx.restype = wintypes.BOOL
    library.PeekNamedPipe.argtypes = [handle, pointer, dword, ctypes.POINTER(dword),
                                    ctypes.POINTER(dword), ctypes.POINTER(dword)]
    library.PeekNamedPipe.restype = wintypes.BOOL
    library.CloseHandle.argtypes = [handle]
    library.CloseHandle.restype = wintypes.BOOL
    return library


class NamedPipe:
    def __init__(self, path=None, *, handle=None):
        self.api = api()
        if handle is None:
            # A synchronous FILE_OBJECT serializes even duplicated handles.
            # FILE_FLAG_OVERLAPPED allows independent reads and writes; each
            # operation owns its own OVERLAPPED structure and completion event.
            handle = self.api.CreateFileW(path, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        self.closed = False
        self.reader_lock, self.writer_lock = threading.Lock(), threading.Lock()
        self.close_lock = threading.Lock()
        self.buffer = bytearray()
        self.wake = threading.Event()

    def operation(self, writing, data):
        lock = self.writer_lock if writing else self.reader_lock
        with lock:
            if self.closed:
                raise OSError('Windows pipe is closed')
            size = len(data) if writing else data
            if size == 0:
                return 0 if writing else b''
            if not writing:
                # Keep idle reads out of QEMU/Wine's pipe polling path. Read
                # only bytes already present; one caller owns the read side.
                # This also gives close() a prompt cancellation point while
                # retaining independent OVERLAPPED completion for each I/O.
                while True:
                    available = wintypes.DWORD()
                    if not self.api.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
                        error = ctypes.get_last_error()
                        if error in (109, 232):
                            return b''
                        raise ctypes.WinError(error)
                    if available.value:
                        size = min(size, available.value)
                        break
                    if self.wake.wait(.005):
                        raise OSError('Windows pipe is closed')
            buffer = ctypes.create_string_buffer(data, size) if writing else ctypes.create_string_buffer(size)
            count = wintypes.DWORD()
            event = self.api.CreateEventW(None, True, False, None)
            if not event:
                raise ctypes.WinError(ctypes.get_last_error())
            overlapped = Overlapped(hEvent=event)
            try:
                function = self.api.WriteFile if writing else self.api.ReadFile
                success = function(self.handle, buffer, size, ctypes.byref(count), ctypes.byref(overlapped))
                error = ctypes.get_last_error() if not success else 0
                if error == 997:  # ERROR_IO_PENDING; buffers remain alive until completion.
                    success = self.api.GetOverlappedResult(self.handle, ctypes.byref(overlapped),
                                                          ctypes.byref(count), True)
                    error = ctypes.get_last_error() if not success else 0
                if not success:
                    if not writing and error in (109, 232):  # Peer closed.
                        return b''
                    raise ctypes.WinError(error)
                return count.value if writing else buffer.raw[:count.value]
            finally:
                self.api.CloseHandle(event)

    def read(self, count):
        if self.buffer:
            result = bytes(self.buffer[:count])
            del self.buffer[:count]
            return result
        return self.operation(False, count)

    def write(self, data):
        return self.operation(True, bytes(data))

    def readline(self, limit=65537):
        while True:
            index = self.buffer.find(b'\n', 0, limit)
            if index >= 0 or len(self.buffer) >= limit:
                size = index + 1 if index >= 0 else limit
                result = bytes(self.buffer[:size])
                del self.buffer[:size]
                return result
            block = self.operation(False, min(4096, limit - len(self.buffer)))
            if not block:
                result = bytes(self.buffer)
                self.buffer.clear()
                return result
            self.buffer.extend(block)

    recv = read
    send = write

    def settimeout(self, value):
        pass  # Cancellation and QEMU exit release pending I/O; no partial SSH writes.

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            self.wake.set()
            # An operation may have passed its closed check just before this
            # thread sets it. Repeat cancellation until both sides drain so
            # even I/O submitted after the first CancelIoEx is cancelled.
            while True:
                self.api.CancelIoEx(self.handle, None)
                if self.reader_lock.acquire(blocking=False):
                    if self.writer_lock.acquire(blocking=False):
                        break
                    self.reader_lock.release()
                threading.Event().wait(.005)
            try:
                self.api.CloseHandle(self.handle)
            finally:
                self.writer_lock.release()
                self.reader_lock.release()
