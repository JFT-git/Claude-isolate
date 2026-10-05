"""Private, hash-pinned native GnuPG from the vendor's WiX CAB payload."""
import hashlib
import os
from pathlib import Path, PureWindowsPath
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

import ubuntu_image

URL = 'https://gnupg.org/ftp/gcrypt/binary/gnupg-w32-2.5.24_20260923.wixlib'
SHA256 = '4e4600b3d28b6507e2433d59c69ba6c33d4da158f2c8da1e12f948b68a97ebda'
VERSION = '2.5.24'


def payload(package):
    if len(package) < 36 or package[:4] != b'MSCF':
        raise RuntimeError('Invalid GnuPG CAB payload')
    end = struct.unpack_from('<I', package, 8)[0]
    if not 36 <= end < len(package):
        raise RuntimeError('Invalid GnuPG CAB boundary')
    mapping = {}
    for field in ET.fromstring(package[end:]).iter():
        index = field.get('cabinetFileId')
        if index is None:
            continue
        if not index.isascii() or not index.isdecimal() or index in mapping:
            raise RuntimeError('Invalid GnuPG CAB file ID')
        source = PureWindowsPath(field.text or '')
        parts = source.parts[1:]
        if not source.is_absolute() or not parts or any(
                part in ('.', '..') or ':' in part or '/' in part or '\\' in part
                for part in parts):
            raise RuntimeError('Unsafe GnuPG payload path')
        mapping[index] = Path(*parts)
    if Path('bin/gpg.exe') not in mapping.values() or Path('bin/gpgconf.exe') not in mapping.values():
        raise RuntimeError('GnuPG payload is incomplete')
    return package[:end], mapping


def install(data):
    tools = data / 'tools'
    tools.mkdir(parents=True, exist_ok=True)
    destination = tools / ('GnuPG-' + VERSION)
    executable = destination / 'bin/gpg.exe'
    if executable.is_file():
        return executable
    if destination.exists():
        raise RuntimeError('Private GnuPG installation is incomplete; remove ' + str(destination))
    with tempfile.TemporaryDirectory(prefix='.gnupg-', dir=tools) as temporary:
        directory = Path(temporary)
        archive = directory / 'gnupg.wixlib'
        ubuntu_image.fetch(URL, archive)
        package = archive.read_bytes()
        if hashlib.sha256(package).hexdigest() != SHA256:
            raise RuntimeError('Native GnuPG package checksum mismatch')
        cabinet, mapping = payload(package)
        cab = directory / 'payload.cab'
        cab.write_bytes(cabinet)
        extracted = directory / 'extracted'
        extracted.mkdir()
        expand = Path(os.environ['SystemRoot']) / 'System32/expand.exe'
        subprocess.run([str(expand), '-F:*', str(cab), str(extracted)],
                       check=True, capture_output=True, timeout=60,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        staged = directory / 'GnuPG'
        staged.mkdir()
        for index, relative in mapping.items():
            source, target = extracted / index, staged / relative
            if not source.is_file():
                raise RuntimeError('GnuPG CAB extraction is incomplete: ' + index)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        # Documented portable mode avoids changing global GnuPG configuration.
        (staged / 'bin/gpgconf.ctl').touch()
        (staged / 'home').mkdir()
        (staged / 'usr/local/var/cache/gnupg').mkdir(parents=True)
        subprocess.run([str(staged / 'bin/gpg.exe'), '--version'], check=True,
                       capture_output=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        os.replace(staged, destination)
    return executable
