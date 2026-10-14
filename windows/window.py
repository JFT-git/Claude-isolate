"""Open the QEMU window maximized on the screen Windows shows it on.

QEMU's GTK window first takes the size of the firmware's text mode. With
zoom-to-fit, QEMU never resizes it when Linux switches to 1920x1200, so the
desktop is drawn tiny and the window often extends past the screen edge.
A maximized window fits the work area (above the taskbar), and zoom-to-fit
scales the guest into it.
"""
import ctypes
from ctypes import wintypes
import time

SW_MAXIMIZE = 3
TITLE = 'QEMU'


def _user32():
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    return user32


def find_window(pid, user32=None):
    """The visible top-level QEMU window of process pid, or None."""
    user32 = user32 or _user32()
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value.startswith(TITLE):
                found.append(hwnd)
                return False
        return True

    callback = callback_type(visit)
    user32.EnumWindows(ctypes.cast(callback, ctypes.c_void_p), 0)
    return found[0] if found else None


def maximize_when_shown(pid, stop, timeout=180):
    """Maximize QEMU's window once it appears. Returns True if it did."""
    try:
        user32 = _user32()
    except (OSError, AttributeError):
        return False
    deadline = time.monotonic() + timeout
    while not stop.wait(.25) and time.monotonic() < deadline:
        hwnd = find_window(pid, user32)
        if hwnd:
            # Asynchronous: never wait on QEMU's GUI thread.
            user32.ShowWindowAsync(hwnd, SW_MAXIMIZE)
            return True
    return False
