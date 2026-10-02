#!/usr/bin/env python3
"""Create a fresh, platform-specific CLI config without overwriting user settings."""
import argparse
import json
from pathlib import Path
import platform
import shutil

ROOT = Path(__file__).resolve().parents[1]


def config(system, machine, directory, firmware=None):
    arm = machine.lower() in ('aarch64', 'arm64')
    result = dict(arch='aarch64' if arm else 'x86_64', network_mode='system',
                  web_access='public', memory_mb=8192, cpus=4,
                  disk=str(directory / 'desktop.qcow2'), seed=str(directory / 'seed.iso'))
    if arm:
        candidates = [firmware] if firmware else ([
            '/opt/homebrew/share/qemu/edk2-aarch64-code.fd'] if system == 'Darwin' else [
            '/usr/share/qemu-efi-aarch64/QEMU_EFI.fd', '/usr/share/AAVMF/AAVMF_CODE.fd'])
        found = next((p for p in candidates if p and Path(p).is_file()), None)
        if not found:
            raise RuntimeError('ARM firmware missing: install QEMU EFI firmware or pass --firmware')
        result['firmware'] = found
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT / 'state')
    parser.add_argument('--config', type=Path, default=ROOT / 'environment.json')
    parser.add_argument('--firmware')
    args = parser.parse_args()
    directory = args.data.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    result = config(platform.system(), platform.machine(), directory, args.firmware)
    with args.config.open('x') as output:
        json.dump(result, output, indent=2)
    print(args.config)
