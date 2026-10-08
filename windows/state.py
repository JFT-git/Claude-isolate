"""Saved memory of a stopped guest for a fast start; usable exactly once.

The file holds the guest's RAM, including signed-in sessions. It stays in
the private environment folder next to the disk, which already holds them.
"""
import hashlib
import json
import os
from pathlib import Path
import time

from network_guard import write_state


def paths(cfg):
    directory = Path(cfg['disk']).parent
    return directory / 'state.bin', directory / 'state.json'


def supported(cfg):
    # QEMU's file: URI treats a comma as an option separator.
    return ',' not in str(paths(cfg)[0])


def uri(cfg):
    return 'file:' + str(paths(cfg)[0])


def identity(cfg, command):
    # Restoring requires the same QEMU, devices and memory layout; any
    # change of resources, accelerator or installation invalidates it.
    stable = [item for item in command if item != 'defer' and item != '-incoming']
    return hashlib.sha256(json.dumps(dict(command=stable, revision=cfg.get('guest_revision')),
                                     sort_keys=True).encode()).hexdigest()


def disk_stamp(cfg):
    status = Path(cfg['disk']).stat()
    return [status.st_size, status.st_mtime_ns]


def discard(cfg):
    for path in paths(cfg):
        path.unlink(missing_ok=True)


def record(cfg, command):
    """Publish a completed save once QEMU has exited and closed the disk."""
    state, metadata = paths(cfg)
    if not state.is_file():
        discard(cfg)
        return False
    write_state(metadata, dict(identity=identity(cfg, command), disk=disk_stamp(cfg),
                               size=state.stat().st_size, saved=time.time()))
    if os.name != 'nt':
        state.chmod(0o600)
    return True


def usable(cfg, command):
    """True if the saved state belongs to this exact VM; otherwise remove it."""
    state, metadata = paths(cfg)
    try:
        data = json.loads(metadata.read_text(encoding='utf-8'))
        valid = (supported(cfg) and state.is_file() and isinstance(data, dict)
                 and data.get('identity') == identity(cfg, command)
                 and data.get('disk') == disk_stamp(cfg)
                 and data.get('size') == state.stat().st_size)
    except (OSError, ValueError, TypeError):
        valid = False
    if not valid:
        discard(cfg)
    return valid
