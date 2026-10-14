"""Shared folder, localhost SSH and clipboard for macOS and Linux hosts.

The guest side (sshd, /home/claude/Shared, virtio clipboard) is the same as on
Windows. These hosts reach it over localhost:2222 and a Unix socket chardev.
"""
import hashlib
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from windows.clipboard_framing import TEXT, PNG, pack_text, pack_png, parse


def prepare_access(cfg, data):
    """Create the host shared directory and Ed25519 key. Sets cfg['ssh_pubkey']."""
    data = Path(data)
    (data / 'shared').mkdir(parents=True, exist_ok=True)
    from windows.ssh_keys import ensure_client_keypair
    cfg['ssh_pubkey'] = ensure_client_keypair(data / 'tools' / 'ssh_host_key')
    return cfg['ssh_pubkey']


def clipboard_socket(cfg):
    qmp = cfg.get('qmp_socket')
    if qmp:
        return Path(qmp).expanduser().parent / 'clipboard.sock'
    return Path(cfg['disk']).expanduser().parent / 'clipboard.sock'


def _run(command, payload=None, timeout=2):
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def get_text():
    if sys.platform == 'darwin':
        data = _run(['pbpaste'])
        return data.decode('utf-8', errors='replace') if data else ''
    data = _run(['xclip', '-selection', 'clipboard', '-o'])
    return data.decode('utf-8', errors='replace') if data else ''


def set_text(text):
    command = ['pbcopy'] if sys.platform == 'darwin' else ['xclip', '-selection', 'clipboard']
    _run(command, text.encode('utf-8'))


def get_png():
    if sys.platform == 'darwin':
        data = _run(['osascript', '-l', 'JavaScript', '-e', _MAC_READ_PNG], timeout=3)
        if not data:
            return None
        import base64
        try:
            png = base64.b64decode(data.strip())
        except ValueError:
            return None
        return png if png.startswith(b'\x89PNG') else None
    data = _run(['xclip', '-selection', 'clipboard', '-t', 'image/png', '-o'], timeout=2)
    return data if data and data.startswith(b'\x89PNG') else None


def set_png(png):
    if sys.platform == 'darwin':
        import base64
        script = _MAC_WRITE_PNG.replace('__B64__', base64.b64encode(png).decode('ascii'))
        _run(['osascript', '-l', 'JavaScript', '-e', script], timeout=3)
        return
    _run(['xclip', '-selection', 'clipboard', '-t', 'image/png'], png, timeout=2)


_MAC_READ_PNG = r'''
ObjC.import('AppKit');
var data = $.NSPasteboard.generalPasteboard.dataForType('public.png');
data ? data.base64EncodedStringWithOptions(0).js : '';
'''

_MAC_WRITE_PNG = r'''
ObjC.import('AppKit');
var pb = $.NSPasteboard.generalPasteboard;
pb.clearContents;
var raw = $.NSData.alloc.initWithBase64EncodedStringOptions('__B64__', 0);
pb.setDataForType(raw, 'public.png');
'''


class UnixClipboard:
    """Bidirectional TEXT/PNG bridge over the QEMU clipboard Unix socket."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.path = clipboard_socket(cfg)
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
        sock = None
        deadline = time.monotonic() + 120
        while not self.stop.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(self.path))
                break
            except OSError:
                if sock:
                    sock.close()
                    sock = None
                if time.monotonic() >= deadline:
                    return
                self.stop.wait(.3)
        if not sock:
            return
        sock.settimeout(.3)
        carry = b''
        try:
            while not self.stop.is_set():
                try:
                    chunk = sock.recv(65536)
                    if chunk == b'':
                        break
                    messages, carry = parse(carry, chunk)
                    for kind, payload in messages:
                        if kind == TEXT:
                            set_text(payload)
                            self.last_text = hashlib.sha256(payload.encode('utf-8')).hexdigest()
                        else:
                            set_png(payload)
                            self.last_png = hashlib.sha256(payload).hexdigest()
                except socket.timeout:
                    pass
                except OSError:
                    break
                except Exception as error:
                    print('Clipboard bridge: ' + str(error), file=sys.stderr, flush=True)
                try:
                    text = get_text()
                    if text:
                        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
                        if digest != self.last_text:
                            self.last_text = digest
                            sock.sendall(pack_text(text))
                    png = get_png()
                    if png:
                        digest = hashlib.sha256(png).hexdigest()
                        if digest != self.last_png:
                            self.last_png = digest
                            sock.sendall(pack_png(png))
                except Exception as error:
                    print('Clipboard bridge: ' + str(error), file=sys.stderr, flush=True)
                    break
        finally:
            sock.close()
