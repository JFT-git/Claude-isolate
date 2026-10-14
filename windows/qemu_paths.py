"""Drive-letter paths for files QEMU opens on Windows.

QEMU's Windows build opens -drive, -serial and -L paths with the ANSI code
page, so a profile such as C:\\Users\\Александр\\... cannot be passed through.
A Windows 8.3 short path is not a fix either: Python raises WinError 448
(untrusted mount point) when it walks one, as in
C:\\Users\\8523~1\\AppData\\Local\\CLAUDE~3\\qemu.

Every launch that needs it maps a free drive letter onto the real per-user
data directory with DefineDosDeviceW. QEMU then sees Q:\\desktop.qcow2 and the
bytes stay in the user's folder. Nothing is created in Public, «Общие»,
ProgramData, Program Files, or the drive root. A letter that is already in
use is never replaced. If DefineDosDeviceW fails, the error is raised for the
UI instead of falling back to a Cyrillic path.
"""
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil

HOST_FILES = {'disk': 'desktop.qcow2', 'seed': 'seed.iso', 'boot_log': 'boot.log'}
PATH_KEYS = ('qemu_executable', 'qemu_data_dir', 'firmware', 'disk', 'seed', 'boot_log', 'qmp_socket')
_LETTER_ORDER = 'ZYXWVUTSRQPONMLKJIHGFED'
_SHORT_NAME = re.compile(r'(^|[\\/])[^\\/]*~[0-9]+([\\/]|$)')
_DRIVE = re.compile(r'^[A-Za-z]:[\\/]')


class OpenPath:
    """Path string for a QEMU argument, with the real file kept for existence checks."""

    def __init__(self, visible, real=None):
        self.visible = str(visible)
        self.real = Path(real) if real is not None else None

    def __str__(self):
        return self.visible

    def is_file(self):
        target = self.real if self.real is not None else Path(self.visible)
        try:
            return target.is_file()
        except OSError as error:
            if _untrusted(error):
                return False
            raise


def path_is_ascii(value):
    try:
        str(value).encode('ascii')
    except UnicodeEncodeError:
        return False
    return True


def _untrusted(error):
    """WinError 448: the path walks an untrusted mount point, often an 8.3 name."""
    if not isinstance(error, OSError):
        return False
    if getattr(error, 'winerror', None) == 448 or getattr(error, 'errno', None) == 448:
        return True
    folded = str(error).casefold()
    return '448' in folded and (
        'untrusted' in folded or 'недоверен' in folded
        or 'mount point' in folded or 'точку подключения' in folded)


def _is_file(path):
    try:
        return Path(path).is_file()
    except OSError as error:
        if _untrusted(error):
            return False
        raise


def _is_dir(path):
    try:
        return Path(path).is_dir()
    except OSError as error:
        if _untrusted(error):
            return False
        raise


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


def _norm(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _absolute(path):
    try:
        return os.path.normpath(str(Path(path).absolute()))
    except OSError as error:
        if _untrusted(error):
            return os.path.normpath(str(path))
        raise


def _within(path, root):
    path_text, root_text = _norm(_absolute(path)), _norm(_absolute(root))
    return path_text == root_text or path_text.startswith(root_text + os.sep)


def _relative(path, root):
    path_text, root_text = _absolute(path), _absolute(root)
    folded_path, folded_root = _norm(path_text), _norm(root_text)
    if folded_path == folded_root:
        return ''
    prefix = folded_root + os.sep
    if not folded_path.startswith(prefix):
        return None
    return path_text[len(root_text):].lstrip('\\/')


def _parts(value):
    text = str(value).replace('/', '\\')
    return [part.casefold() for part in text.split('\\') if part]


def forbidden_qemu_text(value):
    """True when an argument would hand QEMU a path it must not open."""
    text = str(value)
    if not path_is_ascii(text):
        return True
    folded = text.casefold()
    if 'programdata' in folded or 'program files' in folded:
        return True
    parts = _parts(text)
    if 'public' in parts or 'общие' in parts:
        return True
    return _SHORT_NAME.search(text.replace('/', '\\')) is not None


def _is_drive_path(value):
    return _DRIVE.match(str(value)) is not None


def safe_for_qemu(value):
    if not value:
        return True
    if _is_drive_path(value):
        return not forbidden_qemu_text(value)
    return path_is_ascii(value) and not forbidden_qemu_text(value)


def _drive_file(letter, relative):
    relative = str(relative).replace('/', '\\').lstrip('\\')
    if not relative:
        return letter + ':\\'
    return letter + ':\\' + relative


def select_drive_letter(used, preferred=None, owned=False):
    """Pick a free letter. A letter already in use is returned only when we own it."""
    used = {str(item).upper()[:1] for item in used}
    preferred = (preferred or '').upper()[:1]
    if preferred and owned and preferred not in 'ABC' and preferred.isalpha():
        return preferred
    if preferred and preferred.isalpha() and preferred not in used and preferred not in 'ABC':
        return preferred
    for letter in _LETTER_ORDER:
        if letter not in used:
            return letter
    return None


def _logical_letters():
    if os.name != 'nt':
        return set()
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get = kernel.GetLogicalDrives
    get.restype = wintypes.DWORD
    mask = get()
    return {chr(ord('A') + index) for index in range(26) if mask & (1 << index)}


def _query_target(letter):
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.QueryDosDeviceW
    query.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    query.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    written = query(letter + ':', buffer, len(buffer))
    if not written:
        return None
    text = buffer.value
    if text.startswith('\\\\??\\UNC\\'):
        text = '\\\\' + text[8:]
    elif text.startswith('\\\\??\\'):
        text = text[4:]
    return text or None


def _define_dos_device(letter, target):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    define = kernel.DefineDosDeviceW
    define.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR]
    define.restype = wintypes.BOOL
    if not define(0, letter + ':', str(target)):
        raise ctypes.WinError(ctypes.get_last_error())


def _targets_match(current, target):
    if not current:
        return False
    return _norm(current) == _norm(_absolute(target))


def _drive_error(detail):
    return RuntimeError('Не удалось подключить букву диска для QEMU через DefineDosDeviceW: ' + detail)


def assign_drive(target, preferred=None):
    """Map target to a free drive letter and return that letter.

    Recreates the mapping when the preferred letter is missing. Never steals a
    letter that already points somewhere else.
    """
    if os.name != 'nt':
        raise _drive_error('функция доступна только в Windows.')
    target = Path(target)
    preferred = (preferred or '').upper()[:1]
    try:
        if preferred and _targets_match(_query_target(preferred), target):
            return preferred
        used = set(_logical_letters())
        for letter in _LETTER_ORDER:
            if _query_target(letter):
                used.add(letter)
        # Our own mapping is not "in use" by someone else.
        if preferred and _targets_match(_query_target(preferred), target):
            used.discard(preferred)
        chosen = select_drive_letter(used, preferred, owned=False)
        if not chosen:
            raise _drive_error('нет свободной буквы диска. Занятая буква не используется.')
        try:
            _define_dos_device(chosen, target)
        except OSError as error:
            raise _drive_error(str(error)) from error
        if not _targets_match(_query_target(chosen), target):
            raise _drive_error('буква ' + chosen + ': не указывает на каталог среды.')
        return chosen
    except OSError as error:
        if _untrusted(error):
            raise _drive_error(str(error)) from error
        raise


def _programdata_roots():
    roots = []
    value = os.environ.get('PROGRAMDATA')
    if value:
        roots.append(Path(value))
    if os.name == 'nt':
        default = Path('C:/ProgramData')
        if all(_norm(item) != _norm(default) for item in roots):
            roots.append(default)
    return roots


def path_under_programdata(path):
    """True for a ClaudeIsolate tree under ProgramData. Read-only check."""
    if not path:
        return False
    text = _norm(path)
    for root in _programdata_roots():
        prefix = _norm(root)
        if text == prefix or not text.startswith(prefix + os.sep):
            continue
        rest = text[len(prefix) + len(os.sep):]
        head = rest.split(os.sep, 1)[0]
        if head.casefold() == 'claudeisolate':
            return True
    return False


def _legacy_disk_dirs(data):
    """Places an older build may have copied the disk. Never created here."""
    identity = stable_id(data)
    found = []
    for root in _programdata_roots():
        found.append(root / 'ClaudeIsolate' / identity)
    public = os.environ.get('PUBLIC')
    if public:
        found.append(Path(public) / 'ClaudeIsolate' / identity)
    return found


def _copy_file(source, target):
    """Copy once. A WinError 448 on a leftover short path is ignored."""
    source, target = Path(source), Path(target)
    try:
        if not source.is_file():
            return False
        if target.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + '.partial')
        shutil.copy2(source, partial)
        if partial.stat().st_size != source.stat().st_size:
            partial.unlink(missing_ok=True)
            raise RuntimeError('Не удалось скопировать файл в каталог среды')
        os.replace(partial, target)
        return True
    except OSError as error:
        if _untrusted(error):
            return False
        raise


def _recover_profile_files(cfg, data):
    """Keep qcow2, seed and the boot log in the user's own directory."""
    data = Path(data)
    for key, name in HOST_FILES.items():
        dest = data / name
        if _is_file(dest):
            cfg[key] = str(dest)
            continue
        source = cfg.get(key)
        copied = False
        if source and _copy_file(source, dest):
            if key == 'disk':
                cfg['qemu_migrated_disk'] = str(source)
            sidecar = Path(source).parent / 'state.json'
            destination = data / 'state.json'
            if _is_file(sidecar) and not destination.exists():
                try:
                    shutil.copy2(sidecar, destination)
                except OSError as error:
                    if not _untrusted(error):
                        raise
            copied = True
        if not copied:
            for legacy in _legacy_disk_dirs(data):
                candidate = legacy / name
                if _copy_file(candidate, dest):
                    if key == 'disk':
                        cfg['qemu_migrated_disk'] = str(candidate)
                    copied = True
                    break
        if copied or cfg.get(key):
            cfg[key] = str(dest)


def _stage_firmware(cfg, data):
    """Put the firmware directory on the same drive QEMU uses for the disk."""
    source = cfg.get('qemu_data_dir')
    if not source:
        return
    source = Path(source)
    data = Path(data)
    if _within(source, data):
        return
    dest = data / 'qemu-firmware'
    try:
        if not _is_dir(source):
            return
    except OSError as error:
        if _untrusted(error):
            raise RuntimeError('Не удалось прочитать каталог прошивки QEMU: ' + str(error)) from error
        raise
    marker = data / 'qemu-firmware.source'
    expected = str(source)
    try:
        if dest.is_dir() and marker.is_file() and marker.read_text(encoding='utf-8') == expected:
            cfg['qemu_data_dir'] = str(dest)
            return
    except OSError as error:
        if not _untrusted(error):
            raise
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    marker.write_text(expected, encoding='utf-8')
    cfg['qemu_data_dir'] = str(dest)


def _needs_drive(cfg, data):
    if not safe_for_qemu(data):
        return True
    for key in ('disk', 'seed', 'boot_log', 'qemu_executable', 'qemu_data_dir', 'firmware'):
        value = cfg.get(key)
        if value and not safe_for_qemu(value):
            return True
    return False


def _ensure_exe_drive(cfg, data):
    exe = cfg.get('qemu_executable')
    if not exe or safe_for_qemu(exe):
        return
    parent = Path(exe).parent
    if _within(parent, data):
        return
    cfg['qemu_exe_drive'] = assign_drive(parent, preferred=cfg.get('qemu_exe_drive'))


def adopt_qemu_paths(cfg, data=None, install_dir=None):
    """Map a drive letter when QEMU would otherwise see a non-ASCII path.

    install_dir is ignored. Program Files and ProgramData can require
    elevation, and the files stay in the per-user data directory.
    """
    del install_dir
    if platform.system() != 'Windows':
        return cfg
    anchor = Path(data) if data is not None else app_directory(cfg)
    _recover_profile_files(cfg, anchor)
    if not _needs_drive(cfg, anchor):
        return cfg
    cfg['qemu_drive'] = assign_drive(anchor, preferred=cfg.get('qemu_drive'))
    _stage_firmware(cfg, anchor)
    _ensure_exe_drive(cfg, anchor)
    return cfg


def _cache_outside(path, data):
    path, data = Path(path), Path(data)
    directory = data / 'qemu-cache'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    if target.is_file():
        try:
            source_stat, target_stat = path.stat(), target.stat()
            if source_stat.st_size == target_stat.st_size and source_stat.st_mtime_ns == target_stat.st_mtime_ns:
                return target
        except OSError as error:
            if not _untrusted(error):
                raise
        target.unlink()
    if not _copy_file(path, target) and not target.is_file():
        raise RuntimeError('Не удалось скопировать файл в каталог, который QEMU открывает по букве диска')
    return target


def open_for_qemu(path, cfg=None, data=None):
    """ASCII path QEMU can open. Unsafe profile paths become a drive-letter path."""
    cfg = cfg if cfg is not None else {}
    text = str(path)
    if platform.system() != 'Windows':
        return OpenPath(text, text)
    if _is_drive_path(text):
        visible = text.replace('/', '\\')
        if forbidden_qemu_text(visible):
            raise _drive_error('путь ' + visible + ' использовать нельзя.')
        return OpenPath(visible, text)
    if safe_for_qemu(text):
        return OpenPath(str(qemu_argument_path(text)), text)
    anchor = Path(data) if data is not None else app_directory(cfg)
    if not cfg.get('qemu_drive'):
        cfg['qemu_drive'] = assign_drive(anchor, preferred=cfg.get('qemu_drive'))
    letter = cfg['qemu_drive']
    relative = _relative(path, anchor)
    if relative is not None:
        return OpenPath(_drive_file(letter, relative), path)
    real = Path(path)
    if real.suffix.lower() == '.exe' or 'qemu-system' in real.name.lower() or real.name.lower().startswith('qemu-img'):
        if not cfg.get('qemu_exe_drive'):
            cfg['qemu_exe_drive'] = assign_drive(real.parent, preferred=cfg.get('qemu_exe_drive'))
        return OpenPath(_drive_file(cfg['qemu_exe_drive'], real.name), path)
    cached = _cache_outside(real, anchor)
    return OpenPath(_drive_file(letter, _relative(cached, anchor)), cached)


def input_for_qemu(path, cfg=None, data=None):
    path = Path(path)
    if platform.system() != 'Windows' or safe_for_qemu(path):
        return path
    cfg = cfg or {}
    anchor = Path(data) if data is not None else app_directory(cfg if cfg else {'disk': path})
    if _within(path, anchor):
        return path
    return _cache_outside(path, anchor)


def work_directory(data, cfg):
    """Temporary files QEMU opens stay in the user's directory, under the mapped letter."""
    del cfg
    return Path(data)


def qemu_argument_path(value):
    """Absolute path for a QEMU argument that is already safe to open.

    A drive-letter path is kept as written. Resolving it would turn Q: back
    into the user's real directory, or raise WinError 448.
    """
    text = str(value)
    if _is_drive_path(text):
        return text.replace('/', '\\')
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        import environment
        raw = environment.ROOT / raw
    try:
        absolute = raw.absolute()
    except OSError as error:
        if not _untrusted(error):
            raise
        absolute = raw
    try:
        resolved = absolute.resolve()
    except OSError as error:
        if _untrusted(error):
            if safe_for_qemu(absolute):
                return absolute
            raise RuntimeError(
                'QEMU не может открыть путь: недоверенная точка подключения (WinError 448). '
                'Короткий путь 8.3 не используется.') from error
        resolved = absolute
    # A junction back to a Cyrillic directory must stay on the ASCII path.
    if path_is_ascii(absolute) and not path_is_ascii(resolved):
        return absolute
    if safe_for_qemu(resolved):
        return resolved
    if safe_for_qemu(absolute):
        return absolute
    return absolute


def paths_match(left, right):
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError as error:
        if not _untrusted(error):
            raise
    return _norm(left) == _norm(right)


def assert_qemu_argv(command):
    if platform.system() != 'Windows':
        return command
    for item in command:
        if forbidden_qemu_text(item):
            raise RuntimeError(
                'QEMU на Windows не получает путь с кириллицей, коротким именем 8.3, '
                'ProgramData или папкой «Общие»: ' + str(item))
    return command
