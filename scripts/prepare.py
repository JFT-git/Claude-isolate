#!/usr/bin/env python3
"""Download Ubuntu with pinned signing-key verification, then create a fresh guest."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import environment

FINGERPRINT = 'D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81'
BASE = 'https://cloud-images.ubuntu.com/noble/current/'


def fetch(url, target):
    if not url.startswith('https://'):
        raise ValueError('HTTPS required')
    with urllib.request.urlopen(url, timeout=60) as response, target.open('wb') as out:  # nosec B310 # Fixed HTTPS vendor URLs; schemes checked before/after redirect.
        if not response.url.startswith('https://'):
            raise ValueError('Insecure redirect')
        shutil.copyfileobj(response, out, 1024 * 1024)


def prepare(config_path):
    cfg = environment.load_config(config_path)
    disk = environment.local_path(cfg['disk'])
    if disk.exists() or environment.local_path(cfg['seed']).exists():
        raise RuntimeError('Existing VM will not be overwritten')
    filename = 'noble-server-cloudimg-' + ('arm64' if cfg['arch'] == 'aarch64' else 'amd64') + '.img'
    download = disk.parent / 'downloads'
    download.mkdir(parents=True, exist_ok=True)
    home = download / 'verification'
    home.mkdir(mode=0o700, exist_ok=True)
    key = download / 'ubuntu-key.asc'
    fetch('https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x' + FINGERPRINT, key)
    sums, signature = download / 'SHA256SUMS', download / 'SHA256SUMS.gpg'
    fetch(BASE + sums.name, sums)
    fetch(BASE + signature.name, signature)
    common = [environment.tool('gpg'), '--homedir', str(home), '--batch', '--no-autostart']
    subprocess.run(common + ['--import', str(key)], check=True, capture_output=True)
    verified = subprocess.run(common + ['--status-fd', '1', '--verify', str(signature), str(sums)],
                              check=True, capture_output=True, text=True)
    if not any(line.startswith('[GNUPG:] VALIDSIG ' + FINGERPRINT + ' ') for line in verified.stdout.splitlines()):
        raise RuntimeError('Ubuntu signing key mismatch')
    digest = next(line.split()[0] for line in sums.read_text().splitlines()
                  if line.split()[-1].lstrip('*') == filename)
    image = download / filename
    fetch(BASE + filename, image)
    environment.prepare(cfg, image, digest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'environment.json')
    prepare(parser.parse_args().config)
