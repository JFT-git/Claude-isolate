"""Saved memory of a stopped guest for a fast start; usable exactly once.

The guest's RAM (including signed-in sessions) is stored as an internal
snapshot of the private disk image, which already holds that data. QEMU's
file: migration cannot be used on Windows, and an internal snapshot needs no
network or pipe endpoint.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time

from network_guard import write_state

TAG = 'claude-fast-start'
NODE = 'claude-disk'


def metadata(cfg):
    return Path(cfg['disk']).parent / 'state.json'


def stable_command(command):
    """The QEMU arguments that define the guest's hardware.

    The paused-start flag and the display backend are not part of the saved
    machine, so a state saved by a hidden first-run boot restores in a window.
    """
    result, skip = [], False
    for item in command:
        if skip:
            skip = False
        elif item == '-display':
            skip = True
        elif item != '-S':
            result.append(item)
    return result


_DRIVE_FILE = re.compile(r'file=(?:[^,]|,,)*,?')
_HOST_FORWARD = re.compile(r',hostfwd=[^,]*')


def hardware_command(command):
    """stable_command without host-side details that do not reach the guest.

    The drive letter QEMU opens files through, the installation path and the
    localhost SSH port can change between launches. The saved machine does
    not depend on them, so they must not discard a fast start.
    """
    items = stable_command(command)
    result = []
    for index, item in enumerate(items):
        flag = items[index - 1] if index else None
        if index == 0:
            item = 'qemu'
        elif flag == '-drive':
            item = _DRIVE_FILE.sub('', item)
        elif flag == '-netdev':
            item = _HOST_FORWARD.sub('', item)
        elif flag in ('-L', '-serial'):
            item = flag
        result.append(item)
    return result


def qemu_version(cfg):
    """The bundled QEMU's VERSION file; a new QEMU may not load an old state."""
    if not cfg.get('qemu_executable'):
        return None
    try:
        return (Path(cfg['qemu_executable']).parent / 'VERSION').read_text(encoding='utf-8').strip()
    except (OSError, ValueError):
        return None


def identity(cfg, command):
    # Restoring requires the same devices and memory layout; any change of
    # resources, accelerator, QEMU version or guest revision invalidates it.
    return hashlib.sha256(json.dumps(dict(command=hardware_command(command), revision=cfg.get('guest_revision'),
                                          qemu=qemu_version(cfg)),
                                     sort_keys=True).encode()).hexdigest()


def legacy_identity(cfg, command):
    """Identity written by 1.0.6 and older, so their saved states still load."""
    return hashlib.sha256(json.dumps(dict(command=stable_command(command), revision=cfg.get('guest_revision')),
                                     sort_keys=True).encode()).hexdigest()


def disk_stamp(cfg):
    status = Path(cfg['disk']).stat()
    return [status.st_size, status.st_mtime_ns]


def image_tool(cfg):
    if cfg.get('qemu_executable'):
        return str(Path(cfg['qemu_executable']).with_name('qemu-img.exe' if os.name == 'nt' else 'qemu-img'))
    return 'qemu-img'


def discard(cfg, delete_snapshot=True):
    """Forget the saved state; remove its snapshot unless QEMU holds the disk."""
    metadata(cfg).unlink(missing_ok=True)
    if delete_snapshot and Path(cfg['disk']).is_file():
        try:
            # Fails harmlessly if there is no such snapshot or QEMU is running.
            disk = cfg['disk']
            if platform.system() == 'Windows':
                from windows.qemu_paths import open_for_qemu
                disk = open_for_qemu(disk, cfg)
            subprocess.run([image_tool(cfg), 'snapshot', '-d', TAG, str(disk)], capture_output=True,
                           timeout=300, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError):
            pass


def record(cfg, command):
    """Publish a completed save once QEMU has exited and closed the disk."""
    write_state(metadata(cfg), dict(identity=identity(cfg, command), disk=disk_stamp(cfg), saved=time.time()))


def exists(cfg):
    return metadata(cfg).is_file()


def usable(cfg, command):
    """True if the saved state belongs to this exact VM; otherwise remove it."""
    if not exists(cfg):
        # A save without metadata is incomplete; the next save replaces it.
        return False
    try:
        data = json.loads(metadata(cfg).read_text(encoding='utf-8'))
        valid = (isinstance(data, dict)
                 and data.get('identity') in (identity(cfg, command), legacy_identity(cfg, command))
                 and data.get('disk') == disk_stamp(cfg))
    except (OSError, ValueError, TypeError):
        valid = False
    if not valid:
        discard(cfg)
    return valid
