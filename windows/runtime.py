"""Build a private, pinned Windows runtime; never run a system installer."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import ubuntu_image
from windows import gnupg

QEMU_URL = 'https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811.exe'
QEMU_SHA512 = ('5bcf9eed634e8575a37b74f445af41a2fe4106da512d0c30c368301d4c105037f'
               'dfab40a5287367a28a957624cddebbc8c07e16c88ab6634f554cdf3d16bf543')
FIRMWARE = ('bios-256k.bin', 'bios.bin', 'kvmvapic.bin', 'vgabios-virtio.bin',
            'pxe-virtio.rom', 'efi-virtio.rom', 'linuxboot_dma.bin',
            'multiboot_dma.bin', 'pvh.bin')


def qemu_files(source):
    # Follow both regular and delay-loaded PE imports, retaining all transitive
    # vendor DLLs. Windows system libraries are supplied by the supported OS.
    import pefile
    libraries = {path.name.lower(): path for path in source.glob('*.dll')}
    pending = [source / 'qemu-system-x86_64.exe', source / 'qemu-img.exe']
    selected = set()
    while pending:
        path = pending.pop()
        if path in selected:
            continue
        selected.add(path)
        with pefile.PE(str(path), fast_load=True) as image:
            image.parse_data_directories(directories=[1, 13])
            imports = (getattr(image, 'DIRECTORY_ENTRY_IMPORT', []) +
                       getattr(image, 'DIRECTORY_ENTRY_DELAY_IMPORT', []))
            for entry in imports:
                dependency = libraries.get(entry.dll.decode('ascii').lower())
                if dependency:
                    pending.append(dependency)
    return sorted(selected)


def copy_qemu(source, target):
    target.mkdir(parents=True)
    for path in qemu_files(source):
        shutil.copy2(path, target / path.name)
    share = target / 'share'
    share.mkdir()
    for name in FIRMWARE:
        shutil.copy2(source / 'share' / name, share / name)
    for name in ('icons', 'keymaps'):
        shutil.copytree(source / 'share' / name, share / name)
    for name in ('COPYING', 'COPYING.LIB', 'README.rst', 'VERSION'):
        shutil.copy2(source / name, target / name)


def bundle(folder):
    target = folder / 'runtime'
    target.mkdir()
    with tempfile.TemporaryDirectory(prefix='claude-runtime-') as temporary:
        directory = Path(temporary)
        installer = directory / 'qemu.exe'
        ubuntu_image.fetch(QEMU_URL, installer)
        with installer.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha512').hexdigest() != QEMU_SHA512:
                raise RuntimeError('Pinned QEMU package checksum mismatch')
        # 7-Zip is a build dependency only. Extract NSIS files instead of
        # registering QEMU globally or requesting elevation on the client.
        extractor = shutil.which('7z') or str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / '7-Zip/7z.exe')
        extracted = directory / 'qemu'
        subprocess.run([extractor, 'x', '-y', '-o' + str(extracted), str(installer)],
                       check=True, capture_output=True, timeout=180)
        copy_qemu(extracted, target / 'qemu')
        gpg = gnupg.install(directory)
        shutil.copytree(gpg.parent.parent, target / 'GnuPG')
    manifest = {
        'qemu': {'version': '11.1.0', 'url': QEMU_URL, 'sha512': QEMU_SHA512},
        'gnupg': {'version': gnupg.VERSION, 'url': gnupg.URL, 'sha256': gnupg.SHA256},
        'files': {},
    }
    for path in sorted(target.rglob('*')):
        if path.is_file():
            with path.open('rb') as stream:
                manifest['files'][path.relative_to(target).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    (target / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (target / 'SOURCES.txt').write_text(
        'Unmodified QEMU 11.1.0 Windows binaries and their runtime libraries:\n'
        'https://qemu.weilnetz.de/w64/ (vendor build instructions and source links)\n'
        'https://github.com/stefanweil/qemu (vendor source and make-installers-all)\n'
        'https://www.qemu.org/download/#source\n'
        'https://packages.msys2.org/ (vendor dependency sources)\n'
        'GnuPG and its libraries, unmodified vendor portable payload:\n'
        'https://gnupg.org/download/index.html\n'
        'Vendor license files are preserved in the runtime directories.\n', encoding='utf-8')
    return manifest
