#!/usr/bin/env python3
"""Build, install, and smoke-test the Windows GUI and background executable."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'dist'
sys.path.insert(0, str(ROOT))
from windows import runtime


def run(command, **kwargs):
    return subprocess.run(command, check=True, cwd=ROOT, **kwargs)


def checksum(path):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    path.with_suffix(path.suffix + '.sha256').write_text(f'{digest}  {path.name}\n', encoding='ascii')


def smoke(folder, directory):
    # Removing Python from PATH catches accidental dependence on the CI setup.
    env = dict(os.environ, PATH=str(Path(os.environ['SystemRoot']) / 'System32'))
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONUTF8', None)
    core = folder / 'Claude Isolate Core.exe'
    if not (folder / '_internal/certifi/cacert.pem').is_file():
        raise RuntimeError('Installer is missing bundled TLS trust roots')
    result = run([str(core), '--version'], capture_output=True, text=True, env=env, timeout=30)
    if 'Claude Isolate 0.3.12' not in result.stdout:
        raise RuntimeError('Worker version smoke test failed')
    for executable in ('qemu/qemu-system-x86_64.exe', 'qemu/qemu-img.exe', 'GnuPG/bin/gpg.exe'):
        run([str(folder / 'runtime' / executable), '--version'], capture_output=True, env=env, timeout=30)
    report = directory / 'gui.json'
    data = directory / 'private data Тест'
    run([str(folder / 'Claude Isolate.exe'), '--data', str(data), '--smoke-test', str(report)], env=env, timeout=60)
    if json.loads(report.read_text(encoding='utf-8')).get('gui') is not True:
        raise RuntimeError('GUI could not initialize on a clean Windows process')
    plan = json.loads(run([str(core), 'plan', '--config', str(data / 'environment.json')],
                         capture_output=True, text=True, env=env, timeout=30).stdout)
    net = plan[plan.index('-netdev') + 1]
    if 'guestfwd=' in net or 'restrict=on' not in net or 'virtserialport,chardev=gateway,name=claude.gateway' not in plan:
        raise RuntimeError('Frozen relay or QEMU network plan is incorrect')
    for host, expected in [('127.0.0.1', b'HTTP/1.1 403'), ('claude.ai', b'HTTP/1.1 503')]:
        response = run([str(core), 'relay', '--mode', 'system', '--web-access', 'public'],
                       input=f'CONNECT {host}:443 HTTP/1.1\r\n\r\n'.encode(),
                       capture_output=True, env=env, timeout=30)
        if not response.stdout.startswith(expected):
            raise RuntimeError('Packaged gateway did not fail closed: ' + host)
    return data


def licenses(folder):
    target = folder / 'licenses'
    target.mkdir()
    for package in ('pycdlib', 'pyinstaller', 'paramiko', 'cryptography', 'bcrypt', 'pynacl', 'cffi', 'invoke', 'certifi'):
        distribution = importlib.metadata.distribution(package)
        for path in distribution.files or []:
            if 'license' in Path(path).name.lower() and path.locate().is_file():
                shutil.copy2(path.locate(), target / (package + '-' + Path(path).name))
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copy2(python_license, target / 'Python-LICENSE.txt')


def main():
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Build on Windows x64 using 64-bit Python 3.12.')
    run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
         '--distpath', str(DIST / 'windows'), '--workpath', str(ROOT / 'build/windows'),
         str(ROOT / 'windows/app.spec')])
    folder = DIST / 'windows' / 'Claude Isolate'
    shutil.copy2(ROOT / 'windows/README.txt', folder / 'README.txt')
    licenses(folder)
    manifest = runtime.bundle(folder)
    print('Bundled runtime:', len(manifest['files']), 'files', flush=True)
    (folder / 'BUILD.json').write_text(json.dumps({
        'version': '0.3.12', 'target': 'windows-x64', 'kind': 'gui-and-worker-executables',
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
    }, indent=2), encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='Claude Isolate smoke ') as temporary:
        smoke(folder, Path(temporary))
    compiler = shutil.which('ISCC') or str(Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Inno Setup 6/ISCC.exe')
    run([compiler, str(ROOT / 'windows/setup.iss')])
    installer = DIST / 'Claude-isolate-windows-x64-setup.exe'
    with tempfile.TemporaryDirectory(prefix='Claude Isolate installer ') as temporary:
        directory = Path(temporary)
        installed = directory / 'Clean installation Тест'
        run([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
             '/DIR=' + str(installed), '/TASKS='], timeout=120)
        data = smoke(installed, directory)
        run([str(installed / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'], timeout=120)
        if not (data / 'environment.json').exists():
            raise RuntimeError('Uninstaller removed VM data outside the application folder')
    archive = DIST / 'Claude-isolate-windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(folder.parent))
    for path in (installer, archive):
        checksum(path)
        print('Built and verified:', path)


if __name__ == '__main__':
    main()
