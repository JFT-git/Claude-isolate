#!/usr/bin/env python3
"""Build the native controller; dependencies and guest data are installed at first run."""
import argparse
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess


def build(output, arch, analysis_only=False):
    root = Path(__file__).resolve().parents[1]
    app = output / 'Claude Environment.app'
    contents = app / 'Contents'
    runtime = contents / 'Resources/runtime'
    (contents / 'MacOS').mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ['environment.py', 'network_guard.py', 'network_transport.py', 'tls_trust.py', 'route_guard.py', 'ubuntu_image.py', 'release_image.py', 'session_lock.py',
                 'environment.example.json', 'guest', 'macos']:
        source, target = root / name, runtime / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(source, target)
    for manifest in root.glob('guest-image-*.json'):
        shutil.copy2(manifest, runtime / manifest.name)
    if not analysis_only and not list(runtime.glob('guest-image-*.json')):
        raise RuntimeError('Missing CI prepared guest manifests')
    (contents / 'Info.plist').write_bytes(plistlib.dumps(dict(
        CFBundleExecutable='ClaudeEnvironment', CFBundleIdentifier='local.claude.environment',
        CFBundleName='Claude Environment', CFBundlePackageType='APPL',
        CFBundleShortVersionString='0.3.18', CFBundleVersion='18',
        LSMinimumSystemVersion='14.0', NSHighResolutionCapable=True)))
    cache = root / 'build/modulecache' / arch
    cache.mkdir(parents=True, exist_ok=True)
    subprocess.run(['swiftc', str(root / 'macos/main.swift'), '-o', str(contents / 'MacOS/ClaudeEnvironment'),
                    '-framework', 'AppKit', '-target', arch + '-apple-macos14.0',
                    '-module-cache-path', str(cache)], check=True)
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(app)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    print(app)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=['arm64', 'x86_64'], default=platform.machine())
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'dist')
    parser.add_argument('--analysis-only', action='store_true', help='Compile for SAST without distributable image metadata')
    args = parser.parse_args()
    build(args.output.resolve(), args.arch, args.analysis_only)
