"""Windows setup and VM control; invoked by the packaged console worker."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import subprocess
import tempfile
import uuid

import environment
import network_guard
import ubuntu_image
from session_lock import exclusive

VERSION = '0.3.0'


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
                               network_status=str(data / 'network.json')))
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
    if found:
        return Path(found)
    folders = [Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'qemu',
               Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'GnuPG/bin',
               Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'GnuPG/bin',
               Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/usr/bin']
    return next((folder / (name + '.exe') for folder in folders
                 if (folder / (name + '.exe')).is_file()), None)


def dependencies(data, cfg):
    if cfg.get('qemu_executable') and Path(cfg['qemu_executable']).is_file():
        os.environ['PATH'] = str(Path(cfg['qemu_executable']).parent) + os.pathsep + os.environ.get('PATH', '')
    packages = [('qemu-system-x86_64', 'SoftwareFreedomConservancy.QEMU'),
                ('gpg', 'GnuPG.Gpg4win')]
    for name, package in packages:
        if find_tool(name):
            continue
        winget = shutil.which('winget')
        if not winget:
            raise RuntimeError('Для автоматической установки нужен «Установщик приложений» '
                               'Microsoft (App Installer). Обновите его через Microsoft Store '
                               'или установите QEMU и Gpg4win вручную и повторите запуск.')
        emit('Установка ' + ('QEMU' if name.startswith('qemu') else 'GnuPG') +
             ' — подтвердите системный запрос Windows, если он появится.')
        subprocess.run([winget, 'install', '--exact', '--id', package, '--source', 'winget',
                        '--silent', '--accept-source-agreements', '--accept-package-agreements',
                        '--disable-interactivity'], check=True, timeout=900)
        if not find_tool(name):
            raise RuntimeError('Установленный компонент не найден: ' + name)
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


def prepare(data, cfg):
    disk, seed = Path(cfg['disk']), Path(cfg['seed'])
    if disk.is_file() and seed.is_file():
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
    # QEMU uses a duplex named pipe on Windows. No listening TCP control port.
    with open('\\\\.\\pipe\\' + cfg['qmp_pipe'], 'r+b', buffering=0) as pipe:
        greeting = json.loads(pipe.readline(65537))
        if 'QMP' not in greeting:
            raise RuntimeError('Неверный ответ канала управления QEMU')
        for request in ('qmp_capabilities', execute):
            pipe.write(json.dumps({'execute': request, 'id': request}).encode() + b'\n')
            for _ in range(100):
                line = pipe.readline(65537)
                if not line or len(line) > 65536:
                    raise RuntimeError('Канал управления QEMU прерван')
                reply = json.loads(line)
                if reply.get('id') != request:
                    continue
                if 'error' in reply:
                    raise RuntimeError(str(reply['error']))
                if 'return' in reply:
                    break
            else:
                raise RuntimeError('Нет ответа QEMU на команду управления')
        return reply['return']


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
    state['message'] = ('Рабочий стол готов' if 'CLAUDE-ISOLATION: desktop-ready' in boot
                        else 'Linux запускается и устанавливает компоненты…')
    if 'CLAUDE-ISOLATION: FAILURE' in boot:
        state['message'] = 'Установка не завершена — проверьте журнал'
    try:
        network = json.loads(Path(cfg['network_status']).read_text())
        network['allowed'] = network_guard.permitted(cfg['network_status'])
        for suffix in ('.revoked', '.paused'):
            marker = Path(cfg['network_status']).with_suffix(suffix)
            if marker.exists():
                network.update(allowed=False, reason=marker.read_text())
                break
        state['network'] = network
    except (OSError, ValueError, TypeError):
        state['network'] = {'allowed': False, 'reason': 'Ожидаю проверку сети…'}
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='Claude Isolate ' + VERSION)
    parser.add_argument('action', choices=['prepare', 'start', 'stop', 'economy', 'standard'])
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
            if args.action in ('economy', 'standard'):
                cfg.update(memory_mb=3072 if args.action == 'economy' else 6144,
                           cpus=2 if args.action == 'economy' else 4)
                write_config(path, cfg)
                emit('Ресурсы сохранены')
                return
            dependencies(data, cfg)
            prepare(data, cfg)
            if args.action == 'start':
                cfg['accelerator'] = acceleration()
                write_config(path, cfg)
                emit('Проверяю подключение и запускаю Linux…', accelerator=cfg['accelerator'])
                sys.argv = [sys.argv[0], 'start', '--config', str(path)]
                environment.main()
                emit('Среда остановлена')
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        emit(str(error), error=True)
        raise SystemExit(1)
