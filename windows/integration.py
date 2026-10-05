#!/usr/bin/env python3
"""Disposable Windows QEMU boot check of the packaged gateway, without accounts."""
import hashlib
import argparse
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
from windows import gnupg
from windows.job import Job
from windows.serial_gateway import boot_command
from windows.control import paths as control_paths

QEMU_URL = 'https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811.exe'
QEMU_SHA512 = ('5bcf9eed634e8575a37b74f445af41a2fe4106da512d0c30c368301d4c105037f'
               'dfab40a5287367a28a957624cddebbc8c07e16c88ab6634f554cdf3d16bf543')
PROBE = '''import concurrent.futures, hashlib, http.client, socket, ssl, time

def blocked_direct():
    try:
        with socket.create_connection(('1.1.1.1', 443), timeout=3):
            raise RuntimeError('Direct Internet connection unexpectedly succeeded')
    except OSError:
        print('WINDOWS-INTEGRATION: DIRECT-BLOCKED', flush=True)

def proxy(host):
    deadline = time.monotonic() + 60
    while True:
        try:
            stream = socket.create_connection(('10.0.2.100', 7890), timeout=20)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.2)
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
        stream.close()
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
    return index

def public_pair():
    # A temporary probe outage intentionally closes active connections. Retry
    # the WHOLE concurrent pair, never count a partial/failed attempt as success.
    for attempt in range(3):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(public_https, range(2)))
            for index in results:
                print('WINDOWS-INTEGRATION: PUBLIC-HTTPS-OK', index, flush=True)
            return
        except (OSError, RuntimeError) as error:
            print('WINDOWS-INTEGRATION: HTTPS-ATTEMPT-FAILED', attempt + 1, str(error), flush=True)
            if attempt == 2:
                raise
            time.sleep(5)

def bulk_https():
    # Exercise many SSH packets and window updates; short trace responses did
    # not reveal Windows descriptor locks that stalled full-duplex downloads.
    for attempt in range(3):
        try:
            stream, reply = proxy('gnupg.org')
            if not reply.startswith(b'HTTP/1.1 200'):
                stream.close()
                raise RuntimeError('Gateway refused bulk HTTPS')
            with ssl.create_default_context().wrap_socket(stream, server_hostname='gnupg.org') as tls:
                tls.sendall(b'GET @BULK_PATH@ HTTP/1.1\\r\\nHost: gnupg.org\\r\\nConnection: close\\r\\n\\r\\n')
                response = http.client.HTTPResponse(tls)
                response.begin()
                if response.status != 200:
                    raise RuntimeError('Bulk HTTPS status: ' + str(response.status))
                digest, count = hashlib.sha256(), 0
                while block := response.read(65536):
                    count += len(block)
                    if count > 8 * 1024 * 1024:
                        raise RuntimeError('Oversized bulk response')
                    digest.update(block)
            if count < 1024 * 1024 or digest.hexdigest() != '@BULK_HASH@':
                raise RuntimeError('Bulk HTTPS checksum mismatch')
            print('WINDOWS-INTEGRATION: BULK-HTTPS-OK', count, flush=True)
            return
        except (OSError, RuntimeError, http.client.HTTPException) as error:
            print('WINDOWS-INTEGRATION: BULK-ATTEMPT-FAILED', attempt + 1, str(error), flush=True)
            if attempt == 2:
                raise
            time.sleep(5)

blocked_direct()
stream, reply = proxy('127.0.0.1')
stream.close()
if not reply.startswith(b'HTTP/1.1 403'):
    raise RuntimeError('Gateway allowed a local destination')
print('WINDOWS-INTEGRATION: LOCAL-BLOCKED', flush=True)
public_pair()
bulk_https()
'''.replace('@BULK_PATH@', gnupg.URL.split('gnupg.org', 1)[1]).replace('@BULK_HASH@', gnupg.SHA256)

DESKTOP_PROBE = '''import pathlib, subprocess, time
deadline = time.monotonic() + 1200
while not pathlib.Path('/var/lib/claude-isolation-ready').is_file():
    # Production setup retries transient outages automatically. Do not shut
    # down its VM just because the first systemd start job had to retry.
    if time.monotonic() >= deadline:
        raise RuntimeError('Automatic desktop installation did not complete')
    time.sleep(5)
for package in ('claude-desktop', 'firefox'):
    installed = subprocess.check_output(['dpkg-query', '-W', '-f=${db:Status-Status}', package], text=True)
    if installed != 'installed':
        raise RuntimeError('Missing automatically installed package: ' + package)
    subprocess.run(['dpkg-query', '-W', '-f=WINDOWS-INTEGRATION: INSTALLED ${Package} ${Version}\\n', package], check=True)
deadline = time.monotonic() + 90
while True:
    desktop = subprocess.run(['runuser', '-u', 'claude', '--', 'env', 'DISPLAY=:0',
                             'XAUTHORITY=/home/claude/.Xauthority', 'xrandr', '--current'],
                            capture_output=True, text=True)
    if desktop.returncode == 0 and ' connected' in desktop.stdout:
        print(desktop.stdout, flush=True)
        break
    if time.monotonic() >= deadline:
        raise RuntimeError('Installed graphical desktop did not start: ' + desktop.stderr)
    time.sleep(2)
subprocess.run(['systemctl', 'is-active', '--quiet', 'lightdm'], check=True)
print('WINDOWS-INTEGRATION: DESKTOP-READY', flush=True)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop', action='store_true', help='Verify complete automatic desktop installation')
    args = parser.parse_args()
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
        cfg.update(qemu_executable=str(executable), accelerator='tcg', display='sdl',
                   memory_mb=3072 if args.desktop else 2048)
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
        cloud_data = {
            'hostname': 'windows-isolation-test', 'ssh_pwauth': False,
            'bootcmd': [boot_command(ROOT)],
            'write_files': [{'path': '/ci-probe.py', 'content': PROBE, 'permissions': '0600'}],
            'runcmd': [['python3', '/ci-probe.py'], ['systemctl', 'poweroff']],
        }
        if args.desktop:
            # Keep the production first-run setup; only add account-free
            # assertions and shutdown AFTER it finishes automatically.
            cloud_data = json.loads(environment.cloud_config(cfg).split('\n', 1)[1])
            cloud_data['write_files'].append({'path': '/ci-probe.py',
                                             'content': DESKTOP_PROBE + PROBE, 'permissions': '0600'})
            cloud_data['runcmd'] += [['python3', '/ci-probe.py'], ['systemctl', 'poweroff']]
        cloud = '#cloud-config\n' + json.dumps(cloud_data)
        # This guest is disposable and has never booted. No account data is
        # copied. The desktop mode uses the production bootstrap unchanged.
        seed_directory = directory / 'probe-seed'
        seed_directory.mkdir()
        (seed_directory / 'user-data').write_text(cloud, encoding='utf-8', newline='\n')
        (seed_directory / 'meta-data').write_text('instance-id: windows-gateway-ci\n', encoding='utf-8', newline='\n')
        prepared_seed = directory / 'probe.iso'
        backend.seed_iso(seed_directory, prepared_seed)
        os.replace(prepared_seed, cfg['seed'])
        print('Booting disposable Ubuntu and checking gateway…', flush=True)
        # Exercise the same persistent compatibility selection and start action
        # used by the GUI, rather than bypassing it with the generic CLI.
        subprocess.run([str(core), 'accel-tcg', '--data', str(path.parent)],
                       check=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        job = Job()
        process = None
        try:
            with (report / 'core.log').open('wb') as output:
                process = subprocess.Popen([
                    str(core),
                    'start', '--data', str(path.parent), '--start-gate'],
                    stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                job.assign(process)
                process.stdin.write(b'GO\n')
                process.stdin.flush()
                process.stdin.close()
                deadline = time.monotonic() + 120
                while True:
                    try:
                        boot = Path(cfg['boot_log']).read_text(encoding='utf-8', errors='replace')
                    except OSError:
                        boot = ''
                    # Do not connect to QMP from the test until the guest has
                    # booted: that used to unblock a broken launcher and hide
                    # its permanent startup hang from CI.
                    if 'Linux version ' in boot:
                        try:
                            ready = network_guard.read_state(control_paths(cfg)['ready'])
                            if isinstance(ready, dict) and ready.get('ready'):
                                break
                        except (OSError, ValueError):
                            pass
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Guest did not boot without an external QMP client')
                    time.sleep(.5)
                for _ in range(2):
                    control = backend.qmp(cfg, 'query-status')
                    if not control.get('running'):
                        raise RuntimeError('Windows control owner lost the running VM')
                (report / 'qmp.json').write_text(json.dumps(control), encoding='utf-8')
                boot_timeout = 1500 if args.desktop else 420
                deadline = time.monotonic() + boot_timeout
                history = report / 'network-events.jsonl'
                history.write_text('', encoding='utf-8')
                previous = None
                while process.poll() is None:
                    try:
                        state = network_guard.read_state(cfg['network_status'])
                        state['permission_current'] = network_guard.permitted(cfg['network_status'])
                        if state != previous:
                            with history.open('a', encoding='utf-8') as events:
                                events.write(json.dumps(state) + '\n')
                            previous = state
                    except (OSError, ValueError, TypeError):
                        pass
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(process.args, boot_timeout)
                    try:
                        process.wait(timeout=.5)
                    except subprocess.TimeoutExpired:
                        pass
                if process.returncode:
                    raise RuntimeError('Packaged launcher failed during guest boot')
        finally:
            # Preserve final state as well as the transitions collected above.
            try:
                state = network_guard.read_state(cfg['network_status'])
                state['permission_current'] = network_guard.permitted(cfg['network_status'])
                (report / 'network-state.json').write_text(json.dumps(state), encoding='utf-8')
            except (OSError, ValueError, TypeError):
                pass
            network_guard.revoke(None, cfg['network_status'])
            job.close()
            if process and process.poll() is None:
                process.wait(timeout=10)
            if Path(cfg['boot_log']).is_file():
                shutil.copy2(cfg['boot_log'], report / 'boot.log')
        boot = (report / 'boot.log').read_text(encoding='utf-8', errors='replace')
        for marker in ('DIRECT-BLOCKED', 'LOCAL-BLOCKED', 'PUBLIC-HTTPS-OK', 'BULK-HTTPS-OK'):
            if 'WINDOWS-INTEGRATION: ' + marker not in boot:
                lines = boot.splitlines()
                for index, line in enumerate(lines):
                    if any(word in line for word in ('WINDOWS-INTEGRATION:', 'Traceback', 'Error:', 'ci-probe.py')):
                        print('\n'.join(lines[max(0, index - 2):index + 12]))
                print((report / 'core.log').read_text(encoding='utf-8', errors='replace')[-12000:])
                raise RuntimeError('Guest network check failed: ' + marker)
        if boot.count('WINDOWS-INTEGRATION: PUBLIC-HTTPS-OK') != 2:
            raise RuntimeError('Both concurrent public HTTPS connections must succeed')
        if args.desktop and 'WINDOWS-INTEGRATION: DESKTOP-READY' not in boot:
            raise RuntimeError('Full automatic desktop and applications were not verified')
        result = {'windows_qemu_boot': True, 'packaged_gateway': True,
                  'sdl_guest_window': True, 'automatic_desktop_verified': args.desktop,
                  'boots_without_external_qmp_client': True, 'repeated_control_requests': True,
                  'bulk_https_checksum_verified': True,
                  'direct_internet_blocked': True, 'local_targets_blocked': True,
                  'public_https_connections': boot.count('WINDOWS-INTEGRATION: PUBLIC-HTTPS-OK'),
                  'seconds': round(time.monotonic() - started, 1)}
        (report / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
