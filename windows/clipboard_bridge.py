"""Host-side clipboard bridge for the claude.clipboard virtio-serial port."""
import ctypes
import hashlib
import sys
import threading
import time
from types import SimpleNamespace

from windows.clipboard_framing import TEXT, PNG, pack_text, pack_png, parse
from windows.clipboard_image import dib_to_png, png_to_dib
from windows.pipe import gateway_pipe

CF_UNICODETEXT = 13
CF_DIB = 8
CF_DIBV5 = 17


class ClipboardBridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.last_text = ''
        self.last_png = ''

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def run(self):
        pipe = None
        try:
            deadline = time.monotonic() + 60
            path = '\\\\.\\pipe\\' + self.cfg['qmp_pipe'] + '-clipboard'
            while not self.stop.is_set():
                try:
                    pipe = gateway_pipe(path)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        return
                    self.stop.wait(.2)
            if not pipe:
                return
            carry = b''
            while not self.stop.is_set() and not pipe.closed:
                try:
                    if pipe.peek():
                        chunk = pipe.read(65536)
                        if not chunk:
                            break
                        messages, carry = parse(carry, chunk)
                        for kind, payload in messages:
                            if kind == TEXT:
                                self.set_text(payload)
                                self.last_text = hashlib.sha256(payload.encode('utf-8')).hexdigest()
                            else:
                                self.set_png(payload)
                                self.last_png = hashlib.sha256(payload).hexdigest()
                    text = self.get_text()
                    if text:
                        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
                        if digest != self.last_text:
                            self.last_text = digest
                            self.write_all(pipe, pack_text(text))
                    png = self.get_png()
                    if png:
                        digest = hashlib.sha256(png).hexdigest()
                        if digest != self.last_png:
                            self.last_png = digest
                            self.write_all(pipe, pack_png(png))
                except OSError:
                    break
                except Exception as error:
                    # A Win32 clipboard failure must not close the pipe. QEMU's
                    # Windows chardev does not reconnect, and a closed pipe stalls it.
                    print('Windows clipboard bridge: ' + str(error), file=sys.stderr, flush=True)
                self.stop.wait(.3)
        except Exception as error:
            print('Windows clipboard bridge: ' + str(error), file=sys.stderr, flush=True)
        finally:
            if pipe and not pipe.closed and self.stop.is_set():
                pipe.close()

    @staticmethod
    def write_all(pipe, data):
        while data and not pipe.closed:
            data = data[pipe.write(data):]

    @staticmethod
    def user32():
        library = ctypes.WinDLL('user32', use_last_error=True)
        library.OpenClipboard.argtypes = [ctypes.c_void_p]
        library.OpenClipboard.restype = ctypes.c_int
        library.CloseClipboard.argtypes = []
        library.GetClipboardData.argtypes = [ctypes.c_uint]
        library.GetClipboardData.restype = ctypes.c_void_p
        library.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        library.IsClipboardFormatAvailable.restype = ctypes.c_int
        library.EmptyClipboard.argtypes = []
        library.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        library.SetClipboardData.restype = ctypes.c_void_p
        return library

    @staticmethod
    def win32(name, restype, *argtypes):
        """Resolve a kernel32 export, including forwarders ctypes cannot load."""
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        get_proc = kernel32.GetProcAddress
        get_proc.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        get_proc.restype = ctypes.c_void_p
        encoded = name.encode('ascii')
        for library_name in ('kernel32', 'kernelbase'):
            library = ctypes.WinDLL(library_name, use_last_error=True)
            address = get_proc(library._handle, encoded)
            if address:
                return ctypes.WINFUNCTYPE(restype, *argtypes)(address)
        raise AttributeError("function '" + name + "' not found")

    @staticmethod
    def kernel32():
        # GlobalAlloc/GlobalLock are forwarded from kernel32 to kernelbase.
        # ctypes attribute lookup raises "function not found" on that forwarder,
        # which used to kill the bridge and drop the QEMU clipboard pipe.
        cached = getattr(ClipboardBridge, '_kernel32', None)
        if cached is not None:
            return cached
        pointer, size = ctypes.c_void_p, ctypes.c_size_t
        cached = SimpleNamespace(
            GlobalAlloc=ClipboardBridge.win32('GlobalAlloc', pointer, ctypes.c_uint, size),
            GlobalLock=ClipboardBridge.win32('GlobalLock', pointer, pointer),
            GlobalUnlock=ClipboardBridge.win32('GlobalUnlock', ctypes.c_int, pointer),
            GlobalSize=ClipboardBridge.win32('GlobalSize', size, pointer),
            GlobalFree=ClipboardBridge.win32('GlobalFree', pointer, pointer),
        )
        ClipboardBridge._kernel32 = cached
        return cached

    def get_text(self):
        user32 = self.user32()
        if not user32.OpenClipboard(None):
            return ''
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ''
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ''
            kernel32 = self.kernel32()
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ''
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def set_text(self, text):
        user32 = self.user32()
        kernel32 = self.kernel32()
        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
            encoded = (text + '\0').encode('utf-16-le')
            handle = kernel32.GlobalAlloc(0x0042, len(encoded))
            if not handle:
                return
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return
            ctypes.memmove(pointer, encoded, len(encoded))
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(CF_UNICODETEXT, handle)
        finally:
            user32.CloseClipboard()

    def png_format(self):
        user32 = self.user32()
        if not hasattr(self, '_png_format'):
            user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
            user32.RegisterClipboardFormatW.restype = ctypes.c_uint
            self._png_format = user32.RegisterClipboardFormatW('PNG')
        return self._png_format

    def read_format(self, fmt):
        user32 = self.user32()
        kernel32 = self.kernel32()
        if not user32.IsClipboardFormatAvailable(fmt):
            return None
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            size = kernel32.GlobalSize(handle)
            return ctypes.string_at(pointer, size)
        finally:
            kernel32.GlobalUnlock(handle)

    def write_format(self, fmt, payload):
        user32 = self.user32()
        kernel32 = self.kernel32()
        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(0x0042, len(payload))
            if not handle:
                return
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return
            ctypes.memmove(pointer, payload, len(payload))
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(fmt, handle)
        finally:
            user32.CloseClipboard()

    def get_png(self):
        user32 = self.user32()
        if not user32.OpenClipboard(None):
            return None
        try:
            png = self.read_format(self.png_format())
            if png and png.startswith(b'\x89PNG'):
                return png
            for fmt in (CF_DIB, CF_DIBV5):
                dib = self.read_format(fmt)
                if dib:
                    converted = dib_to_png(dib)
                    if converted:
                        return converted
        finally:
            user32.CloseClipboard()
        return None

    def set_png(self, png):
        user32 = self.user32()
        kernel32 = self.kernel32()
        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
            self._give(user32, kernel32, self.png_format(), png)
            try:
                dib = png_to_dib(png)
            except ValueError:
                dib = None
            if dib:
                self._give(user32, kernel32, CF_DIB, dib)
        finally:
            user32.CloseClipboard()

    @staticmethod
    def _give(user32, kernel32, fmt, payload):
        handle = kernel32.GlobalAlloc(0x0042, len(payload))
        if not handle:
            return
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return
        ctypes.memmove(pointer, payload, len(payload))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)
