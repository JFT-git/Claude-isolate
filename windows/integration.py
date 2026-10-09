#!/usr/bin/env python3
"""Disposable Windows QEMU boot check of the packaged gateway, without accounts."""
import hashlib
import argparse
import json
import os
import re
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
import release_image
from windows import backend
from windows import gnupg
from windows.job import Job
from windows.serial_gateway import boot_command
from windows.control import paths as control_paths
from windows.guest_update import REVISION

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

def gateway_latency():
    # Informational: time from a new guest connection to the gateway's reply,
    # including the host relay start and the outbound TCP connection.
    samples = []
    for _ in range(5):
        started = time.monotonic()
        stream, reply = proxy('www.cloudflare.com')
        stream.close()
        if reply.startswith(b'HTTP/1.1 200'):
            samples.append(round((time.monotonic() - started) * 1000))
    print('WINDOWS-INTEGRATION: GATEWAY-CONNECT-MS', *samples, flush=True)

blocked_direct()
stream, reply = proxy('127.0.0.1')
stream.close()
if not reply.startswith(b'HTTP/1.1 403'):
    raise RuntimeError('Gateway allowed a local destination')
print('WINDOWS-INTEGRATION: LOCAL-BLOCKED', flush=True)
public_pair()
bulk_https()
gateway_latency()
'''.replace('@BULK_PATH@', gnupg.URL.split('gnupg.org', 1)[1]).replace('@BULK_HASH@', gnupg.SHA256)

DESKTOP_PROBE = '''import pathlib, subprocess, time
deadline = time.monotonic() + 1800
while not pathlib.Path('/var/lib/claude-isolation-ready').is_file():
    # Production setup retries transient outages automatically. Do not shut
    # down its VM just because the first systemd start job had to retry.
    if time.monotonic() >= deadline:
        raise RuntimeError('Automatic desktop installation did not complete')
    time.sleep(5)
if not pathlib.Path('/etc/cloud/cloud-init.disabled').is_file():
    raise RuntimeError('Reboot test must not rely on cloud-init boot commands')
if subprocess.check_output(['systemctl', 'is-enabled', 'claude-gateway.service'], text=True).strip() != 'enabled':
    raise RuntimeError('Private gateway is not enabled for subsequent boots')
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
for command in (['systemd-analyze'], ['systemd-analyze', 'critical-chain', 'graphical.target']):
    analysis = subprocess.run(command, capture_output=True, text=True)
    for line in (analysis.stdout + analysis.stderr).splitlines():
        print('WINDOWS-INTEGRATION: TIMING', line, flush=True)
'''

UPGRADE_PROBE = '''import hashlib
fixture = pathlib.Path('/var/lib/claude-isolate/ci-preservation.json')
home = pathlib.Path('/home/claude')
if fixture.exists():
    import json
    for relative, expected in json.loads(fixture.read_text()).items():
        actual = hashlib.sha256((home / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError('Guest upgrade changed user data: ' + relative)
    if pathlib.Path('/etc/claude-isolate/revision').read_text().strip() != '@REVISION@':
        raise RuntimeError('Offline updater did not install the current guest revision')
    state = subprocess.check_output(['systemctl', 'show', '-p', 'LoadState', '--value', 'console-setup.service'], text=True)
    if state.strip() != 'masked' or pathlib.Path('/etc/udev/rules.d/90-console-setup.rules').read_bytes():
        raise RuntimeError('Offline updater did not install boot tuning')
    deadline = time.monotonic() + 600
    while not pathlib.Path('/var/lib/claude-isolate/updated-@REVISION@').exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Online application update did not complete')
        time.sleep(5)
    print('WINDOWS-INTEGRATION: USERDATA-PRESERVED', flush=True)
    print('WINDOWS-INTEGRATION: GUEST-UPDATE-OK', flush=True)
    # The next boot stays running for the saved-state test.
    keep = pathlib.Path('/var/lib/claude-isolate/ci-keep-running')
    if not keep.exists():
        keep.touch()
        subprocess.run(['systemctl', 'enable', 'ci-net-loop.service'], check=True)
        subprocess.run(['systemctl', '--no-block', 'poweroff'], check=True)
else:
    import json
    files = {'Documents/preserved.txt': b'User document before upgrade\\n',
             '.config/claude-isolate-ci/preferences.json': b'{"language":"fr","custom":true}',
             '.config/claude-isolate-ci/session': b'test-session-no-real-credentials'}
    hashes = {}
    for relative, content in files.items():
        file = home / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
        hashes[relative] = hashlib.sha256(content).hexdigest()
    fixture.write_text(json.dumps(hashes))
    # Model an installed OLD image with cloud-init disabled and a missing
    # persistent gateway. Merely replacing its seed cannot fix its next boot.
    pathlib.Path('/etc/systemd/system/claude-gateway.service').unlink()
    pathlib.Path('/usr/local/sbin/claude-environment-update').unlink()
    pathlib.Path('/etc/systemd/system/console-setup.service').unlink(missing_ok=True)
    pathlib.Path('/etc/udev/rules.d/90-console-setup.rules').unlink(missing_ok=True)
    pathlib.Path('/etc/claude-isolate/revision').write_text('0.3.12\\n')
    pathlib.Path('/var/lib/claude-isolate/updated-@REVISION@').unlink(missing_ok=True)
    print('WINDOWS-INTEGRATION: LEGACY-GUEST-PREPARED', flush=True)
'''.replace('@REVISION@', REVISION)

NET_LOOP = '''import socket, ssl, time
while True:
    try:
        stream = socket.create_connection(('10.0.2.100', 7890), timeout=20)
        stream.sendall(b'CONNECT www.cloudflare.com:443 HTTP/1.1\\r\\n\\r\\n')
        reply = stream.recv(4096)
        if not reply.startswith(b'HTTP/1.1 200'):
            stream.close()
            raise RuntimeError(reply[:16])
        with ssl.create_default_context().wrap_socket(stream, server_hostname='www.cloudflare.com') as tls:
            tls.sendall(b'GET /cdn-cgi/trace HTTP/1.1\\r\\nHost: www.cloudflare.com\\r\\nConnection: close\\r\\n\\r\\n')
            if b'ip=' not in tls.recv(4096):
                raise RuntimeError('no trace')
        print('WINDOWS-INTEGRATION: NET-OK', int(time.time()), flush=True)
    except Exception as error:
        print('WINDOWS-INTEGRATION: NET-FAIL', error, flush=True)
    time.sleep(10)
'''


def install_guest_update(directory, data, report):
    installer = ROOT / 'dist/Claude-isolate-windows-x64-setup.exe'
    target = directory / 'Installer updated app'
    path = data / 'environment.json'
    cfg = environment.load_config(path)
    disk, seed = cfg['disk'], cfg['seed']
    seed_digest = hashlib.sha256(Path(seed).read_bytes()).hexdigest()
    cfg.update(guest_revision='0.3.12', guest_gateway_version=1)
    backend.write_config(path, cfg)
    command = [str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
               '/DIR=' + str(target), '/GUESTDATA=' + str(data), '/TASKS=']
    print('Updating the existing guest through the REAL installer…', flush=True)
    subprocess.run(command, check=True, timeout=1800)
    for name in ('installer-update.log', 'guest-update-boot.log', 'guest-update-qemu.log'):
        if (data / name).exists():
            shutil.copy2(data / name, report / name)
    updated = environment.load_config(path)
    if updated.get('guest_revision') != REVISION or not updated.get('guest_update_snapshot'):
        print((data / 'installer-update.log').read_text(encoding='utf-8', errors='replace')[-12000:])
        raise RuntimeError('The installer did not update the old Linux image')
    if updated['disk'] != disk or updated['seed'] != seed:
        raise RuntimeError('Installer switched to a different disk or seed path')
    if hashlib.sha256(Path(updated['guest_update_seed_backup']).read_bytes()).hexdigest() != seed_digest:
        raise RuntimeError('Installer did not preserve the old seed for rollback')
    snapshot = updated['guest_update_snapshot']
    subprocess.run(command, check=True, timeout=120)
    if environment.load_config(path).get('guest_update_snapshot') != snapshot:
        raise RuntimeError('Installing the same version repeated the guest migration')
    return updated


def start_core(core, path, report, name):
    output = (report / (name + '.log')).open('wb')
    process = subprocess.Popen([str(core), 'start', '--data', str(path.parent), '--start-gate'],
                               stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    process.stdin.write(b'GO\n')
    process.stdin.flush()
    process.stdin.close()
    return process, output


def boot_text(cfg):
    try:
        return re.sub(r'\x1b\[[0-9;]*m', '', Path(cfg['boot_log']).read_text(encoding='utf-8', errors='replace'))
    except OSError:
        return ''


def wait_for(condition, process, timeout, what):
    deadline = time.monotonic() + timeout
    while not condition():
        if process.poll() is not None:
            raise RuntimeError(what + ': launcher exited with ' + str(process.returncode))
        if time.monotonic() >= deadline:
            raise RuntimeError(what + ': timed out')
        time.sleep(1)


def network_after(cfg):
    # The guest prints its own Unix time with every successful gateway HTTPS.
    for line in reversed(boot_text(cfg).splitlines()):
        match = re.search(r'WINDOWS-INTEGRATION: NET-OK (\d+)', line)
        if match:
            return int(match.group(1))
    return None


def run_fast_start(core, path, cfg, report):
    """Save a running guest, restore it, and verify the network and clock."""
    from windows import state
    report.mkdir(parents=True, exist_ok=True)
    job = Job()
    measured = {}
    process = output = None
    try:
        process, output = start_core(core, path, report, 'boot-3')
        job.assign(process)
        wait_for(lambda: 'CLAUDE-ISOLATION: desktop-ready' in boot_text(cfg)
                 and network_after(cfg) is not None, process, 1800, 'Cold boot before saving')
        shutil.copy2(cfg['boot_log'], report / 'boot-3.boot.log')
        started = time.monotonic()
        saved = subprocess.run([str(core), 'suspend', '--data', str(path.parent)], capture_output=True,
                               timeout=900, creationflags=subprocess.CREATE_NO_WINDOW)
        (report / 'suspend.log').write_bytes(saved.stdout + saved.stderr)
        if saved.returncode:
            raise RuntimeError('Saving the running guest failed: ' + saved.stdout.decode(errors='replace')[-2000:])
        process.wait(timeout=300)
        output.close()
        measured['suspend_seconds'] = round(time.monotonic() - started, 1)
        if process.returncode or not state.exists(cfg):
            raise RuntimeError('No saved state after suspending')
        info = json.loads(subprocess.check_output([state.image_tool(cfg), 'info', '--output=json', cfg['disk']],
                                                  creationflags=subprocess.CREATE_NO_WINDOW))
        snapshots = [item for item in info.get('snapshots', []) if item.get('name') == state.TAG]
        if len(snapshots) != 1:
            raise RuntimeError('The fast-start snapshot is missing from the disk')
        measured['state_mib'] = snapshots[0].get('vm-state-size', 0) // 1048576
        time.sleep(30)
        started = time.monotonic()
        process, output = start_core(core, path, report, 'boot-4')
        job.assign(process)
        from windows.control import paths as control_paths
        def restored():
            try:
                return bool(network_guard.read_state(control_paths(cfg)['ready']).get('restored'))
            except (OSError, ValueError, AttributeError):
                return False
        wait_for(restored, process, 600, 'Restoring the saved guest')
        measured['restore_seconds'] = round(time.monotonic() - started, 1)
        if state.exists(cfg):
            raise RuntimeError('A restored guest kept its single-use saved state')
        wait_for(lambda: network_after(cfg) is not None, process, 300, 'Gateway HTTPS after restore')
        measured['network_after_restore_seconds'] = round(time.monotonic() - started, 1)
        guest_time = network_after(cfg)
        measured['clock_skew_seconds'] = round(time.time() - guest_time, 1)
        if abs(measured['clock_skew_seconds']) > 30:
            raise RuntimeError('Restored guest clock was not corrected: ' + str(measured['clock_skew_seconds']))
        shutil.copy2(cfg['boot_log'], report / 'boot-4.boot.log')
        stopped = subprocess.run([str(core), 'stop', '--data', str(path.parent)], capture_output=True,
                                 timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        if stopped.returncode:
            raise RuntimeError('Power-off after restore failed')
        process.wait(timeout=600)
        info = json.loads(subprocess.check_output([state.image_tool(cfg), 'info', '--output=json', cfg['disk']],
                                                  creationflags=subprocess.CREATE_NO_WINDOW))
        if any(item.get('name') == state.TAG for item in info.get('snapshots', [])):
            raise RuntimeError('The used fast-start snapshot was not removed from the disk')
    finally:
        network_guard.revoke(None, cfg['network_status'])
        job.close()
        if output:
            output.close()
        if Path(cfg['boot_log']).is_file():
            shutil.copy2(cfg['boot_log'], report / 'boot.log')
    (report / 'fast-start.json').write_text(json.dumps(measured), encoding='utf-8')
    return measured


def run_guest(core, path, cfg, report, desktop):
    report.mkdir(parents=True, exist_ok=True)
    job = Job()
    process = None
    timings = {}
    try:
        with (report / 'core.log').open('wb') as output:
            started = time.monotonic()
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
                    timings.setdefault('kernel_started_seconds', round(time.monotonic() - started, 1))
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
            boot_timeout = 2100 if desktop else 420
            deadline = time.monotonic() + boot_timeout
            history = report / 'network-events.jsonl'
            history.write_text('', encoding='utf-8')
            previous = None
            desktop_control_checked = False
            while process.poll() is None:
                if desktop and not desktop_control_checked:
                    boot = Path(cfg['boot_log']).read_text(encoding='utf-8', errors='replace')
                    if 'CLAUDE-ISOLATION: desktop-ready' in boot:
                        timings['desktop_ready_seconds'] = round(time.monotonic() - started, 1)
                        control = backend.qmp(cfg, 'query-status')
                        if not control.get('running'):
                            raise RuntimeError('Graphical desktop stopped responding to QMP')
                        pointers = backend.qmp(cfg, 'query-mice')
                        if not any(pointer.get('absolute') and pointer.get('current') for pointer in pointers):
                            raise RuntimeError('Desktop has no active absolute pointer')
                        (report / 'desktop-qmp.json').write_text(json.dumps({'status': control, 'mice': pointers}), encoding='utf-8')
                        desktop_control_checked = True
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
            if desktop and not desktop_control_checked:
                raise RuntimeError('Post-desktop control and absolute pointer were not verified')
    finally:
        (report / 'timings.json').write_text(json.dumps(timings), encoding='utf-8')
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
    if desktop and 'WINDOWS-INTEGRATION: DESKTOP-READY' not in boot:
        raise RuntimeError('Full automatic desktop and applications were not verified')
    if desktop and 'CLAUDE-ISOLATION: desktop-ready' not in boot:
        raise RuntimeError('Production graphical readiness was not reported on this boot')
    return boot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop', action='store_true', help='Verify complete automatic desktop installation')
    parser.add_argument('--memory-mb', type=int, choices=(1024, 2048, 3072),
                        help='Guest RAM for the real packaged boot test')
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
        print('Verifying bundled QEMU with no external tools in PATH…', flush=True)
        executable = ROOT / 'dist/windows/Claude Isolate/runtime/qemu/qemu-system-x86_64.exe'
        os.environ['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
        (report / 'qemu-version.txt').write_bytes(subprocess.check_output([str(executable), '--version']))
        path, cfg = backend.config(directory / 'data')
        memory_mb = args.memory_mb or (3072 if args.desktop else 2048)
        cfg.update(qemu_executable=str(executable), accelerator='tcg', memory_mb=memory_mb,
                   cpus=1 if memory_mb == 1024 else 2,
                   resources_mode='minimal' if memory_mb == 1024 else 'economy')
        backend.write_config(path, cfg)
        core = ROOT / 'dist/windows/Claude Isolate/Claude Isolate Core.exe'
        # Seed the normal digest-addressed cache from this CI run's artifact.
        # The release URL is not published until all installation tests pass.
        manifest = release_image.metadata(cfg['arch'])
        artifact = ROOT / 'dist/guest' / manifest['filename']
        if not release_image.matches(artifact, manifest):
            raise RuntimeError('CI prepared image does not match the embedded manifest')
        cache = path.parent / 'downloads'
        cache.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact, cache / (manifest['sha256'] + '.qcow2'))
        # Use the actual packaged first-run setup, including GPG, qemu-img,
        # image download/signature checks and the bundled ISO writer.
        with (report / 'prepare.log').open('wb') as output:
            print('Preparing Ubuntu with the packaged executable…', flush=True)
            prepared = subprocess.run([str(core), 'prepare', '--data', str(path.parent)],
                                      stdout=output, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW, timeout=600,
                                      env=dict(os.environ, CLAUDE_SKIP_PREBOOT='1'))
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
            # Start setup asynchronously so readiness is tested concurrently
            # with bootstrap, including the state after a failed/retried apt
            # download. Waiting for the first systemctl job hid this race.
            commands = []
            for command in cloud_data['runcmd']:
                if command == ['systemctl', 'enable', '--now', 'claude-setup.service']:
                    commands += [['systemctl', 'enable', 'claude-setup.service'],
                                 ['systemctl', '--no-block', 'start', 'claude-setup.service']]
                else:
                    commands.append(command)
            cloud_data['runcmd'] = commands
            cloud_data['write_files'].append({'path': '/ci-probe.py',
                                             'content': DESKTOP_PROBE + PROBE + UPGRADE_PROBE, 'permissions': '0600'})
            cloud_data['write_files'].append({'path': '/etc/systemd/system/ci-reboot-probe.service',
                'content': ('[Unit]\nDescription=Verify guest after reboot\n'
                            'After=claude-desktop-ready.service claude-gateway.service\n'
                            'Wants=claude-desktop-ready.service claude-gateway.service\n'
                            'ConditionPathExists=/var/lib/claude-isolation-ready\n'
                            '[Service]\nType=oneshot\nTimeoutStartSec=300\n'
                            'StandardOutput=journal+console\nStandardError=journal+console\n'
                            'ExecStart=/usr/bin/python3 /ci-probe.py\n'
                            'ExecStopPost=/bin/sh -c "test -e /var/lib/claude-isolate/ci-keep-running || '
                            'systemctl --no-block poweroff"\n'
                            '[Install]\nWantedBy=multi-user.target\n'), 'permissions': '0644'})
            cloud_data['write_files'].append({'path': '/ci-net-loop.py', 'content': NET_LOOP, 'permissions': '0600'})
            cloud_data['write_files'].append({'path': '/etc/systemd/system/ci-net-loop.service',
                'content': ('[Unit]\nDescription=Report gateway HTTPS and guest time\n'
                            'After=claude-gateway.service\n[Service]\nType=simple\n'
                            'StandardOutput=journal+console\nStandardError=journal+console\n'
                            'ExecStart=/usr/bin/python3 /ci-net-loop.py\n'
                            '[Install]\nWantedBy=multi-user.target\n'), 'permissions': '0644'})
            cloud_data['runcmd'] += [['systemctl', 'daemon-reload'],
                                    ['systemctl', 'enable', 'ci-reboot-probe.service'],
                                    ['python3', '/ci-probe.py'], ['systemctl', 'poweroff']]
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
        boots = []
        for cycle in range(2 if args.desktop else 1):
            print('Verifying guest boot', cycle + 1, flush=True)
            evidence = report / ('boot-' + str(cycle + 1))
            boot = run_guest(core, path, cfg, evidence, args.desktop)
            boots.append(boot)
            if args.desktop and cycle == 0:
                if 'preinstalled-image no-package-downloads' not in boot or re.search(r'Get:\d+ ', boot):
                    raise RuntimeError('Fresh release guest downloaded packages or missed the preinstalled path')
            if args.desktop and cycle == 0:
                cfg = install_guest_update(directory, path.parent, report)
            if args.desktop and cycle == 1:
                for marker in ('USERDATA-PRESERVED', 'GUEST-UPDATE-OK'):
                    if 'WINDOWS-INTEGRATION: ' + marker not in boot:
                        raise RuntimeError('Old image upgrade was not verified: ' + marker)
        # Retain the original evidence names for existing artifact consumers.
        for file in evidence.iterdir():
            shutil.copy2(file, report / file.name)
        fast_start = run_fast_start(core, path, cfg, report / 'fast-start') if args.desktop else None
        timings = []
        for cycle, boot_text in enumerate(boots):
            plain = re.sub(r'\x1b\[[0-9;]*m', '', boot_text)
            measured = json.loads((report / ('boot-' + str(cycle + 1)) / 'timings.json').read_text(encoding='utf-8'))
            gateway = re.findall(r'WINDOWS-INTEGRATION: GATEWAY-CONNECT-MS ([\d ]*)', plain)
            measured['gateway_connect_ms'] = [int(value) for value in gateway[-1].split()] if gateway else []
            startup = re.findall(r'WINDOWS-INTEGRATION: TIMING (Startup finished in .*)', plain)
            measured['guest_startup'] = startup[-1].strip() if startup else None
            timings.append(measured)
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as output:
                output.write('### Windows TCG timings (' + str(cfg['memory_mb']) + ' MB, ' + str(cfg['cpus']) + ' CPU)\n\n')
                for cycle, measured in enumerate(timings):
                    output.write(f'- Boot {cycle + 1}: kernel {measured.get("kernel_started_seconds")} s, '
                                 f'desktop {measured.get("desktop_ready_seconds")} s, '
                                 f'guest: {measured["guest_startup"]}, gateway connect ms: {measured["gateway_connect_ms"]}\n')
                if fast_start:
                    output.write(f'- Fast start: save {fast_start["suspend_seconds"]} s ({fast_start["state_mib"]} MiB), '
                                 f'restore {fast_start["restore_seconds"]} s, gateway HTTPS after '
                                 f'{fast_start["network_after_restore_seconds"]} s, clock skew '
                                 f'{fast_start["clock_skew_seconds"]} s\n')
        result = {'windows_qemu_boot': True, 'packaged_gateway': True,
                  'memory_mb': cfg['memory_mb'], 'cpus': cfg['cpus'],
                  'guest_display': 'gtk', 'bundled_runtime': True, 'external_tools_removed_from_path': True, 'automatic_desktop_verified': args.desktop,
                  'boot_cycles': len(boots), 'gateway_after_reboot_verified': len(boots) == 2,
                  'installer_existing_guest_upgrade_verified': args.desktop,
                  'user_files_profiles_and_settings_preserved': args.desktop,
                  'same_version_upgrade_idempotent': args.desktop,
                  'boots_without_external_qmp_client': True, 'repeated_control_requests': True,
                  'bulk_https_checksum_verified': True,
                  'direct_internet_blocked': True, 'local_targets_blocked': True,
                  'public_https_connections': boot.count('WINDOWS-INTEGRATION: PUBLIC-HTTPS-OK'),
                  'boot_timings': timings,
                  'fast_start': fast_start,
                  'seconds': round(time.monotonic() - started, 1)}
        (report / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
