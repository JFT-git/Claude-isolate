"""OpenSSH ProxyCommand over the VM's private virtio serial port."""
import os
import select
import threading
import time
import uuid

PATH = '/dev/virtio-ports/claude.gateway'
SYNC = b'\x00CLAUDE-SYNC-'
ACK = b'\x00CLAUDE-ACK-'
# Sent by a newly started host gateway, e.g. after a saved VM was restored:
# the SSH session in the guest's memory no longer has a host peer.
RESET = b'\x00CLAUDE-RESET\n'


def write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def set_clock(milliseconds):
    # A restored VM resumes with the time it was saved at.
    target = milliseconds / 1000
    if abs(time.time() - target) > 2:
        try:
            time.clock_settime(time.CLOCK_REALTIME, target)
        except (OSError, PermissionError):
            pass


def handshake(device, deadline=None, clock=set_clock):
    """Start a new session on the shared serial stream.

    The port carries no session boundaries: bytes the host sent to a previous
    OpenSSH process may still be queued. Discard everything until the host
    acknowledges this process's nonce, and return bytes that follow the
    acknowledgement (the start of the host's SSH banner).
    """
    nonce = uuid.uuid4().hex.encode()
    ack = ACK + nonce
    buffer = b''
    resend = 0
    while deadline is None or time.monotonic() < deadline:
        # Read what has arrived before repeating the marker, so a slow guest
        # rarely repeats it after the host has already acknowledged.
        if not select.select([device], [], [], 0 if time.monotonic() >= resend else .2)[0]:
            if time.monotonic() >= resend:
                write_all(device, SYNC + nonce + b'\n')
                resend = time.monotonic() + 2
            continue
        chunk = os.read(device, 32768)
        if not chunk:
            time.sleep(.1)
            continue
        buffer += chunk
        index = buffer.find(ack)
        if index < 0:
            buffer = buffer[-(len(ack) + 32):]
            continue
        end = buffer.find(b'\n', index)
        if end < 0:
            continue
        fields = buffer[index + len(ack):end].split()
        if fields and fields[0].isdigit() and clock:
            clock(int(fields[0]))
        return buffer[end + 1:]
    raise TimeoutError('Private gateway did not acknowledge the session')


def copy(source, target, reset=False):
    tail = b''
    try:
        while True:
            chunk = os.read(source, 32768)
            if not chunk:
                break
            if reset:
                combined = tail + chunk
                if RESET in combined:
                    break
                tail = combined[-(len(RESET) - 1):]
            write_all(target, chunk)
    finally:
        os._exit(0)


def main():
    for _ in range(100):
        try:
            device = os.open(PATH, os.O_RDWR)
            break
        except FileNotFoundError:
            time.sleep(.1)
    else:
        raise SystemExit('Private gateway device not available')
    write_all(1, handshake(device))
    threading.Thread(target=copy, args=(0, device), daemon=True).start()
    copy(device, 1, reset=True)


if __name__ == '__main__':
    main()
