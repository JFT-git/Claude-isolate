#!/usr/bin/env python3
"""Disposable Windows QEMU boot check of the packaged gateway, without accounts."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import environment
import network_guard
import ubuntu_image
from windows import backend
from windows.job import Job

QEMU_URL = 'https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811.exe'
QEMU_SHA512 = ('5bcf9eed634e8575a37b74f445af41a2fe4106da512d0c30c368301d4c105037f'
               'dfab40a5287367a28a957624cddebbc8c07e16c88ab6634f554cdf3d16bf543')
PROBE = '''import concurrent.futures, socket, ssl

def blocked_direct():
    try:
        with socket.create_connection(('1.1.1.1', 443), timeout=3):
            raise RuntimeError('Direct Internet connection unexpectedly succeeded')
    except OSError:
        print('WINDOWS-INTEGRATION: DIRECT-BLOCKED', flush=True)

def proxy(host):
    stream = socket.create_connection(('10.0.2.100', 7890), timeout=20)
    stream.sendall(('CONNECT ' + host + ':443 HTTP/1.1\\r\\n\\r\\n').encode())
    reply = bytearray()
    while b'\\r\\n\\r\\n' not in reply:
        chunk = stream.recv(1)
        if not chunk or len(reply) > 32768:
            raise RuntimeError('Invalid gateway response: ' + str(reply))
        reply.extend(chunk)
    return stream, bytes(reply)

def public_https(index):
    stream, reply = proxy('www.cloudflare.com')
    if not reply.startswith(b'HTTP/1.1 200'):
        raise RuntimeError('Gateway refused public HTTPS: ' + str(reply))
    with ssl.create_default_context().wrap_socket(stream, server_hostname='www.cloudflare.com') as tls:
        tls.sendall(b'GET /cdn-cgi/trace HTTP/1.1\\r\\nHost: www.cloudflare.com\\r\\nConnection: close\\r\\n\\r\\n')
        result = bytearray()
        while True:
            block = tls.recv(4096)
            if not block:
                break
            result.extend(block)
    if b'ip=' not in result or b'loc=' not in result:
        raise RuntimeError('Missing public HTTPS response')
    print('WINDOWS-INTEGRATION: PUBLIC-HTTPS-OK', index, flush=True)

blocked_direct()
stream, reply = proxy('127.0.0.1')
stream.close()
if not reply.startswith(b'HTTP/1.1 403'):
    raise RuntimeError('Gateway allowed a local destination')
print('WINDOWS-INTEGRATION: LOCAL-BLOCKED', flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(public_https, range(2)))
'''


def qemu(directory):
    executable = backend.find_tool('qemu-system-x86_64')
    if executable:
        return executable
    installer = directory / 'qemu-setup.exe'
    ubuntu_image.fetch(QEMU_URL, installer)
    with installer.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha512').hexdigest()
    if digest != QEMU_SHA512:
        raise RuntimeError('Pinned Windows QEMU installer checksum mismatch')
    destination = directory / 'qemu'
    subprocess.run([str(installer), '/S', '/D=' + str(destination)], check=True, timeout=180)
    installer.unlink()
    executable = destination / 'qemu-system-x86_64.exe'
    if not executable.is_file():
        executable = backend.find_tool('qemu-system-x86_64')
    if not executable:
        raise RuntimeError('Windows QEMU installation failed')
    return executable


def main():
    if os.name != 'nt':
        raise SystemExit('Run this integration test on Windows.')
    report = ROOT / 'reports/windows'
    report.mkdir(parents=True, exist_ok=True)
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='win-boot-', dir=build) as temporary:
        directory = Path(temporary)
        print('Installing checksum-verified Windows QEMU…', flush=True)
        executable = qemu(directory)
        os.environ['PATH'] = str(executable.parent) + os.pathsep + os.environ['PATH']
        (report / 'qemu-version.txt').write_bytes(subprocess.check_output([str(executable), '--version']))
        path, cfg = backend.config(directory / 'data')
        cfg.update(qemu_executable=str(executable), accelerator='tcg', display='none', memory_mb=2048)
        backend.write_config(path, cfg)
        core = ROOT / 'dist/windows/Claude Isolate/Claude Isolate Core.exe'
        # Use the actual packaged first-run setup, including GPG, qemu-img,
        # image download/signature checks and the bundled ISO writer.
        with (report / 'prepare.log').open('wb') as output:
            print('Preparing Ubuntu with the packaged executable…', flush=True)
            prepared = subprocess.run([str(core), 'prepare', '--data', str(path.parent)],
                                      stdout=output, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW, timeout=600)
        if prepared.returncode:
            print((report / 'prepare.log').read_text(encoding='utf-8', errors='replace')[-12000:])
            raise RuntimeError('Packaged first-run setup failed')
        cfg = environment.load_config(path)
        cloud = '#cloud-config\n' + json.dumps({
            'hostname': 'windows-isolation-test', 'ssh_pwauth': False,
            'write_files': [{'path': '/ci-probe.py', 'content': PROBE, 'permissions': '0600'}],
            'runcmd': [['python3', '/ci-probe.py'], ['systemctl', 'poweroff']],
        })
        # This guest is disposable and has never booted. Replace only its seed
        # with the account-free probe instead of installing the full desktop.
        seed_directory = directory / 'probe-seed'
        seed_directory.mkdir()
        (seed_directory / 'user-data').write_text(cloud, encoding='utf-8', newline='\n')
        (seed_directory / 'meta-data').write_text('instance-id: windows-gateway-ci\n', encoding='utf-8', newline='\n')
        prepared_seed = directory / 'probe.iso'
        backend.seed_iso(seed_directory, prepared_seed)
        os.replace(prepared_seed, cfg['seed'])
        print('Booting disposable Ubuntu and checking gateway…', flush=True)
        job = Job()
        process = None
        try:
            with (report / 'core.log').open('wb') as output:
                process = subprocess.Popen([
                    str(core),
                    'cli', 'start', '--config', str(path), '--start-gate'],
                    stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                job.assign(process)
                process.stdin.write(b'GO\n')
                process.stdin.flush()
                process.stdin.close()
                deadline = time.monotonic() + 60
                while True:
                    try:
                        control = backend.qmp(cfg, 'query-status')
                        (report / 'qmp.json').write_text(json.dumps(control), encoding='utf-8')
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError('Packaged launcher did not expose the Windows control pipe')
                        time.sleep(.5)
                if process.wait(timeout=420):
                    raise RuntimeError('Packaged launcher failed during guest boot')
        finally:
            network_guard.revoke(None, cfg['network_status'])
            job.close()
            if process and process.poll() is None:
                process.wait(timeout=10)
            if Path(cfg['boot_log']).is_file():
                shutil.copy2(cfg['boot_log'], report / 'boot.log')
        boot = (report / 'boot.log').read_text(encoding='utf-8', errors='replace')
        for marker in ('DIRECT-BLOCKED', 'LOCAL-BLOCKED', 'PUBLIC-HTTPS-OK'):
            if 'WINDOWS-INTEGRATION: ' + marker not in boot:
                lines = boot.splitlines()
                for index, line in enumerate(lines):
                    if any(word in line for word in ('WINDOWS-INTEGRATION:', 'Traceback', 'Error:', 'ci-probe.py')):
                        print('\n'.join(lines[max(0, index - 2):index + 12]))
                print((report / 'core.log').read_text(encoding='utf-8', errors='replace')[-12000:])
                raise RuntimeError('Guest network check failed: ' + marker)
        result = {'windows_qemu_boot': True, 'packaged_gateway': True,
                  'direct_internet_blocked': True, 'local_targets_blocked': True,
                  'public_https_connections': boot.count('WINDOWS-INTEGRATION: PUBLIC-HTTPS-OK'),
                  'seconds': round(time.monotonic() - started, 1)}
        (report / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
