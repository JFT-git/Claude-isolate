#!/usr/bin/env python3
"""Guest clipboard bridge over virtio-serial claude.clipboard."""
import hashlib
import importlib.util
import os
import select
import subprocess
import sys
import time

PORT = '/dev/virtio-ports/claude-clipboard'
FRAMING = '/usr/local/lib/claude-isolate/clipboard-framing.py'


def load_framing():
    spec = importlib.util.spec_from_file_location('clipboard_framing', FRAMING)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def xclip(args, data=None):
    command = ['xclip', '-selection', 'clipboard'] + args
    try:
        if data is None:
            return subprocess.check_output(command, stderr=subprocess.DEVNULL)
        subprocess.run(command, input=data, check=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError('clipboard port closed')
        view = view[written:]


def main():
    while not os.path.exists(PORT):
        time.sleep(1)
    framing = load_framing()
    os.environ.setdefault('DISPLAY', ':0')
    os.environ.setdefault('XAUTHORITY', '/home/claude/.Xauthority')
    fd = os.open(PORT, os.O_RDWR)
    last_text = last_png = ''
    carry = b''
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                messages, carry = framing.parse(carry, chunk)
                for kind, payload in messages:
                    if kind == framing.TEXT:
                        xclip([], payload.encode('utf-8'))
                        last_text = hashlib.sha256(payload.encode('utf-8')).hexdigest()
                    else:
                        xclip(['-t', 'image/png'], payload)
                        last_png = hashlib.sha256(payload).hexdigest()
            text_out = xclip(['-o'])
            if text_out is not None:
                text = text_out.decode('utf-8', errors='replace')
                digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
                if text and digest != last_text:
                    last_text = digest
                    write_all(fd, framing.pack_text(text))
            png_out = xclip(['-o', '-t', 'image/png'])
            if png_out:
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
