#!/usr/bin/env python3
"""Native macOS UI backend. JSON status output contains no account secrets."""
import argparse
import hashlib
import fcntl
import time
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import shutil
import socket
import subprocess
import sys
sys.dont_write_bytecode = True
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import environment
import network_guard
import vm_bundle


def emit(message, **extra):
    print(json.dumps(dict(message=message, **extra), ensure_ascii=False), flush=True)


def write_config(path, cfg):
    fd, name = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(cfg, output, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def config(data):
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    cfg_path = data / 'environment.json'
    if not cfg_path.exists():
        cfg = json.loads((ROOT / 'environment.example.json').read_text())
        cfg['arch'] = 'aarch64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x86_64'
        if cfg['arch'] == 'x86_64':
            cfg.pop('firmware', None)
        cfg.update(disk=str(data / 'desktop.qcow2'), seed=str(data / 'seed.iso'),
                   qmp_socket=str(data / 'control.sock'), boot_log=str(data / 'boot.log'))
        write_config(cfg_path, cfg)
    # Unix socket paths are limited to 104 bytes on macOS.
    control = Path('/private/tmp') / ('claude-env-' + str(os.getuid()) + '-' + hashlib.sha256(str(data).encode()).hexdigest()[:12])
    control.mkdir(mode=0o700, exist_ok=True)
    if control.is_symlink() or control.stat().st_uid != os.getuid():
        raise RuntimeError('Небезопасная папка управления')
    control.chmod(0o700)
    cfg = json.loads(cfg_path.read_text())
    if cfg.get('qmp_socket') != str(control / 'control.sock') or cfg.get('network_status') != str(control / 'network.json'):
        cfg['qmp_socket'] = str(control / 'control.sock')
        cfg['network_status'] = str(control / 'network.json')
        write_config(cfg_path, cfg)
    return cfg_path, environment.load_config(cfg_path)


def qmp(cfg, execute, arguments=None):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(cfg['qmp_socket'])
        f = sock.makefile('rwb')
        json.loads(f.readline())
        for command in ({'execute': 'qmp_capabilities'},
                        dict(execute=execute, **({'arguments': arguments} if arguments else {}))):
            f.write(json.dumps(command).encode() + b'\n'); f.flush()
            while True:
                response = json.loads(f.readline())
                if 'error' in response:
                    raise RuntimeError(str(response['error']))
                if 'return' in response:
                    break
        return response['return']


def network_state(cfg):
    try:
        state = json.loads(Path(cfg['network_status']).read_text())
        state['allowed'] = network_guard.permitted(cfg['network_status'])
        marker = Path(cfg['network_status']).with_suffix('.revoked')
        if marker.exists():
            state.update(allowed=False, locked=True, reason=marker.read_text() or 'Сеть закрыта')
        paused = Path(cfg['network_status']).with_suffix('.paused')
        if paused.exists() and not state.get('locked'):
            state.update(allowed=False, reason='Проверяю подключение после сетевого события…')
        if not state['allowed'] and not state.get('locked') and not paused.exists() and state.get('expires', 0):
            state['reason'] = 'Разрешение на сеть истекло или среда остановлена'
        return state
    except (OSError, ValueError, KeyError):
        return {'allowed': False, 'reason': 'Состояние защиты пока неизвестно'}


def fetch(url, target):
    if not url.startswith('https://'):
        raise ValueError('HTTPS required')
    with urllib.request.urlopen(url, timeout=30) as response, target.open('wb') as out:  # nosec B310 # Fixed HTTPS vendor URLs; schemes checked before/after redirect.
        if not response.url.startswith('https://'):
            raise ValueError('Insecure redirect')
        shutil.copyfileobj(response, out, length=1024 * 1024)


def prepare(data, cfg):
    if platform.system() == 'Darwin':
        original = environment.tool('qemu-system-' + cfg['arch'])
        cfg['qemu_executable'] = vm_bundle.provision(data, original)
        cfg['qemu_data_dir'] = str(Path(original).resolve().parent.parent / 'share/qemu')
        write_config(data / 'environment.json', cfg)
    environment.command(cfg)  # fail before downloading if QEMU is missing
    gpg = environment.tool('gpg')
    if Path(cfg['disk']).is_file() and Path(cfg['seed']).is_file():
        emit('Среда уже подготовлена', ready=True)
        return
    download = data / 'downloads'
    download.mkdir(exist_ok=True)
    filename = 'noble-server-cloudimg-' + ('arm64' if cfg['arch'] == 'aarch64' else 'amd64') + '.img'
    base = download / filename
    checksums, signature = download / 'SHA256SUMS', download / 'SHA256SUMS.gpg'
    url = 'https://cloud-images.ubuntu.com/noble/current/'
    emit('Загрузка Ubuntu — примерно 600 МБ')
    if not base.exists():
        partial = base.with_suffix('.partial')
        fetch(url + filename, partial)
        partial.replace(base)
    fetch(url + 'SHA256SUMS', checksums)
    fetch(url + 'SHA256SUMS.gpg', signature)
    emit('Проверка подписи Ubuntu')
    home = download / 'verification'
    home.mkdir(exist_ok=True); home.chmod(0o700)
    fingerprint = 'D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81'
    key = download / 'ubuntu-signing-key.asc'
    fetch('https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x' + fingerprint, key)
    common = [gpg, '--homedir', str(home), '--batch', '--no-autostart']
    subprocess.run(common + ['--import', str(key)], check=True, capture_output=True)
    verified = subprocess.run(common + ['--status-fd', '1', '--verify', str(signature), str(checksums)],
                              check=True, capture_output=True, text=True)
    if not any(line.startswith('[GNUPG:] VALIDSIG ' + fingerprint + ' ') for line in verified.stdout.splitlines()):
        raise RuntimeError('Не подтверждён официальный ключ подписи Ubuntu')
    digest = next(line.split()[0] for line in checksums.read_text().splitlines()
                  if line.split()[-1].lstrip('*') == filename)
    emit('Создание отдельного диска Linux')
    environment.prepare(cfg, base, digest)
    emit('Среда подготовлена. Можно запускать.', ready=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['status', 'country', 'prepare', 'start', 'restart', 'stop', 'screenshot'])
    parser.add_argument('--data', required=True)
    args = parser.parse_args()
    data = Path(args.data).resolve()
    try:
        cfg_path, cfg = config(data)
        if args.action == 'status':
            running = False
            try:
                running = qmp(cfg, 'query-status')['running']
            except (OSError, ValueError, RuntimeError):
                pass
            ready = Path(cfg['disk']).is_file() and Path(cfg['seed']).is_file()
            boot = Path(cfg['boot_log']).read_text(errors='replace') if running and Path(cfg['boot_log']).exists() else ''
            if 'CLAUDE-ISOLATION: desktop-ready' in boot and not cfg.get('desktop_verified'):
                cfg['desktop_verified'] = True
                write_config(cfg_path, cfg)
            message = 'Рабочий стол готов' if 'CLAUDE-ISOLATION: desktop-ready' in boot else 'Linux запущен — идёт подготовка рабочего стола'
            if running and cfg.get('desktop_verified'):
                message = 'Linux запущен — рабочий стол подготовлен'
            if running and not cfg.get('desktop_verified') and 'CLAUDE-ISOLATION: desktop-ready' not in boot:
                downloads = re.findall(r'Get:(\d+) ', boot)
                totals = re.findall(r'(\d+) newly installed', boot)
                if downloads and totals and int(totals[-1]) > 0:
                    message = 'Установка рабочего стола: загрузка ' + downloads[-1] + ' из ' + totals[-1] + ' пакетов'
                if boot.rfind('Setting up ') > boot.rfind('Get:'):
                    message = 'Настройка компонентов Linux и клиента Claude'
                if boot.rfind('CLAUDE-ISOLATION: setup-retrying') > boot.rfind('CLAUDE-ISOLATION: desktop-ready') and 'CLAUDE-ISOLATION: setup-retrying' in boot:
                    message = 'Временная ошибка установки — автоматически повторяю'
                if 'CLAUDE-ISOLATION: FAILURE' in boot:
                    message = 'Установка не завершена — проверьте журнал загрузки'
            emit(message if running else 'Готова к запуску' if ready else 'Нужно подготовить среду',
                 running=running, ready=ready, network=network_state(cfg) if running else None)
        elif args.action == 'country':
            try:
                result = network_guard.probe(cfg.get('proxy_port'), cfg['network_mode'])
            except network_guard.ProbeUnavailable as error:
                result = {'allowed': False, 'reason': str(error)}
            except Exception:
                result = {'allowed': False, 'reason': 'Проверка выхода недоступна'}
            emit('Внешний адрес проверен' if result['allowed'] else 'Проверка сети не пройдена', **result)
        elif args.action == 'prepare':
            prepare(data, cfg)
        elif args.action in ('start', 'restart'):
            if args.action == 'restart':
                network_guard.revoke(None, cfg['network_status'], 'Перезапуск среды')
                emit('Закрываю сеть и корректно завершаю Linux…', network=network_state(cfg))
                try:
                    qmp(cfg, 'system_powerdown')
                except (FileNotFoundError, ConnectionRefusedError):
                    pass
                deadline = time.monotonic() + 60
                while True:
                    try:
                        qmp(cfg, 'query-status')
                    except (FileNotFoundError, ConnectionRefusedError):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Linux ещё работает. Завершите работу в гостевом окне и повторите перезапуск.')
                    time.sleep(.2)
            else:
                try:
                    if qmp(cfg, 'query-status')['running']:
                        emit('Среда уже запущена', running=True)
                        return
                except (OSError, ValueError, RuntimeError):
                    pass
            # Wait for the old launcher to finish its guard cleanup before a
            # new session can clear the one-way block and own the same disk.
            with (data / 'session.lock').open('a') as lock:
                deadline = time.monotonic() + (15 if args.action == 'restart' else 1)
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Предыдущий запуск ещё завершается. Повторите через несколько секунд.')
                        time.sleep(.1)
                prepare(data, cfg)
                emit('Проверяю сеть и запускаю Linux…')
                sys.argv = ['environment.py', 'start', '--config', str(cfg_path)]
                environment.main()
                emit('Среда остановлена', running=False)
        elif args.action == 'stop':
            network_guard.revoke(None, cfg['network_status'], 'Остановка среды')
            qmp(cfg, 'system_powerdown')
            emit('Linux получил запрос на завершение работы.', network=network_state(cfg))
        elif args.action == 'screenshot':
            qmp(cfg, 'screendump', {'filename': str(data / 'screen.ppm')})
            emit('Снимок гостевого экрана сохранён')
    except Exception as e:
        emit('Не удалось выполнить действие: ' + str(e), error=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
