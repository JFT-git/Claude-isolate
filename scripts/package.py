#!/usr/bin/env python3
"""Package only tracked source files; never sweep up a user's VM or credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def package(target, output):
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'Claude-isolate-{target}.zip'
    files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    if not any(files):
        raise RuntimeError('Stage source files before packaging')
    forbidden = {'.qcow2', '.img', '.iso', '.sock', '.pem', '.key', '.pyc', '.log'}
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in files:
            if not name:
                continue
            path = ROOT / name
            if path.is_symlink() or path.suffix in forbidden or path.name in ('environment.json', '.env'):
                raise RuntimeError(f'Refusing to publish potentially private file: {name}')
            z.write(path, 'Claude-isolate/' + name)
        z.writestr('Claude-isolate/BUILD.json', json.dumps({
            'target': target, 'python': platform.python_version(),
            'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
            'distribution': 'source CLI; Windows is a development preview, not a verified VM runtime',
        }, indent=2))
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n')
    print(archive)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', required=True, choices=['linux-x64', 'linux-arm64', 'windows-x64-preview'])
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    package(args.target, args.output)
