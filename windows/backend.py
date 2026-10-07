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
import release_image
from session_lock import exclusive
from windows import gnupg

VERSION = '0.3.16'
GUEST_GATEWAY_VERSION = 2
ACCELERATION_MODES = ('auto', 'tcg', 'whpx')
RESOURCE_PROFILES = {'minimal': (1024, 1), 'economy': (3072, 2), 'standard': (6144, 4)}


def emit(message, **fields):
    print(json.dumps(dict(message=message, **fields), ensure_ascii=False), flush=True)


def write_config(path, cfg):
    fd, temporary = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(cfg, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def host_memory():
    if os.name != 'nt':
        return None
    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', ctypes.c_uint32), ('load', ctypes.c_uint32)] + [
            (name, ctypes.c_uint64) for name in ('total', 'available', 'page_total',
                'page_available', 'virtual_total', 'virtual_available', 'extended')]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    api.GlobalMemoryStatusEx.restype = ctypes.c_int
    if not api.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return status.total // 1048576, status.available // 1048576


def automatic_resources():
    memory = host_memory()
    # Prefer leaving 1 GiB for Windows. Under memory pressure permit a small
    # guest and warn; Windows/QEMU decide whether allocation can succeed.
    budget = min(3072, memory[0] // 2, memory[1] - 1024) if memory else 3072
    allocated = max(1024, budget // 256 * 256)
    cpus = 1 if allocated < 2048 else min(2, max(1, (os.cpu_count() or 2) - 1))
    return dict(memory_mb=allocated, cpus=cpus)


def warn_memory(memory_mb, reserve=512, purpose='Linux'):
    memory = host_memory()
    if memory and memory[1] < memory_mb + reserve:
        emit(f'Мало свободной памяти: {memory[1]} МБ, для {purpose} выделено {memory_mb} МБ. '
             'Запуск разрешён; возможна медленная работа или ошибка выделения памяти Windows/QEMU.',
             warning=True, memory_warning=True)


def maintenance_memory():
    memory = host_memory()
    budget = memory[1] - 512 if memory else 1536
    return max(768, min(1536, budget // 256 * 256))


def configure_resources(path, cfg):
    if cfg.get('resources_mode') == 'auto':
        cfg.update(automatic_resources())
    reserve = 1024 if cfg.get('resources_mode') == 'auto' else 512
    warn_memory(cfg['memory_mb'], reserve)
    write_config(path, cfg)


def config(data):
    data.mkdir(parents=True, exist_ok=True)
    path = data / 'environment.json'
    if not path.exists():
        write_config(path, dict(arch='x86_64', network_mode='system', web_access='public',
                               **automatic_resources(), resources_mode='auto', disk=str(data / 'desktop.qcow2'),
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


def runtime_directory():
    return ((Path(sys.executable).parent if getattr(sys, 'frozen', False)
             else environment.ROOT / 'dist/windows/Claude Isolate') / 'runtime')


def dependencies(data, cfg):
    runtime = runtime_directory()
    bundled_qemu = runtime / 'qemu/qemu-system-x86_64.exe'
    bundled_gpg = runtime / 'GnuPG/bin/gpg.exe'
    if bundled_qemu.is_file():
        # Prefer our tested version even when upgrading a previously installed
        # environment whose configuration points at a global QEMU package.
        cfg['qemu_executable'] = str(bundled_qemu)
        cfg['qemu_data_dir'] = str(bundled_qemu.parent / 'share')
    if cfg.get('qemu_executable') and Path(cfg['qemu_executable']).is_file():
        os.environ['PATH'] = str(Path(cfg['qemu_executable']).parent) + os.pathsep + os.environ.get('PATH', '')
    private_gpg = data / 'tools' / ('GnuPG-' + gnupg.VERSION) / 'bin'
    gpg_bin = bundled_gpg.parent if bundled_gpg.is_file() else private_gpg
    if (gpg_bin / 'gpg.exe').is_file():
        os.environ['PATH'] = str(gpg_bin) + os.pathsep + os.environ.get('PATH', '')
    if not find_tool('qemu-system-x86_64') or not find_tool('qemu-img'):
        raise RuntimeError('В установке отсутствует QEMU. Переустановите полный Windows '
                           'setup.exe или распакуйте ZIP целиком вместе с папкой runtime.')
    if not find_tool('gpg'):
        emit('Загрузка отдельного GnuPG и проверка его SHA256…')
        installed = gnupg.install(data)
        os.environ['PATH'] = str(installed.parent) + os.pathsep + os.environ.get('PATH', '')
    tools = [find_tool(name) for name in ('qemu-system-x86_64', 'qemu-img', 'gpg')]
    if any(tool is None for tool in tools):
        raise RuntimeError('Установка runtime неполная.')
    os.environ['PATH'] = os.pathsep.join(dict.fromkeys(str(p.parent) for p in tools)) + os.pathsep + os.environ.get('PATH', '')
    cfg['qemu_executable'] = str(tools[0])
    write_config(data / 'environment.json', cfg)


def acceleration():
    if (os.name != 'nt' or platform.machine().lower() in ('arm64', 'aarch64')
            or os.environ.get('PROCESSOR_ARCHITEW6432', '').lower() == 'arm64'):
        return 'tcg'
    try:
        library = ctypes.WinDLL('WinHvPlatform.dll')
        library.WHvGetCapability.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
        library.WHvGetCapability.restype = ctypes.c_int32
        present, written = ctypes.c_int(), ctypes.c_uint()
        result = library.WHvGetCapability(0, ctypes.byref(present), ctypes.sizeof(present), ctypes.byref(written))
        return 'whpx' if result == 0 and written.value == ctypes.sizeof(present) and present.value else 'tcg'
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
        from windows.guest_update import upgrade
        upgrade(data, cfg)
        return
    if disk.exists() or seed.exists():
        raise RuntimeError('Найдена неполная среда. Существующий диск автоматически не перезаписывается.')
    emit('Загрузка готовой среды из GitHub Releases и проверка SHA256…')
    base, digest = release_image.download(data / 'downloads', cfg['arch'],
        lambda done, total: emit(f'Загрузка готовой среды: {done // 1048576} / {total // 1048576} МБ'))
    emit('Создание отдельного диска Linux…')
    # Persist the revision before publishing the two files; a process crash
    # after prepare must not mistake a pristine, unbooted guest for a legacy OS.
    from windows.guest_update import REVISION
    cfg.update(guest_revision=REVISION, guest_gateway_version=GUEST_GATEWAY_VERSION)
    write_config(data / 'environment.json', cfg)
    environment.prepare(cfg, base, digest)
    # Retain the signed base as a maintenance cache for future guest updates.
    emit('Среда подготовлена. Приложения уже установлены; первый запуск настроит рабочий стол.')


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
    if plain_boot.rfind('CLAUDE-ISOLATION: update-started') > plain_boot.rfind('CLAUDE-ISOLATION: environment-updated'):
        state['message'] = 'Обновляю Claude и Firefox; при недоступной сети обновление повторится…'
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
    parser.add_argument('action', choices=['prepare', 'upgrade', 'start', 'stop', 'resources-auto', *RESOURCE_PROFILES,
                                         'accel-auto', 'accel-tcg', 'accel-whpx'])
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--log', type=Path, help='Installer update log (UTF-8)')
    parser.add_argument('--start-gate', action='store_true')
    args = parser.parse_args()
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = args.log.open('a', encoding='utf-8', buffering=1)
    try:
        if args.start_gate and sys.stdin.readline().strip() != 'GO':
            raise RuntimeError('Запуск отменён контроллером')
        data = args.data.resolve()
        # Installing the controller for the first time must not download or
        # create a guest; only an existing environment needs installer updates.
        if args.action == 'upgrade' and not (data / 'environment.json').exists():
            emit('Существующей среды нет. Linux установится при первом запуске.')
            return
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
            if args.action == 'resources-auto' or args.action in RESOURCE_PROFILES:
                if args.action == 'resources-auto':
                    cfg.update(automatic_resources(), resources_mode='auto')
                else:
                    memory_mb, cpus = RESOURCE_PROFILES[args.action]
                    cfg.update(memory_mb=memory_mb, cpus=cpus, resources_mode=args.action)
                write_config(path, cfg)
                emit('Ресурсы сохранены')
                return
            if args.action == 'upgrade' and not Path(cfg['disk']).exists() and not Path(cfg['seed']).exists():
                emit('Существующего образа нет. Linux установится при первом запуске.')
                return
            if args.action != 'upgrade':
                configure_resources(path, cfg)
            else:
                warn_memory(maintenance_memory(), purpose='обновления образа')
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
