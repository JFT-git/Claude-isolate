"""OpenSSH ProxyCommand over the VM's private virtio serial port."""
import os
import threading
import time

path = '/dev/virtio-ports/claude.gateway'
for _ in range(100):
    try:
        device = os.open(path, os.O_RDWR)
        break
    except FileNotFoundError:
        time.sleep(.1)
else:
    raise SystemExit('Private gateway device not available')


def copy(source, target):
    try:
        while True:
            chunk = os.read(source, 32768)
            if not chunk:
                break
            while chunk:
                chunk = chunk[os.write(target, chunk):]
    finally:
        os._exit(0)


threading.Thread(target=copy, args=(0, device), daemon=True).start()
copy(device, 1)
