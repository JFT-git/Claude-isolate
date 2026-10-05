"""Windows setup and VM control; invoked by the packaged console worker."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import subprocess
import tempfile
import time
import uuid

import environment
import network_guard
import ubuntu_image
from session_lock import exclusive
from windows import gnupg

VERSION = '0.3.11'
GUEST_GATEWAY_VERSION = 2
ACCELERATION_MODES = ('auto', 'tcg', 'whpx')


def emit(message, **fields):
    print(json.dumps(dict(message=message, **fields), ensure_ascii=False), flush=True)


def write_config(path, cfg):
    fd, temporary = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(cfg, output, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def config(data):
    data.mkdir(parents=True, exist_ok=True)
    path = data / 'environment.json'
    if not path.exists():
        write_config(path, dict(arch='x86_64', network_mode='system', web_access='public',
                               memory_mb=3072, cpus=2, disk=str(data / 'desktop.qcow2'),
                               seed=str(data / 'seed.iso'), boot_log=str(data / 'boot.log'),
                               qmp_pipe='claude-isolate-' + uuid.uuid4().hex,
                               network_status=str(data / 'network.json'),
                               guest_gateway_version=GUEST_GATEWAY_VERSION))
    return path, environment.load_config(path)


def seed_iso(directory, destination):
    import pycdlib
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, vol_ident='cidata', joliet=3, rock_ridge='1.09')
    try:
        for index, path in enumerate(sorted(directory.iterdir())):
            if path.is_file():
                iso.add_file(str(path), iso_path=f'/FILE{index:04d}.;1',
                             rr_name=path.name, joliet_path='/' + path.name)
        iso.write(str(destination))
    finally:
        iso.close()


def find_tool(name):
    found = shutil.which(name)
    if name == 'gpg' and found and 'git/usr/bin/' in str(found).replace('\\', '/').lower():
        # Git's MSYS build is not the native GnuPG package installed by winget.
        found = None
    if found:
        return Path(found)
    folders = [Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'qemu',
               Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'GnuPG/bin',
               Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'GnuPG/bin',
               Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Gpg4win/bin',
               Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Gpg4win/bin']
    return next((folder / (name + '.exe') for folder in folders
                 if (folder / (name + '.exe')).is_file()), None)


def dependencies(data, cfg):
    if cfg.get('qemu_executable') and Path(cfg['qemu_executable']).is_file():
        os.environ['PATH'] = str(Path(cfg['qemu_executable']).parent) + os.pathsep + os.environ.get('PATH', '')
    private_gpg = data / 'tools' / ('GnuPG-' + gnupg.VERSION) / 'bin'
    if (private_gpg / 'gpg.exe').is_file():
        os.environ['PATH'] = str(private_gpg) + os.pathsep + os.environ.get('PATH', '')
    packages = [('qemu-system-x86_64', 'SoftwareFreedomConservancy.QEMU')]
    for name, package in packages:
        if find_tool(name):
            continue
        winget = shutil.which('winget')
        if not winget:
            raise RuntimeError('Для автоматической установки нужен «Установщик приложений» '
                               'Microsoft (App Installer). Обновите его через Microsoft Store '
                               'или установите QEMU и GnuPG вручную и повторите запуск.')
        emit('Установка ' + ('QEMU' if name.startswith('qemu') else 'GnuPG') +
             ' — подтвердите системный запрос Windows, если он появится.')
        subprocess.run([winget, 'install', '--exact', '--id', package, '--source', 'winget',
                        '--silent', '--accept-source-agreements', '--accept-package-agreements',
                        '--disable-interactivity'], check=True, timeout=900)
        if not find_tool(name):
            raise RuntimeError('Установленный компонент не найден: ' + name)
    if not find_tool('gpg'):
        emit('Загрузка отдельного GnuPG и проверка его SHA256…')
        installed = gnupg.install(data)
        os.environ['PATH'] = str(installed.parent) + os.pathsep + os.environ.get('PATH', '')
    tools = [find_tool(name) for name in ('qemu-system-x86_64', 'qemu-img', 'gpg')]
    if any(tool is None for tool in tools):
        raise RuntimeError('Установка QEMU неполная: отсутствует qemu-img.exe.')
    os.environ['PATH'] = os.pathsep.join(dict.fromkeys(str(p.parent) for p in tools)) + os.pathsep + os.environ.get('PATH', '')
    cfg['qemu_executable'] = str(tools[0])
    write_config(data / 'environment.json', cfg)


def acceleration():
    if os.name != 'nt':
        return 'tcg'
    try:
        library = ctypes.WinDLL('WinHvPlatform.dll')
        present, written = ctypes.c_int(), ctypes.c_uint()
        result = library.WHvGetCapability(0, ctypes.byref(present), ctypes.sizeof(present), ctypes.byref(written))
        return 'whpx' if result == 0 and present.value else 'tcg'
    except (OSError, AttributeError):
        return 'tcg'


def select_acceleration(cfg):
    mode = cfg.get('acceleration_mode', 'auto')
    if mode not in ACCELERATION_MODES:
        raise ValueError('Неверный режим ускорения')
    if mode != 'auto':
        return mode
    return 'tcg' if cfg.get('whpx_failed') else acceleration()


def start_environment(path, cfg):
    cfg['accelerator'] = select_acceleration(cfg)
    write_config(path, cfg)
    emit('Проверяю подключение и запускаю Linux…', accelerator=cfg['accelerator'])
    sys.argv = [sys.argv[0], 'start', '--config', str(path)]
    started = time.monotonic()
    try:
        environment.main(raise_errors=True)
    except subprocess.CalledProcessError as error:
        # environment has already reaped QEMU, closed the private SSH bridge,
        # revoked the lease and released the disk lock before we retry.
        if (cfg['accelerator'] != 'whpx' or cfg.get('acceleration_mode', 'auto') != 'auto'
                or time.monotonic() - started > 180):
            raise
        if error.network_locked:
            raise RuntimeError('Сеть была заблокирована во время запуска. '
                               'Автоматический перезапуск отменён; проверьте VPN и перезапустите среду вручную.') from error
        cfg.update(accelerator='tcg', whpx_failed=True,
                   whpx_failure_code=error.returncode)
        write_config(path, cfg)
        emit('Аппаратное ускорение Windows завершилось с ошибкой '
             f'0x{error.returncode & 0xffffffff:08X}. Перехожу на совместимый режим; '
             'он медленнее. Диск Linux сохранён.', accelerator='tcg', running=False)
        # A fresh lease must still use the original verified exit IP. An
        # automatic recovery never silently accepts a VPN/exit change.
        environment.main(raise_errors=True, expected_exit_ip=error.initial_exit_ip)


def prepare(data, cfg):
    disk, seed = Path(cfg['disk']), Path(cfg['seed'])
    if disk.is_file() and seed.is_file():
        if cfg.get('guest_gateway_version') == GUEST_GATEWAY_VERSION:
            return
        # Old installed guests disabled cloud-init before persisting the
        # serial gateway. Replacing their seed cannot repair that root disk.
        # Prepare a separate corrected guest and keep the old disk/seed and
        # configuration intact. Never silently erase files or copy logins.
        replacement = dict(cfg, disk=str(data / f'desktop-gateway-v{GUEST_GATEWAY_VERSION}.qcow2'),
                           seed=str(data / f'seed-gateway-v{GUEST_GATEWAY_VERSION}.iso'),
                           guest_gateway_version=GUEST_GATEWAY_VERSION)
        write_config(data / f'environment-before-gateway-v{GUEST_GATEWAY_VERSION}-upgrade.json', cfg)
        original_backup = data / 'environment-before-gateway-upgrade.json'
        if not original_backup.exists():
            write_config(original_backup, cfg)
        emit('Обновляю Linux для исправления сети после перезапуска. Старый диск сохранён; аккаунты не копируются.')
        if not (Path(replacement['disk']).is_file() and Path(replacement['seed']).is_file()):
            base, digest = ubuntu_image.download(data / 'downloads', cfg['arch'], str(find_tool('gpg')))
            environment.prepare(replacement, base, digest)
            base.unlink(missing_ok=True)
        write_config(data / 'environment.json', replacement)
        cfg.update(replacement)
        emit('Исправленная среда подготовлена. Приложения установятся автоматически.')
        return
    if disk.exists() or seed.exists():
        raise RuntimeError('Найдена неполная среда. Существующий диск автоматически не перезаписывается.')
    emit('Загрузка Ubuntu и проверка подписи образа…')
    base, digest = ubuntu_image.download(data / 'downloads', cfg['arch'], str(find_tool('gpg')))
    emit('Создание отдельного диска Linux…')
    environment.prepare(cfg, base, digest)
    base.unlink(missing_ok=True)
    emit('Среда подготовлена. При первой загрузке Linux установит рабочий стол и приложения.')


def qmp(cfg, execute):
    from windows.control import request
    return request(cfg, execute)


def status(cfg, running=False):
    state = {'ready': Path(cfg['disk']).is_file() and Path(cfg['seed']).is_file(),
             'running': running, 'memory_mb': cfg['memory_mb'], 'cpus': cfg['cpus']}
    if not running:
        state['message'] = 'Готова к запуску' if state['ready'] else 'Нужно подготовить среду'
        return state
    try:
        with Path(cfg['boot_log']).open('rb') as source:
            source.seek(0, os.SEEK_END)
            source.seek(max(0, source.tell() - 262144))
            boot = source.read().decode('utf-8', errors='replace')
    except OSError:
        boot = ''
    # The installation marker is emitted only on the first boot. Later boots
    # already have the desktop and must not show installation indefinitely.
    plain_boot = re.sub(r'\x1b\[[0-9;]*m', '', boot)
    desktop_ready = ('CLAUDE-ISOLATION: desktop-ready' in plain_boot
                     or 'Started lightdm.service - Light Display Manager.' in plain_boot)
    state['message'] = ('Рабочий стол готов' if desktop_ready
                        else 'Linux запускается и устанавливает компоненты…')
    if 'CLAUDE-ISOLATION: FAILURE' in boot:
        state['message'] = 'Установка не завершена — проверьте журнал'
    try:
        network = network_guard.read_state(cfg['network_status'])
        network['allowed'] = network_guard.permitted(cfg['network_status'])
        for suffix in ('.revoked', '.paused'):
            marker = Path(cfg['network_status']).with_suffix(suffix)
            if marker.exists():
                network.update(allowed=False, reason=marker.read_text(encoding='utf-8'))
                break
        state['network'] = network
    except (OSError, ValueError, TypeError):
        state['network'] = {'allowed': False, 'reason': 'Ожидаю проверку сети…'}
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='Claude Isolate ' + VERSION)
    parser.add_argument('action', choices=['prepare', 'start', 'stop', 'economy', 'standard',
                                         'accel-auto', 'accel-tcg', 'accel-whpx'])
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--start-gate', action='store_true')
    args = parser.parse_args()
    try:
        if args.start_gate and sys.stdin.readline().strip() != 'GO':
            raise RuntimeError('Запуск отменён контроллером')
        data = args.data.resolve()
        data.mkdir(parents=True, exist_ok=True)
        if args.action == 'stop':
            _, cfg = config(data)
            network_guard.revoke(None, cfg['network_status'], 'Завершение Linux')
            qmp(cfg, 'system_powerdown')
            emit('Linux завершает работу…')
            return
        with exclusive(data / 'session.lock'):
            path, cfg = config(data)
            if args.action.startswith('accel-'):
                cfg['acceleration_mode'] = args.action.removeprefix('accel-')
                cfg.pop('whpx_failed', None)
                cfg.pop('whpx_failure_code', None)
                write_config(path, cfg)
                emit('Режим запуска сохранён')
                return
            if args.action in ('economy', 'standard'):
                cfg.update(memory_mb=3072 if args.action == 'economy' else 6144,
                           cpus=2 if args.action == 'economy' else 4)
                write_config(path, cfg)
                emit('Ресурсы сохранены')
                return
            dependencies(data, cfg)
            prepare(data, cfg)
            if args.action == 'start':
                emit('Claude Isolate ' + VERSION, version=VERSION,
                     windows=platform.version(), machine=platform.machine())
                start_environment(path, cfg)
                emit('Среда остановлена')
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        emit(str(error), error=True)
        raise SystemExit(1)
