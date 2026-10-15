#!/usr/bin/env python3
"""Guest clipboard bridge over virtio-serial claude.clipboard."""
import hashlib
import importlib.util
import os
import select
import signal
import subprocess
import sys
import time

PORT = '/dev/virtio-ports/claude.clipboard'
FRAMING = '/usr/local/lib/claude-isolate/clipboard-framing.py'
# The desktop session is the claude user on :0. A root service's xclip can
# set the selection, but xfce will not answer a root xclip -o, so guest-to-host
# never fires. Every xclip call is the session user on that display.
DISPLAY = ':0'
XAUTHORITY = '/home/claude/.Xauthority'
# xclip stays alive to own the X selection. The previous process is reaped
# after a newer one replaces it; it is not killed while it still owns the paste.
_selection_owner = None


def load_framing():
    spec = importlib.util.spec_from_file_location('clipboard_framing', FRAMING)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def xclip_command(args):
    command = ['env', 'DISPLAY=' + DISPLAY, 'XAUTHORITY=' + XAUTHORITY,
               'xclip', '-selection', 'clipboard', *args]
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        command = ['runuser', '-u', 'claude', '--', *command]
    return command


def stop_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def xclip(args, data=None):
    global _selection_owner
    command = xclip_command(args)
    try:
        if data is None:
            # xclip -o -t image/png waits forever when the selection has no PNG.
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                output, _ = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                stop_group(proc)
                return None
            if proc.returncode != 0:
                return None
            return output
        # Waiting for xclip to exit froze this loop after the first paste:
        # it does not exit until another client takes the selection.
        # Drop the previous owner first so a text selection cannot keep
        # covering a newer image (or the reverse).
        previous = _selection_owner
        _selection_owner = None
        if previous is not None and previous.poll() is None:
            stop_group(previous)
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        proc.stdin.write(data)
        proc.stdin.close()
        try:
            proc.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            previous = _selection_owner
            _selection_owner = proc
            if previous is not None:
                previous.poll()
            return None
        if proc.returncode not in (0, None):
            return None
        if _selection_owner is not None:
            _selection_owner.poll()
        _selection_owner = None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError('clipboard port closed')
        view = view[written:]


class SelectionWatch:
    """Wake up when the CLIPBOARD owner changes (XFixes) instead of polling.

    Polling ran runuser, env and xclip twice a second. Under software
    emulation each process start costs the guest noticeable CPU even while
    nobody copies anything, and the desktop competes with it for every key.
    """

    def __init__(self):
        self.x11 = self.display = self.fd = None

    def connect(self):
        try:
            import ctypes
            x11 = ctypes.CDLL('libX11.so.6')
            xfixes = ctypes.CDLL('libXfixes.so.3')
        except OSError:
            return False
        pointer, ulong, cint = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = pointer
        x11.XDefaultRootWindow.argtypes = [pointer]
        x11.XDefaultRootWindow.restype = ulong
        x11.XInternAtom.argtypes = [pointer, ctypes.c_char_p, cint]
        x11.XInternAtom.restype = ulong
        x11.XConnectionNumber.argtypes = [pointer]
        x11.XPending.argtypes = [pointer]
        x11.XNextEvent.argtypes = [pointer, pointer]
        x11.XFlush.argtypes = [pointer]
        x11.XCloseDisplay.argtypes = [pointer]
        xfixes.XFixesQueryExtension.argtypes = [pointer, ctypes.POINTER(cint), ctypes.POINTER(cint)]
        xfixes.XFixesSelectSelectionInput.argtypes = [pointer, ulong, ulong, ulong]
        # Root may read the session user's cookie; xclip runs as that user.
        os.environ['XAUTHORITY'] = XAUTHORITY
        display = x11.XOpenDisplay(DISPLAY.encode())
        if not display:
            return False
        event_base, error_base = cint(), cint()
        if not xfixes.XFixesQueryExtension(display, ctypes.byref(event_base), ctypes.byref(error_base)):
            x11.XCloseDisplay(display)
            return False
        clipboard = x11.XInternAtom(display, b'CLIPBOARD', 0)
        # XFixesSetSelectionOwnerNotifyMask
        xfixes.XFixesSelectSelectionInput(display, x11.XDefaultRootWindow(display), clipboard, 1)
        x11.XFlush(display)
        self.event = ctypes.create_string_buffer(256)
        self.x11, self.display, self.fd = x11, display, x11.XConnectionNumber(display)
        return True

    def changed(self):
        """Drain queued X events; True if any arrived. A lost X server exits
        the process through Xlib, and systemd restarts the bridge."""
        seen = False
        while self.x11.XPending(self.display):
            self.x11.XNextEvent(self.display, self.event)
            seen = True
        return seen


# Without XFixes (no session yet, or the library is missing) poll slowly.
FALLBACK_POLL = 2.0
RECONNECT = 5.0


def main():
    while not os.path.exists(PORT):
        time.sleep(1)
    framing = load_framing()
    fd = os.open(PORT, os.O_RDWR)
    last_text = last_png = ''
    carry = b''
    watch = SelectionWatch()
    retry_at = 0.0
    check = True
    try:
        while True:
            if watch.display is None and time.monotonic() >= retry_at:
                if watch.connect():
                    check = True  # Nothing observed while disconnected.
                else:
                    retry_at = time.monotonic() + RECONNECT
            if watch.display is not None and watch.changed():
                check = True
            descriptors = [fd] + ([watch.fd] if watch.display is not None else [])
            if check:
                timeout = 0
            elif watch.display is not None:
                timeout = None
            else:
                timeout = FALLBACK_POLL
            ready, _, _ = select.select(descriptors, [], [], timeout)
            if watch.display is None and not ready:
                check = True
            if watch.display is not None and watch.fd in ready and watch.changed():
                check = True
            if fd in ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                messages, carry = framing.parse(carry, chunk)
                for kind, payload in messages:
                    if kind == framing.TEXT:
                        xclip([], payload.encode('utf-8'))
                        last_text = hashlib.sha256(payload.encode('utf-8')).hexdigest()
                    else:
                        png = payload
                        marker = png.find(b'IEND')
                        if png.startswith(b'\x89PNG') and marker >= 0:
                            png = png[:marker + 8]
                        xclip(['-t', 'image/png'], png)
                        last_png = hashlib.sha256(png).hexdigest()
                        # Clipboards often keep serving the previous text target.
                        # Remember it so the next read does not paste that text
                        # back over the image we just applied.
                        echoed = xclip(['-o'])
                        if echoed:
                            stale = echoed.decode('utf-8', errors='replace')
                            if stale.endswith('\n'):
                                stale = stale[:-1]
                            if stale.endswith('\r'):
                                stale = stale[:-1]
                            if stale:
                                last_text = hashlib.sha256(stale.encode('utf-8')).hexdigest()
            if not check:
                continue
            check = False
            text_out = xclip(['-o'])
            text = ''
            if text_out is not None:
                text = text_out.decode('utf-8', errors='replace')
                if text.endswith('\n'):
                    text = text[:-1]
                if text.endswith('\r'):
                    text = text[:-1]
            # A text selection is the clipboard. xclip can also answer image/png
            # for that same selection; pushing both makes the host replace the
            # text with only the image, so guest-to-host text never sticks.
            if text:
                digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
                if digest != last_text:
                    last_text = digest
                    write_all(fd, framing.pack_text(text))
            else:
                png_out = xclip(['-o', '-t', 'image/png'])
                if png_out and png_out.startswith(b'\x89PNG'):
                    digest = hashlib.sha256(png_out).hexdigest()
                    if digest != last_png:
                        last_png = digest
                        write_all(fd, framing.pack_png(png_out))
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        main()
    except OSError:
        sys.exit(0)
