"""ASCII paths for QEMU on Windows.

QEMU's Windows build opens -drive, -serial and -L paths with the ANSI code
page. A profile such as C:\\Users\\Александр\\... is not that path from QEMU's
point of view, even when the letters exist in cp1251, because the process
treats the resulting bytes as UTF-8. The app keeps using the user's data
directory. Only files QEMU itself opens are placed on an ASCII path.
"""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess

HOST_FILES = {'disk': 'desktop.qcow2', 'seed': 'seed.iso', 'boot_log': 'boot.log'}
PATH_KEYS = ('qemu_executable', 'qemu_data_dir', 'firmware', 'disk', 'seed', 'boot_log', 'qmp_socket')


def path_is_ascii(value):
    try:
        str(value).encode('ascii')
    except UnicodeEncodeError:
        return False
    return True


def app_directory(cfg):
    """Directory the app owns. It may contain the user's real name."""
    status = cfg.get('network_status')
    if status:
        return Path(status).expanduser().parent
    return Path(cfg['disk']).expanduser().parent


def stable_id(data):
    path = Path(data).expanduser()
    try:
        text = str(path.resolve())
    except OSError:
        text = str(path.absolute())
    text = os.path.normcase(os.path.normpath(text))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def default_install_dir():
    from windows.backend import runtime_directory
    return runtime_directory().parent


def program_data_root():
    value = os.environ.get('PROGRAMDATA')
    if value:
        return Path(value)
    if os.name == 'nt':
        return Path('C:/ProgramData')
    return None


def _candidates(data, install_dir):
    identity = stable_id(data)
    roots = []
    program = program_data_root()
    if program is not None:
        roots.append(program / 'ClaudeIsolate')
    if install_dir is not None and path_is_ascii(install_dir):
        roots.append(Path(install_dir) / 'ClaudeIsolate')
    local = os.environ.get('LOCALAPPDATA')
    if local and path_is_ascii(local):
        roots.append(Path(local) / 'ClaudeIsolate')
    return [root / identity for root in roots]


def _writable(path):
    path.mkdir(parents=True, exist_ok=True)
    probe = path / '.write-probe'
    probe.write_text('ok', encoding='ascii')
    probe.unlink()
    return path_is_ascii(path)


def ensure_host_dir(data, cfg=None, install_dir=None):
    """Directory QEMU can open. Never a profile path with non-ASCII letters."""
    cfg = cfg or {}
    existing = cfg.get('qemu_host_dir')
    if existing and path_is_ascii(existing):
        try:
            if _writable(Path(existing)):
                return Path(existing)
        except OSError:
            pass
    if install_dir is None:
        install_dir = default_install_dir()
    errors = []
    for candidate in _candidates(data, install_dir):
        if not path_is_ascii(candidate):
            continue
        try:
            if _writable(candidate):
                return candidate
        except OSError as error:
            errors.append(str(candidate) + ': ' + str(error))
    detail = '; '.join(errors)
    raise RuntimeError(
        'QEMU на Windows не открывает файлы, если в пути есть нелатинские буквы. '
        'Не удалось создать отдельный каталог только из латинских букв'
        + ((': ' + detail) if detail else '.'))


def _configured_paths_are_ascii(cfg):
    values = [cfg.get(key) for key in PATH_KEYS if cfg.get(key)]
    return all(path_is_ascii(value) for value in values)


def qemu_argument_path(value):
    """Path string for a QEMU argument.

    Resolving a directory junction would turn an ASCII link back into the
    user's real directory. Keep the ASCII path QEMU is able to open.
    """
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        import environment
        raw = environment.ROOT / raw
    absolute = raw.absolute()
    try:
        resolved = absolute.resolve()
    except OSError:
        resolved = absolute
    if path_is_ascii(absolute) and not path_is_ascii(resolved):
        return absolute
    if path_is_ascii(resolved):
        return resolved
    if path_is_ascii(absolute):
        return absolute
    return resolved


def _reparse_point(path):
    if path.is_symlink():
        return True
    is_junction = getattr(path, 'is_junction', None)
    if is_junction and is_junction():
        return True
    if os.name != 'nt':
        return False
    try:
        import stat
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False


def _remove_published(link):
    """Remove a published link without deleting the directory it points at."""
    if not link.exists() and not link.is_symlink():
        return
    if _reparse_point(link):
        if os.name == 'nt' and not link.is_symlink():
            subprocess.run(['cmd', '/c', 'rmdir', str(link)], check=False, capture_output=True)
        if link.is_symlink():
            link.unlink(missing_ok=True)
        if link.exists() or link.is_symlink():
            raise RuntimeError('Не удалось заменить ссылку на каталог QEMU')
        return
    shutil.rmtree(link)


def publish_directory(source, link):
    """Expose a directory at an ASCII path without copying when a link works."""
    source = Path(source)
    try:
        source = source.resolve()
    except OSError:
        source = source.absolute()
    link = Path(link)
    marker = link.with_name(link.name + '.target')
    expected = str(source)
    if (link.exists() or link.is_symlink()) and marker.is_file():
        try:
            if marker.read_text(encoding='utf-8') == expected:
                return link
        except OSError:
            pass
    if link.exists() or link.is_symlink():
        _remove_published(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    published = False
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(source)],
                                capture_output=True, text=True)
        published = result.returncode == 0 and (link.exists() or link.is_symlink())
        if not published and (link.exists() or link.is_symlink()):
            _remove_published(link)
    if not published:
        try:
            os.symlink(source, link, target_is_directory=True)
            published = True
        except OSError:
            published = False
    if not published:
        if link.exists() or link.is_symlink():
            _remove_published(link)
        shutil.copytree(source, link)
    marker.write_text(expected, encoding='utf-8')
    return link


def migrate_file(source, target):
    """Move an existing file onto the ASCII path once. A missing source is a new file."""
    source, target = Path(source), Path(target)
    if not source.exists() and not source.is_symlink():
        return
    if target.is_file():
        try:
            if source.is_file() and source.samefile(target):
                source.unlink()
        except OSError:
            pass
        return
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        partial = target.with_name(target.name + '.partial')
        shutil.copy2(source, partial)
        if partial.stat().st_size != source.stat().st_size:
            partial.unlink(missing_ok=True)
            raise RuntimeError('Не удалось перенести файл в каталог, который QEMU может открыть')
        os.replace(partial, target)
    try:
        source.unlink()
    except OSError:
        pass


def expose_file(source, directory):
    """Give QEMU an ASCII name for an input that stays in the user's directory."""
    source = Path(source)
    if path_is_ascii(source):
        return source
    if not source.is_file():
        raise RuntimeError('Не найден файл, который нужно открыть в QEMU')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(os.path.normcase(str(source)).encode('utf-8')).hexdigest()[:16]
    target = directory / (identity + '-' + source.name)
    if target.is_file():
        source_stat, target_stat = source.stat(), target.stat()
        same = source_stat.st_dev == target_stat.st_dev and source_stat.st_ino == target_stat.st_ino
        if same or (source_stat.st_size == target_stat.st_size and source_stat.st_mtime_ns == target_stat.st_mtime_ns):
            return target
        target.unlink()
    try:
        os.link(source, target)
        return target
    except OSError:
        partial = target.with_name(target.name + '.partial')
        shutil.copy2(source, partial)
        if partial.stat().st_size != source.stat().st_size:
            partial.unlink(missing_ok=True)
            raise RuntimeError('Не удалось скопировать файл в каталог, который QEMU может открыть')
        os.replace(partial, target)
        return target


def input_for_qemu(path, cfg=None, data=None):
    path = Path(path)
    if platform.system() != 'Windows' or path_is_ascii(path):
        return path
    cfg = cfg or {}
    if cfg.get('qemu_host_dir') and path_is_ascii(cfg['qemu_host_dir']):
        host = Path(cfg['qemu_host_dir'])
    else:
        anchor = Path(data) if data is not None else app_directory(cfg if cfg else {'disk': path})
        host = ensure_host_dir(anchor, cfg)
        cfg['qemu_host_dir'] = str(host)
    return expose_file(path, host / 'cache')


def work_directory(data, cfg):
    """Parent for temporary files QEMU opens during a guest update."""
    data = Path(data)
    if platform.system() != 'Windows' or path_is_ascii(data):
        return data
    if cfg.get('qemu_host_dir') and path_is_ascii(cfg['qemu_host_dir']):
        path = Path(cfg['qemu_host_dir'])
        path.mkdir(parents=True, exist_ok=True)
        return path
    return ensure_host_dir(data, cfg)


def _expose_install(cfg, host):
    executable = cfg.get('qemu_executable')
    if executable and not path_is_ascii(executable):
        source_dir = Path(executable).parent
        link = publish_directory(source_dir, host / 'qemu')
        cfg['qemu_executable'] = str(link / Path(executable).name)
        data_dir = cfg.get('qemu_data_dir')
        if data_dir:
            try:
                relative = Path(data_dir).resolve().relative_to(source_dir.resolve())
                cfg['qemu_data_dir'] = str(link / relative)
            except (OSError, ValueError):
                pass
    data_dir = cfg.get('qemu_data_dir')
    if data_dir and not path_is_ascii(data_dir):
        cfg['qemu_data_dir'] = str(publish_directory(Path(data_dir), host / 'firmware'))
    firmware = cfg.get('firmware')
    if firmware and not path_is_ascii(firmware):
        source = Path(firmware)
        target = host / source.name
        migrate_file(source, target)
        cfg['firmware'] = str(qemu_argument_path(target))


def adopt_qemu_paths(cfg, data=None, install_dir=None):
    """Point QEMU's files at an ASCII directory. The app data directory stays put."""
    if platform.system() != 'Windows' or _configured_paths_are_ascii(cfg):
        return cfg
    anchor = Path(data) if data is not None else app_directory(cfg)
    host = ensure_host_dir(anchor, cfg, install_dir)
    cfg['qemu_host_dir'] = str(qemu_argument_path(host))
    for key, name in HOST_FILES.items():
        if not cfg.get(key) or path_is_ascii(cfg[key]):
            continue
        current = Path(cfg[key])
        target = host / name
        migrate_file(current, target)
        if key == 'disk':
            try:
                cfg['qemu_migrated_disk'] = str(current.resolve())
            except OSError:
                cfg['qemu_migrated_disk'] = str(current)
            sidecar = current.parent / 'state.json'
            destination = host / 'state.json'
            if sidecar.is_file() and not destination.exists():
                shutil.copy2(sidecar, destination)
        cfg[key] = str(qemu_argument_path(target))
    _expose_install(cfg, host)
    if not _configured_paths_are_ascii(cfg):
        raise RuntimeError(
            'QEMU на Windows открывает файлы в системной ANSI-кодировке и не может '
            'использовать путь с нелатинскими буквами.')
    return cfg


def assert_qemu_argv(command):
    if platform.system() != 'Windows':
        return command
    for item in command:
        if not path_is_ascii(item):
            raise RuntimeError(
                'QEMU на Windows открывает пути в кодировке ANSI и не может использовать: ' + str(item))
    return command
