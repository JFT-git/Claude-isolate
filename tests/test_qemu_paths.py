"""QEMU on Windows must not receive a non-ASCII file path."""
import codecs
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import environment
from windows import backend
from windows.control import Control
from windows.qemu_paths import stable_id


def windows_config(data):
    _, cfg = backend.config(data)
    cfg.update(accelerator='tcg', whpx_cpu='qemu64')
    return cfg


def link_directory(alias, target):
    alias.parent.mkdir(parents=True, exist_ok=True)
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != 'nt':
            raise
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(target)],
                                capture_output=True, text=True)
        if result.returncode != 0 or not (alias.exists() or alias.is_symlink()):
            raise OSError(result.stderr or result.stdout or 'junction failed')


def assert_ascii_argv(test, argv):
    for item in argv:
        test.assertTrue(item.isascii(), item)
        folded = item.replace('/', '\\').casefold()
        test.assertNotIn('programdata', folded, item)
        test.assertNotIn('program files', folded, item)
        test.assertNotIn('александр', folded, item)


class QemuAsciiPathTests(unittest.TestCase):
    def test_cyrillic_profile_uses_public_without_an_elevated_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Александр' / 'Claude Isolate'
            data.mkdir(parents=True)
            install = data.parent / 'Programs' / 'Claude Isolate' / 'runtime' / 'qemu'
            (install / 'share').mkdir(parents=True)
            (install / 'qemu-system-x86_64.exe').write_bytes(b'MZ')
            (install / 'share' / 'bios-256k.bin').write_bytes(b'bios')
            public = root / 'Users' / 'Public'
            public.mkdir(parents=True)
            programdata = root / 'ProgramData'
            ascii_install = root / 'ASCIIInstall'
            ascii_install.mkdir()
            cfg = windows_config(data)
            Path(cfg['disk']).write_bytes(b'user-disk')
            Path(cfg['seed']).write_bytes(b'user-seed')
            original_disk = Path(cfg['disk'])
            cfg.update(qemu_executable=str(install / 'qemu-system-x86_64.exe'),
                       qemu_data_dir=str(install / 'share'))
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch('windows.qemu_paths.windows_short_path', return_value=None), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(public),
                                         'LOCALAPPDATA': str(data.parent)}, clear=False):
                command = environment.command(cfg, check=False)
                again = environment.command(cfg, check=False)
            for argv in (command, again):
                assert_ascii_argv(self, argv)
            drive = next(item for item in command if 'desktop.qcow2' in item)
            disk = Path(drive.split(',if=', 1)[0].removeprefix('file='))
            self.assertEqual(disk.read_bytes(), b'user-disk')
            self.assertFalse(original_disk.exists())
            seed = Path(next(item for item in command if 'seed.iso' in item).split(',if=', 1)[0].removeprefix('file='))
            self.assertEqual(seed.read_bytes(), b'user-seed')
            firmware = Path(command[command.index('-L') + 1])
            self.assertEqual((firmware / 'bios-256k.bin').read_bytes(), b'bios')
            self.assertTrue(str(command[0]).endswith('qemu-system-x86_64.exe'))
            gateway = next(item for item in command if item.startswith('pipe,id=gateway,'))
            self.assertRegex(gateway, r'path=claude-isolate-[0-9a-f]{32}-gateway$')
            self.assertRegex(next(item for item in command if item.startswith('pipe:claude-isolate-')),
                             r'^pipe:claude-isolate-[0-9a-f]{32}$')
            self.assertTrue((data / 'shared').is_dir())
            self.assertTrue(str(cfg['disk']).isascii())
            self.assertNotEqual(Path(cfg['disk']).parent.resolve(), data.resolve())
            host = public / 'ClaudeIsolate' / stable_id(data)
            self.assertEqual(disk.resolve().parent, host.resolve())
            self.assertEqual(host.parent.parent.resolve(), public.resolve())
            self.assertFalse(programdata.exists())
            self.assertFalse((ascii_install / 'ClaudeIsolate').exists())
            self.assertGreaterEqual(len(Path(cfg['disk']).parts), 4)

    def test_cyrillic_profile_prefers_an_ascii_short_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Александр' / 'Claude Isolate'
            data.mkdir(parents=True)
            install = data.parent / 'Programs' / 'Claude Isolate' / 'runtime' / 'qemu'
            (install / 'share').mkdir(parents=True)
            (install / 'qemu-system-x86_64.exe').write_bytes(b'MZ')
            (install / 'share' / 'bios-256k.bin').write_bytes(b'bios')
            alias = root / 'short' / 'ALEXAN~1' / 'CLAUDE~1'
            link_directory(alias, data)
            public = root / 'Users' / 'Public'
            programdata = root / 'ProgramData'
            cfg = windows_config(data)
            Path(cfg['disk']).write_bytes(b'user-disk')
            Path(cfg['seed']).write_bytes(b'user-seed')
            original_disk = Path(cfg['disk'])
            cfg.update(qemu_executable=str(install / 'qemu-system-x86_64.exe'),
                       qemu_data_dir=str(install / 'share'))
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch('windows.qemu_paths.windows_short_path', return_value=alias), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(public)}, clear=False):
                command = environment.command(cfg, check=False)
            assert_ascii_argv(self, command)
            drive = next(item for item in command if 'desktop.qcow2' in item)
            disk = Path(drive.split(',if=', 1)[0].removeprefix('file='))
            self.assertEqual(disk.read_bytes(), b'user-disk')
            self.assertTrue(original_disk.is_file())
            self.assertEqual(original_disk.read_bytes(), b'user-disk')
            self.assertEqual(disk.resolve(), original_disk.resolve())
            self.assertTrue(str(disk).isascii())
            self.assertFalse(public.exists())
            self.assertFalse(programdata.exists())
            firmware = Path(command[command.index('-L') + 1])
            self.assertEqual((firmware / 'bios-256k.bin').read_bytes(), b'bios')
            self.assertTrue(str(firmware).isascii())

    def test_short_path_replaces_only_the_cyrillic_user_name(self):
        from windows.qemu_paths import windows_short_path
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / 'Александр'
            data = profile / 'Claude Isolate'
            data.mkdir(parents=True)
            link_directory(root / 'ALEXAN~1', profile)

            def fake_short(path):
                if Path(path).name == 'Александр':
                    return str(root / 'ALEXAN~1')
                return None

            with patch('windows.qemu_paths._get_short_path', side_effect=fake_short):
                short = windows_short_path(data)
            self.assertIsNotNone(short)
            self.assertTrue(str(short).isascii(), short)
            self.assertEqual(short.resolve(), data.resolve())
            self.assertEqual(short.name, 'Claude Isolate')
            self.assertIn('ALEXAN~1', short.parts)

    def test_programdata_disk_migrates_to_public(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Александр' / 'Claude Isolate'
            data.mkdir(parents=True)
            public = root / 'Users' / 'Public'
            public.mkdir(parents=True)
            programdata = root / 'ProgramData'
            legacy = programdata / 'ClaudeIsolate' / stable_id(data)
            legacy.mkdir(parents=True)
            (legacy / 'desktop.qcow2').write_bytes(b'from-programdata')
            (legacy / 'seed.iso').write_bytes(b'legacy-seed')
            (legacy / 'state.json').write_text('{"disk":"programdata"}', encoding='utf-8')
            cfg = windows_config(data)
            cfg.update(disk=str(legacy / 'desktop.qcow2'), seed=str(legacy / 'seed.iso'),
                       boot_log=str(legacy / 'boot.log'),
                       qemu_executable=r'C:\ASCII\qemu-system-x86_64.exe')
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch('windows.qemu_paths.windows_short_path', return_value=None), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(public)}, clear=False):
                command = environment.command(cfg, check=False)
            assert_ascii_argv(self, command)
            disk = Path(cfg['disk'])
            self.assertEqual(disk.read_bytes(), b'from-programdata')
            self.assertEqual(disk.parent.resolve(), (public / 'ClaudeIsolate' / stable_id(data)).resolve())
            self.assertFalse((legacy / 'desktop.qcow2').exists())
            self.assertEqual(Path(cfg['seed']).read_bytes(), b'legacy-seed')
            self.assertEqual((disk.parent / 'state.json').read_text(encoding='utf-8'), '{"disk":"programdata"}')
            self.assertNotIn(programdata.resolve(), disk.resolve().parents)

    def test_no_ascii_directory_refuses_to_pass_the_profile_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Александр' / 'Claude Isolate'
            data.mkdir(parents=True)
            blocked = root / 'not-a-directory'
            blocked.write_text('x', encoding='ascii')
            programdata = root / 'ProgramData'
            cfg = windows_config(data)
            Path(cfg['disk']).write_bytes(b'disk')
            Path(cfg['seed']).write_bytes(b'seed')
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch('windows.qemu_paths.windows_short_path', return_value=None), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(blocked),
                                         'LOCALAPPDATA': str(data.parent)}):
                with self.assertRaisesRegex(RuntimeError, 'нелатинск'):
                    environment.command(cfg, check=False)
            self.assertTrue(Path(cfg['disk']).name == 'desktop.qcow2')
            self.assertIn('Александр', str(cfg['disk']))
            self.assertFalse(programdata.exists())

    def test_ascii_profile_keeps_its_own_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Claude Isolate'
            programdata = root / 'ProgramData'
            public = root / 'Users' / 'Public'
            cfg = windows_config(data)
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata), 'PUBLIC': str(public)}):
                environment.command(cfg, check=False)
            self.assertEqual(Path(cfg['disk']).parent, data)
            self.assertFalse(programdata.exists())
            self.assertFalse(public.exists())

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
