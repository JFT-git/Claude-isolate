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

import ubuntu_image
import release_image


def prepare(config_path, if_needed=False):
    cfg = environment.load_config(config_path)
    disk = environment.local_path(cfg['disk'])
    if if_needed and disk.is_file() and environment.local_path(cfg['seed']).is_file():
        return
    if disk.exists() or environment.local_path(cfg['seed']).exists():
        raise RuntimeError('Existing VM will not be overwritten')
    image, digest = release_image.download(disk.parent / 'downloads', cfg['arch'])
    environment.prepare(cfg, image, digest)
    image.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'environment.json')
    parser.add_argument('--if-needed', action='store_true')
    args = parser.parse_args()
    prepare(args.config, args.if_needed)
