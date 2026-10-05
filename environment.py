#!/usr/bin/env python3
"""Experimental local VM launcher; no system-wide networking changes."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import signal
import subprocess
import sys
sys.dont_write_bytecode = True
import threading
import tempfile
import time
import network_guard
from session_lock import exclusive
from route_guard import RouteWatcher
from network_transport import open_public, public_address
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
ALLOWED_SUFFIXES = ('claude.ai', 'claude.com', 'claude.app', 'anthropic.com',
                    'claudeusercontent.com', 'claudemcpcontent.com',
                    'ubuntu.com', 'debian.org')
ALLOWED_EXACT = ('accounts.google.com', 'oauth2.googleapis.com',
                 'www.googleapis.com', 'www.gstatic.com', 'fonts.googleapis.com',
                 'fonts.gstatic.com', 'cdnjs.cloudflare.com', 'cdn.jsdelivr.net',
                 'cdn.tailwindcss.com', 'code.jquery.com', 'unpkg.com', 'packages.mozilla.org')


def allowed_request(header, web_access='services'):
    """Validate the first request before selecting the configured transport.

    System mode resolves approved domains with public-address validation;
    proxy mode delegates resolution to the configured local proxy.
    """
    try:
        if len(header) > 32768 or not header.endswith(b'\r\n\r\n'):
            return False
        lines = header[:-4].split(b'\r\n')
        # urlsplit strips some controls; reject them BEFORE parsing to avoid
        # validating one target and forwarding another request to the server.
        if any(c < 33 or c > 126 for c in lines[0] if c != 32):
            return False
        for line in lines[1:]:
            name, separator, value = line.partition(b':')
            if not separator or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                return False
            if any(c < 32 and c != 9 or c == 127 for c in value):
                return False
        names = [line.split(b':', 1)[0].lower() for line in lines[1:]]
        if names.count(b'host') > 1 or any(n in names for n in (b'content-length', b'transfer-encoding', b'upgrade', b'expect')):
            return False
        first = header.split(b'\r\n', 1)[0].decode('ascii')
        method, target, version = first.split(' ')
        if version not in ('HTTP/1.0', 'HTTP/1.1'):
            return False
        if method == 'CONNECT':
            url = urlsplit('//' + target)
            if url.port != 443 or url.path or url.query or url.fragment:
                return False
        else:
            # Used by apt for package mirrors; prevent HTTP keep-alive from
            # switching to a different destination after the first request.
            if method not in ('GET', 'HEAD') or version != 'HTTP/1.1':
                return False
            url = urlsplit(target)
            if url.scheme != 'http' or url.port not in (None, 80) or url.fragment:
                return False
        host = (url.hostname or '').lower()
        if url.username is not None or url.password is not None or not host or host.endswith('.'):
            return False
        if web_access == 'public':
            try:
                return public_address(ipaddress.ip_address(host))
            except ValueError:
                # DNS is resolved and checked for public addresses again by
                # open_public immediately before connecting, with no relookup.
                return (len(host) <= 253 and '.' in host
                        and not host.endswith(('.local', '.localhost', '.internal', '.lan'))
                        and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                for label in host.split('.')))
        if web_access != 'services':
            return False
        return host in ALLOWED_EXACT or any(
            host == suffix or host.endswith('.' + suffix) for suffix in ALLOWED_SUFFIXES)
    except (UnicodeError, ValueError):
        return False


def http_headers(header, authority):
    """Keep end-to-end HTTP headers (cookies, ranges, auth), remove hop headers."""
    fields = [line.split(b':', 1) for line in header[:-4].split(b'\r\n')[1:]]
    blocked = {b'host', b'connection', b'proxy-connection', b'keep-alive',
               b'proxy-authorization', b'proxy-authenticate', b'te', b'trailer'}
    for name, value in fields:
        if name.lower() == b'connection':
            blocked.update(token.strip().lower() for token in value.split(b','))
    kept = b''.join(name + b':' + value + b'\r\n' for name, value in fields if name.lower() not in blocked)
    return b'Host: ' + authority.encode('ascii') + b'\r\n' + kept + b'Connection: close\r\n\r\n'


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    if cfg['arch'] not in ('aarch64', 'x86_64'):
        raise ValueError('arch must be aarch64 or x86_64')
    mode = cfg.setdefault('network_mode', 'system')
    access = cfg.setdefault('web_access', 'services')
    if access not in ('services', 'public'):
        raise ValueError('web_access must be services or public')
    if access == 'public' and mode != 'system':
        raise ValueError('Public browsing requires system VPN mode with public-address validation')
    if mode not in ('system', 'proxy'):
        raise ValueError('network_mode must be system or proxy')
    fields = [('memory_mb', 2048, 65536), ('cpus', 1, 32)]
    if mode == 'proxy':
        fields.append(('proxy_port', 1, 65535))
    for key, minimum, maximum in fields:
        if type(cfg[key]) is not int or not minimum <= cfg[key] <= maximum:
            raise ValueError(f'Invalid {key}')
    return cfg


def tool(name):
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f'Missing dependency: {name}')
    return found


def local_path(value):
    p = Path(value).expanduser()
    return p.resolve() if p.is_absolute() else (ROOT / p).resolve()


def qemu_path(p):
    return str(p).replace(',', ',,')


def command(cfg, check=True):
    arch = cfg['arch']
    system = platform.system()
    native = (platform.machine().lower() in ('arm64', 'aarch64')) == (arch == 'aarch64')
    accel = ('hvf' if system == 'Darwin' else 'kvm' if system == 'Linux'
             else 'whpx') if native else 'tcg'
    if arch == 'aarch64' and system == 'Windows':
        accel = 'tcg'
    if system == 'Windows' and cfg.get('accelerator'):
        if cfg['accelerator'] not in ('whpx', 'tcg'):
            raise ValueError('Unsupported Windows accelerator')
        accel = cfg['accelerator']
    exe = str(local_path(cfg['qemu_executable'])) if cfg.get('qemu_executable') else (tool(f'qemu-system-{arch}') if check else f'qemu-system-{arch}')
    mode = cfg.get('network_mode', 'system')
    relay = ([sys.executable, 'relay'] if getattr(sys, 'frozen', False)
             else [sys.executable, str(ROOT / 'environment.py'), 'relay'])
    relay += ['--mode', mode]
    relay += ['--web-access', cfg.get('web_access', 'services')]
    if mode == 'proxy':
        relay += ['--port', str(cfg['proxy_port'])]
    # QEMU's cmd forwarding starts a separate relay for every TCP connection.
    # All values are local validated config / fixed paths, never guest input.
    # libslirp uses GLib g_shell_parse_argv on Windows too, rather than the
    # CommandLineToArgvW parser. Backslashes and spaces need POSIX quoting.
    relay_cmd = shlex.join(relay)
    net = ('user,id=isolated,restrict=on,ipv6=off,'
           'guestfwd=tcp:10.0.2.100:7890-cmd:' + relay_cmd.replace(',', ',,'))
    cmd = [exe, '-name', 'Claude isolated desktop', '-nodefaults',
           '-machine', ('virt' if arch == 'aarch64' else 'q35') + (',dump-guest-core=off' if system == 'Linux' else ''),
           '-accel', accel, '-cpu', 'host' if accel in ('hvf', 'kvm') else 'qemu64' if accel == 'whpx' else 'max',
           '-m', str(cfg['memory_mb']), '-smp', str(cfg['cpus']),
           '-drive', f'file={qemu_path(local_path(cfg["disk"]))},if=virtio,format=qcow2,discard=unmap,detect-zeroes=unmap',
           '-drive', f'file={qemu_path(local_path(cfg["seed"]))},if=virtio,format=raw,readonly=on',
           '-netdev', net, '-device', 'virtio-net-pci,netdev=isolated',
           '-device', 'virtio-gpu-pci,edid=off,xres=1920,yres=1200', '-device', 'qemu-xhci',
           '-device', 'usb-kbd', '-device', 'usb-tablet',
           '-monitor', 'none', '-serial', 'none']
    if system == 'Darwin':
        cmd += ['-display', 'cocoa,zoom-to-fit=on,zoom-interpolation=on,full-screen=on']
        if cfg.get('qemu_data_dir'):
            cmd += ['-L', str(local_path(cfg['qemu_data_dir']))]
    elif system == 'Windows':
        display = cfg.get('display', 'sdl')
        if display not in ('sdl', 'none'):
            raise ValueError('Unsupported Windows display')
        cmd += ['-display', display]
    if arch == 'aarch64':
        firmware = local_path(cfg['firmware'])
        if check and not firmware.is_file():
            raise RuntimeError('ARM64 firmware not found; set firmware in configuration')
        cmd += ['-bios', str(firmware)]
    if cfg.get('qmp_socket'):
        # Host-only control channel, never attached/mounted inside the guest.
        cmd += ['-qmp', f'unix:{qemu_path(local_path(cfg["qmp_socket"]))},server=on,wait=off']
    if cfg.get('qmp_pipe'):
        if system != 'Windows' or not re.fullmatch(r'claude-isolate-[0-9a-f]{32}', cfg['qmp_pipe']):
            raise ValueError('Invalid Windows control pipe')
        cmd += ['-qmp', 'pipe:' + cfg['qmp_pipe']]
    if cfg.get('boot_log'):
        cmd[cmd.index('-serial') + 1] = 'file:' + qemu_path(local_path(cfg['boot_log']))
    # No shared folders, SPICE agent, clipboard channel, host sockets,
    # microphone, webcam, USB passthrough or forwarded incoming ports.
    return cmd


def cloud_config():
    def file(path, content, permissions='0644'):
        return dict(path=path, content=content, owner='root:root', permissions=permissions)
    files = [
        file('/etc/systemd/system/claude-setup.service', '''[Unit]
Description=Install isolated desktop automatically
Wants=network-online.target
After=network-online.target
ConditionPathExists=!/var/lib/claude-isolation-ready
StartLimitIntervalSec=0
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/bootstrap-claude
Restart=on-failure
RestartSec=15
StandardOutput=journal+console
StandardError=journal+console
[Install]
WantedBy=multi-user.target
'''),
        file('/etc/claude-isolation.nft', (ROOT / 'guest/firewall.nft').read_text()),
        file('/usr/local/sbin/claude-repositories', (ROOT / 'guest/repositories.sh').read_text(), '0700'),
        file('/usr/local/sbin/bootstrap-claude', (ROOT / 'guest/bootstrap.sh').read_text(), '0700'),
        file('/usr/local/bin/claude-isolated', (ROOT / 'guest/launch.sh').read_text(), '0755'),
        file('/usr/local/sbin/claude-display-install', (ROOT / 'guest/display-setup.sh').read_text(), '0700'),
        file('/etc/lightdm/lightdm.conf.d/50-isolated.conf',
             '[Seat:*]\nautologin-user=claude\nautologin-user-timeout=0\nuser-session=xfce\n'),
        file('/etc/environment', 'http_proxy="http://10.0.2.100:7890"\n'
             'https_proxy="http://10.0.2.100:7890"\n'
             'HTTP_PROXY="http://10.0.2.100:7890"\nHTTPS_PROXY="http://10.0.2.100:7890"\n'),
        file('/etc/apt/apt.conf.d/80-isolated-proxy',
             'Acquire::http::Proxy "http://10.0.2.100:7890";\n'
             'Acquire::https::Proxy "http://10.0.2.100:7890";\n'
             'Acquire::Retries "10";\nAcquire::http::Timeout "20";\nAcquire::https::Timeout "20";\n')]
    data = dict(hostname='isolated-desktop', manage_etc_hosts=True,
                disable_root=True, ssh_pwauth=False,
                users=[{'name': 'claude', 'gecos': 'Desktop user',
                        'groups': ['video'], 'shell': '/bin/bash', 'lock_passwd': True}],
                apt=dict(http_proxy='http://10.0.2.100:7890',
                         https_proxy='http://10.0.2.100:7890'),
                package_update=False,
                write_files=files,
                runcmd=[['systemctl', 'mask', '--now', 'ssh.service', 'ssh.socket'],
                        ['systemctl', 'daemon-reload'],
                        ['systemctl', 'enable', '--now', 'claude-setup.service']])
    # JSON is valid YAML, including for cloud-init. No YAML dependency needed.
    return '#cloud-config\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def check_proxy(port):
    with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
        s.sendall(b'CONNECT downloads.claude.ai:443 HTTP/1.1\r\n'
                  b'Host: downloads.claude.ai:443\r\n\r\n')
        line = s.recv(2048).split(b'\r\n')[0]
        if not line.startswith(b'HTTP/') or b' 200 ' not in line:
            raise RuntimeError('Local port is not a working unauthenticated HTTP CONNECT proxy')


def relay(port=None, mode='proxy', web_access='services'):
    if mode not in ('system', 'proxy'):
        raise ValueError('Unsupported network mode')
    if web_access not in ('services', 'public') or (web_access == 'public' and mode != 'system'):
        raise ValueError('Public browsing requires system VPN mode')
    if mode == 'proxy' and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError('Invalid proxy port')
    # For plain HTTP, force one upstream request per connection. CONNECT
    # permits a TLS tunnel only to the validated destination.
    import select
    if os.name == 'nt':
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    header = bytearray()
    deadline = time.monotonic() + 10
    while b'\r\n\r\n' not in header and len(header) < 32768:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (os.name != 'nt' and not select.select([sys.stdin.buffer], [], [], remaining)[0]):
            return
        chunk = os.read(0, 1)
        if not chunk:
            return
        header.extend(chunk)
    if b'\r\n\r\n' not in header or not allowed_request(bytes(header), web_access):
        sys.stdout.buffer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
        sys.stdout.buffer.flush()
        return
    lease = os.environ.get('CLAUDE_NETWORK_LEASE')
    if not network_guard.permitted(lease):
        sys.stdout.buffer.write(b'HTTP/1.1 503 Network blocked\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
        sys.stdout.buffer.flush()
        return
    first_line = bytes(header).split(b'\r\n', 1)[0]
    target = first_line.decode('ascii').split(' ')[1]
    host_header = urlsplit('//' + target).netloc if header.startswith(b'CONNECT ') else urlsplit(target).netloc
    if not header.startswith(b'CONNECT '):
        # Close prevents a second HTTP request from using this validated
        # connection. Do not accept request bodies or pipelining.
        forwarded_headers = http_headers(bytes(header), host_header)
        header = bytearray(first_line + b'\r\n' + forwarded_headers)
    else:
        header = bytearray(first_line + b'\r\nHost: ' + host_header.encode('ascii') + b'\r\n\r\n')
    destination = urlsplit('//' + target) if header.startswith(b'CONNECT ') else urlsplit(target)
    upstream = (socket.create_connection(('127.0.0.1', port), timeout=3) if mode == 'proxy'
                else open_public(destination.hostname, destination.port or 80, timeout=3))
    with upstream as s:
        # Recheck after connect: permission may have expired during connection.
        if not network_guard.permitted(lease):
            return
        is_tunnel = header.startswith(b'CONNECT ')
        if mode == 'system' and is_tunnel:
            # We relay encrypted bytes; TLS remains between guest and server.
            sys.stdout.buffer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            sys.stdout.buffer.flush()
        elif mode == 'system':
            path = destination.path or '/'
            if destination.query:
                path += '?' + destination.query
            method = first_line.decode('ascii').split(' ')[0]
            s.sendall(f'{method} {path} HTTP/1.1\r\n'.encode('ascii') + forwarded_headers)
        else:
            s.sendall(header)
        s.settimeout(None)
        finished = threading.Event()
        def watch_lease():
            while not finished.wait(network_guard.RELAY_INTERVAL):
                if not network_guard.permitted(lease):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
        threading.Thread(target=watch_lease, daemon=True).start()
        def upload():
            try:
                while True:
                    chunk = os.read(0, 65536)
                    if not chunk or not network_guard.permitted(lease):
                        break
                    s.sendall(chunk)
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        if header.startswith(b'CONNECT '):
            threading.Thread(target=upload, daemon=True).start()
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk or not network_guard.permitted(lease):
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        finally:
            finished.set()


def prepare(cfg, base, digest):
    base = Path(base).resolve()
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest.lower()):
        raise ValueError('A SHA256 verified against Ubuntu signed checksums is required')
    h = hashlib.sha256()
    with base.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    if h.hexdigest() != digest.lower():
        raise ValueError('Base image checksum mismatch')
    disk, seed = local_path(cfg['disk']), local_path(cfg['seed'])
    if disk.exists() or seed.exists():
        raise RuntimeError('Refusing to overwrite an existing environment')
    disk.parent.mkdir(parents=True, exist_ok=True)
    seed.parent.mkdir(parents=True, exist_ok=True)
    # Build both files privately. A failed conversion/ISO creation leaves no
    # half-installed environment; hard links publish without overwriting a race.
    with tempfile.TemporaryDirectory(prefix='.prepare-', dir=disk.parent) as staging, \
         tempfile.TemporaryDirectory(prefix='.seed-', dir=seed.parent) as seed_staging:
        staged_disk = Path(staging) / 'desktop.qcow2'
        staged_seed = Path(seed_staging) / 'seed.iso'
        seed_dir = Path(seed_staging) / 'files'
        seed_dir.mkdir()
        (seed_dir / 'user-data').write_text(cloud_config(), encoding='utf-8', newline='\n')
        (seed_dir / 'meta-data').write_text('instance-id: claude-isolation-v1\nlocal-hostname: isolated-desktop\n',
                                          encoding='utf-8', newline='\n')
        image_tool = tool('qemu-img')
        subprocess.run([image_tool, 'convert', '-f', 'qcow2', '-O', 'qcow2', str(base), str(staged_disk)], check=True)
        subprocess.run([image_tool, 'resize', str(staged_disk), '64G'], check=True)
        if platform.system() == 'Darwin':
            subprocess.run([tool('hdiutil'), 'makehybrid', '-iso', '-joliet',
                            '-default-volume-name', 'cidata', '-o', str(staged_seed), str(seed_dir)], check=True)
        elif platform.system() == 'Windows':
            # The small bundled ISO writer removes the mkisofs dependency.
            from windows.backend import seed_iso
            seed_iso(seed_dir, staged_seed)
        else:
            iso = shutil.which('genisoimage') or shutil.which('mkisofs')
            if not iso:
                raise RuntimeError('Install genisoimage/mkisofs to create the cloud-init seed ISO')
            subprocess.run([iso, '-output', str(staged_seed), '-volid', 'cidata', '-joliet',
                            '-rock', str(seed_dir)], check=True)
        if os.name != 'nt':
            staged_disk.chmod(0o600)
            staged_seed.chmod(0o600)
        os.link(staged_disk, disk)
        try:
            os.link(staged_seed, seed)
        except BaseException:
            disk.unlink()
            raise
    print('Environment prepared. No account credentials were copied.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'prepare', 'start', 'plan', 'relay', 'country'])
    parser.add_argument('--config', default=str(ROOT / 'environment.json'))
    parser.add_argument('--base')
    parser.add_argument('--sha256')
    parser.add_argument('--port', type=int)
    parser.add_argument('--mode', choices=['system', 'proxy'], default='proxy')
    parser.add_argument('--web-access', choices=['services', 'public'], default='services')
    args = parser.parse_args()
    try:
        if args.action == 'relay':
            relay(args.port, args.mode, args.web_access)
            return
        cfg = load_config(args.config)
        if args.action == 'country':
            result = network_guard.probe(cfg.get('proxy_port'), cfg['network_mode'])
            print(json.dumps(result, ensure_ascii=False))
            if not result['allowed']:
                sys.exit(1)
        elif args.action == 'plan':
            print(json.dumps(command(cfg, check=False), indent=2))
        elif args.action == 'prepare':
            if not args.base or not args.sha256:
                raise ValueError('--base and --sha256 are required')
            prepare(cfg, args.base, args.sha256)
        else:
            cmd = command(cfg)
            if cfg['network_mode'] == 'proxy':
                check_proxy(cfg['proxy_port'])
            if args.action == 'check':
                print('QEMU and firmware available; live VPN routing/isolation not yet verified.')
            else:
                for key in ('disk', 'seed'):
                    if not local_path(cfg[key]).is_file():
                        raise RuntimeError('Run prepare first')
                with exclusive(local_path(cfg['disk']).with_suffix('.launch.lock')), tempfile.TemporaryDirectory(prefix='claude-network-') as directory:
                    lease = Path(directory) / 'lease.json'
                    status_path = cfg.get('network_status')
                    if status_path:
                        Path(status_path).with_suffix('.revoked').unlink(missing_ok=True)
                        Path(status_path).with_suffix('.paused').unlink(missing_ok=True)
                    stop = threading.Event()
                    events = network_guard.NetworkEvents(lease, status_path)
                    initial_generation = events.snapshot()
                    watcher = None
                    routes = None
                    def interrupted(signum, frame):
                        raise KeyboardInterrupt('Launcher interrupted')
                    previous_term = signal.signal(signal.SIGTERM, interrupted)
                    try:
                        # Subscribe BEFORE checking IP, so a route change during
                        # startup cannot produce an unobserved allowed session.
                        if platform.system() == 'Darwin':
                            routes = RouteWatcher(events.pause, failure=lambda reason: network_guard.revoke(lease, status_path, reason))
                            routes.start()
                        startup_deadline = time.monotonic() + 30
                        while True:
                            initial_generation = events.snapshot()
                            result = network_guard.probe(cfg.get('proxy_port'), cfg['network_mode'])
                            if not result['allowed']:
                                raise RuntimeError(result['reason'])
                            if events.verified(initial_generation, result) and network_guard.permitted(lease):
                                break
                            if time.monotonic() >= startup_deadline:
                                raise RuntimeError('Сеть меняется во время запуска. Дождитесь стабильного подключения.')
                            time.sleep(.2)
                        watcher = threading.Thread(target=network_guard.monitor,
                            args=(cfg.get('proxy_port'), lease, stop, cfg['network_mode'], result, status_path, events), daemon=True)
                        watcher.start()
                        env = dict(os.environ, CLAUDE_NETWORK_LEASE=str(lease))
                        if status_path:
                            env['CLAUDE_NETWORK_REVOKE'] = str(Path(status_path).with_suffix('.revoked'))
                        env.pop('IPINFO_TOKEN', None)
                        proc = subprocess.Popen(cmd, env=env)
                        print(json.dumps({'message': 'Linux запускается', 'running': True}, ensure_ascii=False), flush=True)
                        try:
                            proc.wait()
                            if proc.returncode:
                                raise subprocess.CalledProcessError(proc.returncode, cmd)
                        except BaseException:
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait()
                            raise
                    finally:
                        network_guard.revoke(lease, status_path, 'Среда остановлена')
                        stop.set()
                        events.wake.set()
                        if routes:
                            routes.close()
                        if watcher:
                            watcher.join(timeout=12)
                        if status_path:
                            network_guard.publish(status_path, {'allowed': False, 'reason': 'Среда остановлена'})
                        signal.signal(signal.SIGTERM, previous_term)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as e:
        print(f'Cannot continue: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
