"""QEMU on Windows receives a drive letter, never a Cyrillic or 8.3 path."""
import codecs
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import environment
from windows import backend
from windows.control import Control
from windows.qemu_paths import select_drive_letter


def windows_config(data):
    _, cfg = backend.config(data)
    cfg.update(accelerator='tcg', whpx_cpu='qemu64')
    return cfg


class DriveLetters:
    def __init__(self):
        self.targets = {}

    def __call__(self, target, preferred=None):
        text = str(Path(target))
        preferred = (preferred or '').upper()[:1]
        if preferred and self.targets.get(preferred) == text:
            return preferred
        if preferred and preferred not in self.targets and preferred not in 'ABC':
            self.targets[preferred] = text
            return preferred
        for letter in 'ZYXWVUTSRQPONMLKJIHGFED':
            if letter not in self.targets:
                self.targets[letter] = text
                return letter
        raise RuntimeError('Нет свободной буквы диска')


def assert_drive_argv(test, argv):
    for item in argv:
        test.assertTrue(item.isascii(), item)
        folded = item.replace('/', '\\').casefold()
        test.assertNotIn('programdata', folded, item)
        test.assertNotIn('program files', folded, item)
        test.assertNotIn('александр', folded, item)
        test.assertNotIn('общие', folded, item)
        test.assertNotRegex(item, r'(^|[\\/])[^\\/]*~[0-9]')
        parts = [part.casefold() for part in item.replace('/', '\\').split('\\')]
        test.assertNotIn('public', parts, item)


def file_argument(command, name):
    return next(item for item in command if name in item).split(',if=', 1)[0].removeprefix('file=')


class QemuAsciiPathTests(unittest.TestCase):
    def test_cyrillic_profile_uses_a_drive_letter(self):
        with tempfile_cyrillic() as (root, data, install, public, programdata):
            cfg = windows_config(data)
            Path(cfg['disk']).write_bytes(b'user-disk')
            Path(cfg['seed']).write_bytes(b'user-seed')
            cfg.update(qemu_executable=str(install / 'qemu-system-x86_64.exe'),
                       qemu_data_dir=str(install / 'share'))
            letters = DriveLetters()
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch('windows.qemu_paths.assign_drive', letters), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(public)}, clear=False):
                command = environment.command(cfg, check=False)
                again = environment.command(cfg, check=False)
            for argv in (command, again):
                assert_drive_argv(self, argv)
            disk = file_argument(command, 'desktop.qcow2')
            self.assertRegex(disk, r'^[A-Z]:\\desktop\.qcow2$')
            letter = disk[0]
            self.assertEqual(file_argument(command, 'seed.iso'), letter + ':\\seed.iso')
            boot = next(item for item in command if 'boot.log' in item)
            self.assertIn(letter + ':\\boot.log', boot)
            self.assertEqual(command[command.index('-L') + 1], letter + ':\\qemu-firmware')
            self.assertRegex(command[0], r'^[A-Z]:\\qemu-system-x86_64\.exe$')
            self.assertNotEqual(command[0][0], letter)
            self.assertEqual((data / 'desktop.qcow2').read_bytes(), b'user-disk')
            self.assertEqual((data / 'seed.iso').read_bytes(), b'user-seed')
            self.assertEqual((data / 'qemu-firmware' / 'bios-256k.bin').read_bytes(), b'bios')
            self.assertIn('Александр', letters.targets[letter])
            self.assertNotIn('Public', letters.targets[letter])
            self.assertFalse(programdata.exists())
            self.assertFalse(public.exists())
            self.assertFalse((root / 'Общие').exists())
            self.assertEqual(again[again.index('-L') + 1], letter + ':\\qemu-firmware')

    def test_short_path_winerror_448_still_uses_a_drive_letter(self):
        with tempfile_cyrillic() as (root, data, install, public, programdata):
            cfg = windows_config(data)
            Path(cfg['disk']).write_bytes(b'user-disk')
            Path(cfg['seed']).write_bytes(b'user-seed')
            short = r'C:\Users\8523~1\AppData\Local\CLAUDE~3'
            cfg.update(disk=short + r'\desktop.qcow2',
                       seed=short + r'\seed.iso',
                       boot_log=short + r'\boot.log',
                       qemu_executable=str(install / 'qemu-system-x86_64.exe'),
                       qemu_data_dir=str(install / 'share'))
            real_is_file = Path.is_file

            def is_file(self):
                if '~' in str(self):
                    error = OSError('Невозможно пройти путь, поскольку он содержит недоверенную точку подключения')
                    error.winerror = 448
                    raise error
                return real_is_file(self)

            letters = DriveLetters()
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch('windows.qemu_paths.assign_drive', letters), \
                 patch.object(Path, 'is_file', is_file), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(public)}, clear=False):
                command = environment.command(cfg, check=False)
            assert_drive_argv(self, command)
            disk = file_argument(command, 'desktop.qcow2')
            self.assertRegex(disk, r'^[A-Z]:\\desktop\.qcow2$')
            self.assertEqual((data / 'desktop.qcow2').read_bytes(), b'user-disk')
            self.assertNotIn('8523~1', str(cfg['disk']))
            self.assertFalse(programdata.exists())
            self.assertFalse(public.exists())

    def test_untrusted_mount_point_does_not_raise(self):
        from windows.qemu_paths import _is_file

        def boom(self):
            error = OSError('untrusted mount point')
            error.winerror = 448
            raise error

        with patch.object(Path, 'is_file', boom):
            self.assertFalse(_is_file(r'C:\Users\8523~1\AppData\Local\CLAUDE~3\qemu'))

    def test_define_dos_device_failure_is_reported(self):
        with tempfile_cyrillic() as (root, data, install, public, programdata):
            cfg = windows_config(data)
            Path(cfg['disk']).write_bytes(b'disk')
            Path(cfg['seed']).write_bytes(b'seed')
            cfg.update(qemu_executable=str(install / 'qemu-system-x86_64.exe'),
                       qemu_data_dir=str(install / 'share'))

            def fail(target, preferred=None):
                raise RuntimeError('Не удалось подключить букву диска для QEMU через DefineDosDeviceW: Access is denied')

            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch('windows.qemu_paths.assign_drive', fail), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(public)}, clear=False):
                with self.assertRaisesRegex(RuntimeError, 'DefineDosDeviceW'):
                    environment.command(cfg, check=False)
            self.assertFalse(programdata.exists())
            self.assertFalse(public.exists())

    def test_programdata_disk_is_copied_into_the_profile(self):
        from windows.qemu_paths import stable_id
        with tempfile_cyrillic() as (root, data, install, public, programdata):
            programdata.mkdir()
            legacy = programdata / 'ClaudeIsolate' / stable_id(data)
            legacy.mkdir(parents=True)
            (legacy / 'desktop.qcow2').write_bytes(b'from-programdata')
            (legacy / 'seed.iso').write_bytes(b'legacy-seed')
            cfg = windows_config(data)
            cfg.update(disk=str(legacy / 'desktop.qcow2'), seed=str(legacy / 'seed.iso'),
                       boot_log=str(data / 'boot.log'),
                       qemu_executable=str(install / 'qemu-system-x86_64.exe'))
            letters = DriveLetters()
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch('windows.qemu_paths.assign_drive', letters), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(public)}, clear=False):
                command = environment.command(cfg, check=False)
            assert_drive_argv(self, command)
            self.assertEqual((data / 'desktop.qcow2').read_bytes(), b'from-programdata')
            self.assertEqual((data / 'seed.iso').read_bytes(), b'legacy-seed')
            self.assertRegex(file_argument(command, 'desktop.qcow2'), r'^[A-Z]:\\desktop\.qcow2$')
            self.assertNotIn('ProgramData', str(cfg['disk']))

    def test_ascii_profile_keeps_its_own_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Claude Isolate'
            programdata = root / 'ProgramData'
            public = root / 'Users' / 'Public'
            common = root / 'Общие'
            cfg = windows_config(data)

            def fail(target, preferred=None):
                raise AssertionError('an ASCII profile does not need a drive letter')

            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch('windows.qemu_paths.assign_drive', fail), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(public)}):
                command = environment.command(cfg, check=False)
            self.assertEqual(Path(cfg['disk']).parent, data)
            disk = file_argument(command, 'desktop.qcow2').replace('/', '\\')
            self.assertIn('desktop.qcow2', disk)
            # A subst path is Q:\desktop.qcow2. The real profile path keeps its directories.
            self.assertNotRegex(disk, r'^[A-Z]:\\desktop\.qcow2$')
            self.assertFalse(programdata.exists())
            self.assertFalse(public.exists())
            self.assertFalse(common.exists())

    def test_drive_letter_is_not_stolen(self):
        self.assertEqual(select_drive_letter({'Q', 'Z'}, preferred='Q', owned=False), 'Y')
        self.assertEqual(select_drive_letter(set(), preferred='Q'), 'Q')
        self.assertEqual(select_drive_letter({'Q'}, preferred='Q', owned=True), 'Q')
        self.assertIsNone(select_drive_letter(set('ZYXWVUTSRQPONMLKJIHGFED')))
        self.assertEqual(select_drive_letter(set(), preferred='C', owned=True), 'Z')
        self.assertNotEqual(select_drive_letter({'C'}, preferred='C', owned=True), 'C')

    def test_immediate_qemu_exit_reports_stderr_instead_of_the_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'qemu-host.log'
            log.write_bytes('Could not open desktop.qcow2: Системе не удается найти указанный путь\n'.encode('cp1251'))
            process = Mock()
            process.poll.return_value = 1
            process.returncode = 1
            owner = Control({'disk': str(Path(temporary) / 'disk'), 'qmp_pipe': 'claude-isolate-' + 'ab' * 16},
                            process, output_path=log)
            with self.assertRaises(RuntimeError) as caught:
                owner.wait_ready(2)
            message = str(caught.exception)
            self.assertIn('Could not open desktop.qcow2', message)
            self.assertIn('Системе не удается найти указанный путь', message)
            self.assertNotIn('не подготовил канал управления вовремя', message)

    def test_cp1251_qemu_log_is_not_read_as_a_western_ansi_page(self):
        from windows.control import decode_qemu_output
        data = 'Could not open desktop.qcow2: Системе не удается найти указанный путь\n'.encode('cp1251')

        def search(name):
            if name == 'mbcs':
                return codecs.lookup('cp1252')
            return None

        codecs.register(search)
        try:
            with patch('windows.control.os.name', 'nt'):
                text = decode_qemu_output(data)
        finally:
            codecs.unregister(search)
        self.assertIn('Системе не удается найти указанный путь', text)
        self.assertNotIn('Ñ', text)

    def test_running_qemu_without_output_still_reports_the_timeout(self):
        process = Mock()
        process.poll.return_value = None
        owner = Control({'disk': 'disk', 'qmp_pipe': 'claude-isolate-' + 'ab' * 16}, process)
        with patch('windows.control.time.monotonic', side_effect=[0, 100]), \
             self.assertRaisesRegex(RuntimeError, 'QEMU не подготовил канал управления вовремя$'):
            owner.wait_ready(1)


class InstallerScriptTests(unittest.TestCase):
    def test_upgrade_removes_the_old_program_and_keeps_user_data(self):
        text = Path('windows/setup.iss').read_text(encoding='utf-8')
        self.assertIn('AppId={{F426138A-7ECA-487B-A6CD-47EB31C40462}', text)
        self.assertIn('UsePreviousAppDir=yes', text)
        self.assertIn('function PrepareToInstall', text)
        self.assertIn('/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', text)
        self.assertIn('{F426138A-7ECA-487B-A6CD-47EB31C40462}_is1', text)
        self.assertNotIn('[UninstallDelete]', text)
        self.assertNotIn('Public', text)
        self.assertNotIn('Общие', text)
        self.assertNotIn('ProgramData', text)


def tempfile_cyrillic():
    return _CyrillicTree()


class _CyrillicTree:
    def __enter__(self):
        import tempfile
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        data = root / 'Александр' / 'Claude Isolate'
        data.mkdir(parents=True)
        install = data.parent / 'Programs' / 'Claude Isolate' / 'runtime' / 'qemu'
        (install / 'share').mkdir(parents=True)
        (install / 'qemu-system-x86_64.exe').write_bytes(b'MZ')
        (install / 'share' / 'bios-256k.bin').write_bytes(b'bios')
        public = root / 'Users' / 'Public'
        programdata = root / 'ProgramData'
        return root, data, install, public, programdata

    def __exit__(self, *args):
        self._temporary.cleanup()
