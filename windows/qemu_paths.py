"""ASCII paths for QEMU on Windows.

QEMU's Windows build opens -drive, -serial and -L paths with the ANSI code
page. A profile such as C:\\Users\\Александр\\... is not a path QEMU can open,
even when the letters exist in cp1251, because the process treats those bytes
as UTF-8. The app keeps using the user's data directory. Files QEMU itself
opens use that directory's ASCII 8.3 path when Windows has one. Otherwise they
live in C:\\Users\\Public\\ClaudeIsolate\\<id>, which the current user can
create without administrator rights. ProgramData, Program Files, and the drive
root are never created or passed to QEMU.
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


def _get_short_path(path):
    """Windows 8.3 path, or None when this process cannot ask for one."""
    if os.name != 'nt':
        return None
    path = Path(path)
    if not path.exists():
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        get_short = kernel.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        needed = get_short(str(path), None, 0)
        if not needed:
            return None
        buffer = ctypes.create_unicode_buffer(needed)
        written = get_short(str(path), buffer, needed)
        if not written:
            return None
        text = buffer.value
    except (OSError, AttributeError, ValueError):
        return None
    if text.startswith('\\\\?\\UNC\\'):
        text = '\\\\' + text[8:]
    elif text.startswith('\\\\?\\'):
        text = text[4:]
    return text or None


def windows_short_path(path):
    """ASCII 8.3 path of an existing directory, when Windows still has one.

    The whole path is used when Windows can shorten it. Otherwise only
    non-ASCII components are replaced, so C:\\Users\\Александр\\AppData\\...
    \\Claude Isolate can stay C:\\Users\\ALEXAN~1\\AppData\\...\\Claude Isolate.
    """
    path = Path(path)
    if not path.exists():
        return None
    if path_is_ascii(path):
        return path
    direct = _get_short_path(path)
    if direct and path_is_ascii(direct):
        candidate = Path(direct)
        try:
            if candidate.resolve() == path.resolve():
                return candidate
        except OSError:
            pass
    parts = path.parts
    if len(parts) < 2:
        return None
    rebuilt = [parts[0]]
    current = Path(parts[0])
    shortened = False
    for part in parts[1:]:
        current = current / part
        if path_is_ascii(part):
            rebuilt.append(part)
            continue
        short = _get_short_path(current)
        short_name = Path(short).name if short else ''
        if (not short_name or not path_is_ascii(short_name)
                or os.path.normcase(short_name) == os.path.normcase(part)):
            return None
        rebuilt.append(short_name)
        shortened = True
    if not shortened or not path_is_ascii(Path(*rebuilt)):
        return None
    candidate = Path(*rebuilt)
    try:
        if candidate.resolve() != path.resolve():
            return None
    except OSError:
        return None
    return candidate


def _public_root():
    """C:\\Users\\Public, never a directory that needs elevation to create."""
    value = os.environ.get('PUBLIC')
    if value and path_is_ascii(value):
        return Path(value)
    if os.name == 'nt':
        fallback = Path('C:/Users/Public')
        if path_is_ascii(fallback):
            return fallback
    return None


def public_host(data):
    root = _public_root()
    if root is None or not path_is_ascii(root):
        return None
    return root / 'ClaudeIsolate' / stable_id(data)


def _programdata_roots():
    roots = []
    value = os.environ.get('PROGRAMDATA')
    if value:
        roots.append(Path(value))
    if os.name == 'nt':
        default = Path('C:/ProgramData')
        if all(os.path.normcase(str(item)) != os.path.normcase(str(default)) for item in roots):
            roots.append(default)
    return roots


def path_under_programdata(path):
    """True for the 1.0.1 ClaudeIsolate tree under ProgramData. Read-only check."""
    if not path:
        return False
    text = os.path.normcase(os.path.normpath(str(path)))
    for root in _programdata_roots():
        prefix = os.path.normcase(os.path.normpath(str(root)))
        if text == prefix or not text.startswith(prefix + os.sep):
            continue
        rest = text[len(prefix) + len(os.sep):]
        head = rest.split(os.sep, 1)[0]
        if head.casefold() == 'claudeisolate':
            return True
    return False


def _legacy_hosts(data):
    """Directories a 1.0.1 build may already have filled. Never created here."""
    identity = stable_id(data)
    return [root / 'ClaudeIsolate' / identity for root in _programdata_roots()]


def _same_directory(left, right):
    left, right = Path(left), Path(right)
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def _writable(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    probe = path / '.write-probe'
    probe.write_text('ok', encoding='ascii')
    probe.unlink()
    return path_is_ascii(path)


def ensure_host_dir(data, cfg=None, install_dir=None):
    """ASCII directory QEMU can open without administrator rights.

    install_dir is ignored: Program Files and ProgramData can require
    elevation, and the drive root is not a place for a per-user disk.
    """
    del cfg, install_dir
    data = Path(data)
    short = windows_short_path(data)
    if short is not None and path_is_ascii(short):
        try:
            if _writable(short):
                return short
        except OSError:
            pass
    candidate = public_host(data)
    root = _public_root()
    if candidate is None or root is None or not path_is_ascii(candidate) or not root.is_dir():
        raise RuntimeError(
            'QEMU на Windows не открывает файлы, если в пути есть нелатинские буквы. '
            'Короткий путь профиля недоступен, а каталог C:\\Users\\Public\\ClaudeIsolate '
            'нельзя создать без прав администратора.')
    try:
        if _writable(candidate):
            return candidate
    except OSError as error:
        raise RuntimeError(
            'QEMU на Windows не открывает файлы, если в пути есть нелатинские буквы. '
            'Не удалось создать каталог C:\\Users\\Public\\ClaudeIsolate без прав администратора: '
            + str(error)) from error
    raise RuntimeError(
        'QEMU на Windows не открывает файлы, если в пути есть нелатинские буквы. '
        'Не удалось создать каталог только из латинских букв.')


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


def _usable_host(cfg):
    existing = cfg.get('qemu_host_dir') if cfg else None
    if existing and path_is_ascii(existing) and not path_under_programdata(existing):
        return Path(existing)
    return None


def input_for_qemu(path, cfg=None, data=None):
    path = Path(path)
    if platform.system() != 'Windows' or path_is_ascii(path):
        return path
    cfg = cfg or {}
    host = _usable_host(cfg)
    if host is None:
        anchor = Path(data) if data is not None else app_directory(cfg if cfg else {'disk': path})
        host = ensure_host_dir(anchor, cfg)
        cfg['qemu_host_dir'] = str(qemu_argument_path(host))
    return expose_file(path, host / 'cache')


def work_directory(data, cfg):
    """Parent for temporary files QEMU opens during a guest update."""
    data = Path(data)
    if platform.system() != 'Windows' or path_is_ascii(data):
        return data
    host = _usable_host(cfg)
    if host is not None:
        host.mkdir(parents=True, exist_ok=True)
        return host
    return ensure_host_dir(data, cfg)


def _must_republish(path):
    return bool(path) and (not path_is_ascii(path) or path_under_programdata(path))


def _expose_install(cfg, host):
    executable = cfg.get('qemu_executable')
    if _must_republish(executable):
        source_dir = Path(executable).parent
        link = publish_directory(source_dir, host / 'qemu')
        cfg['qemu_executable'] = str(qemu_argument_path(link / Path(executable).name))
        data_dir = cfg.get('qemu_data_dir')
        if data_dir:
            try:
                relative = Path(data_dir).resolve().relative_to(source_dir.resolve())
                cfg['qemu_data_dir'] = str(qemu_argument_path(link / relative))
            except (OSError, ValueError):
                pass
    data_dir = cfg.get('qemu_data_dir')
    if _must_republish(data_dir):
        cfg['qemu_data_dir'] = str(qemu_argument_path(publish_directory(Path(data_dir), host / 'firmware')))
    firmware = cfg.get('firmware')
    if _must_republish(firmware):
        source = Path(firmware)
        target = host / source.name
        if not _same_directory(source.parent, host):
            migrate_file(source, target)
        cfg['firmware'] = str(qemu_argument_path(target))


def _needs_relocation(cfg, data):
    if not _configured_paths_are_ascii(cfg):
        return True
    for key in PATH_KEYS:
        if path_under_programdata(cfg.get(key)):
            return True
    disk = Path(cfg['disk']) if cfg.get('disk') else None
    if disk is not None and disk.is_file() and not path_under_programdata(disk):
        return False
    return any((legacy / 'desktop.qcow2').is_file() for legacy in _legacy_hosts(data))


def _sources_for(cfg, key, name, legacy_dirs):
    found = []

    def add(path):
        if path is None:
            return
        path = Path(path)
        marker = os.path.normcase(os.path.normpath(str(path)))
        if any(os.path.normcase(os.path.normpath(str(item))) == marker for item in found):
            return
        found.append(path)

    current = Path(cfg[key]) if cfg.get(key) else None
    # A configured profile file is the live disk. ProgramData is the live disk
    # only when configuration already points there, or the profile file is gone.
    if current is not None and current.is_file() and not path_under_programdata(current):
        add(current)
        return found
    if current is not None and path_under_programdata(current):
        add(current)
    for legacy in legacy_dirs:
        add(legacy / name)
    if current is not None:
        add(current)
    return found


def _remember_disk(cfg, source):
    if cfg.get('qemu_migrated_disk'):
        return
    try:
        cfg['qemu_migrated_disk'] = str(Path(source).resolve())
    except OSError:
        cfg['qemu_migrated_disk'] = str(source)


def _place_host_files(cfg, data, host):
    legacy_dirs = _legacy_hosts(data)
    for key, name in HOST_FILES.items():
        if not cfg.get(key):
            continue
        target = host / name
        if target.is_file() and _same_directory(target.parent, data):
            # 8.3 path of the profile: the file is already in the user's directory.
            cfg[key] = str(qemu_argument_path(target))
            continue
        if not target.is_file():
            for source in _sources_for(cfg, key, name, legacy_dirs):
                if not source.is_file() or _same_directory(source.parent, target.parent):
                    continue
                migrate_file(source, target)
                if key == 'disk' and target.is_file():
                    _remember_disk(cfg, source)
                    sidecar = Path(source).parent / 'state.json'
                    destination = host / 'state.json'
                    if sidecar.is_file() and not destination.exists():
                        shutil.copy2(sidecar, destination)
                break
        cfg[key] = str(qemu_argument_path(target))


def adopt_qemu_paths(cfg, data=None, install_dir=None):
    """Point QEMU's files at an ASCII directory the user can create."""
    if platform.system() != 'Windows':
        return cfg
    anchor = Path(data) if data is not None else app_directory(cfg)
    if not _needs_relocation(cfg, anchor):
        return cfg
    host = ensure_host_dir(anchor, cfg, install_dir)
    cfg['qemu_host_dir'] = str(qemu_argument_path(host))
    _place_host_files(cfg, anchor, host)
    _expose_install(cfg, host)
    if not _configured_paths_are_ascii(cfg) or any(path_under_programdata(cfg.get(key)) for key in PATH_KEYS):
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
