#!/usr/bin/env python3
"""CI: preinstall signed packages in a fresh signed Ubuntu image, then compress it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ubuntu_image


def build(arch, release, output):
    native = 'aarch64' if platform.machine().lower() in ('aarch64', 'arm64') else 'x86_64'
    if arch != native or not re.fullmatch(r'v\d+\.\d+\.\d+', release):
        raise ValueError('Build on native Linux runner with a versioned release tag')
    output.mkdir(parents=True, exist_ok=True)
    base, _ = ubuntu_image.download(output / 'vendor', arch, 'gpg')
    env = dict(os.environ, LIBGUESTFS_BACKEND='direct')
    def run(args):
        subprocess.run(args, check=True, env=env)
    filename = 'Claude-isolate-guest-' + arch + '.qcow2'
    destination = output / filename
    if destination.exists():
        raise RuntimeError('Refusing to overwrite image')
    with tempfile.TemporaryDirectory(prefix='image-build-', dir=output) as temporary:
        packages = Path(temporary) / 'claude-debs'
        packages.mkdir()
        # Download signed APT packages outside the appliance. An empty status
        # inventory makes APT fetch the entire dependency closure, even packages
        # already installed in the container. No account or CI token is mounted.
        run(['docker', 'run', '--rm', '-v', str(packages.resolve()) + ':/out',
             '-v', str(ROOT / 'guest') + ':/input:ro', 'ubuntu:24.04', 'sh', '-ec',
             'export DEBIAN_FRONTEND=noninteractive; '
             'rm -f /etc/apt/apt.conf.d/docker-clean; '
             'apt-get update; apt-get install -y --no-install-recommends curl gnupg ca-certificates; '
             'sh /input/repositories.sh; apt-get update; '
             'rm -f /var/cache/apt/archives/*.deb; '
             'xargs -r apt-get -o Dir::State::status=/dev/null --download-only '
             'install -y --no-install-recommends < /input/packages.txt; '
             'cp /var/cache/apt/archives/*.deb /out/; '
             'mkdir /out/repository-config; '
             'cp --parents /usr/share/keyrings/claude-desktop-archive-keyring.asc '
             '/etc/apt/keyrings/packages.mozilla.org.asc '
             '/etc/apt/sources.list.d/claude-desktop.list '
             '/etc/apt/sources.list.d/mozilla.list '
             '/etc/apt/preferences.d/mozilla /out/repository-config/'])
        disk = Path(temporary) / 'install.qcow2'
        run(['qemu-img', 'create', '-f', 'qcow2', str(disk), '12G'])
        run(['virt-resize', '--format', 'qcow2', '--output-format', 'qcow2', '--expand', '/dev/sda1', str(base), str(disk)])
        run(['virt-customize', '--format', 'qcow2', '-a', str(disk), '--memsize', '4096', '--smp', '2', '--no-network',
             '--copy-in', str(packages) + ':/tmp',
             '--upload', str(ROOT / 'guest/repositories.sh') + ':/tmp/claude-repositories.sh',
             '--upload', str(ROOT / 'guest/packages.txt') + ':/tmp/claude-packages.txt',
             '--run', str(ROOT / 'guest/preinstall.sh')])
        run(['virt-sysprep', '--format', 'qcow2', '-a', str(disk), '--operations',
             'machine-id,ssh-hostkeys,logfiles,tmp-files,dhcp-client-state,bash-history'])
        run(['virt-sparsify', '--in-place', '--format', 'qcow2', str(disk)])
        run(['qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', '-c', str(disk), str(destination)])
    run(['qemu-img', 'check', '-f', 'qcow2', str(destination)])
    size = destination.stat().st_size
    if size >= 2 * 1024**3:
        raise RuntimeError('Image exceeds GitHub per-asset limit; do not publish')
    with destination.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    manifest = dict(schema=1, arch=arch, release=release, filename=filename, size=size, sha256=digest)
    (output / ('guest-image-' + arch + '.json')).write_text(json.dumps(manifest, indent=2) + '\n')
    destination.with_suffix('.qcow2.sha256').write_text(digest + '  ' + filename + '\n')
    print(json.dumps(manifest), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=['x86_64', 'aarch64'], required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/guest')
    args = parser.parse_args()
    build(args.arch, args.release, args.output)
