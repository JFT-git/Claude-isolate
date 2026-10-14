"""QEMU on Windows must not receive a non-ASCII file path."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import environment
from windows import backend
from windows.control import Control


def windows_config(data):
    _, cfg = backend.config(data)
    cfg.update(accelerator='tcg', whpx_cpu='qemu64')
    return cfg


class QemuAsciiPathTests(unittest.TestCase):
    def test_cyrillic_data_directory_produces_an_ascii_qemu_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Александр' / 'Claude Isolate'
            data.mkdir(parents=True)
            install = data.parent / 'Programs' / 'Claude Isolate' / 'runtime' / 'qemu'
            (install / 'share').mkdir(parents=True)
            (install / 'qemu-system-x86_64.exe').write_bytes(b'MZ')
            (install / 'share' / 'bios-256k.bin').write_bytes(b'bios')
            programdata = root / 'ProgramData'
            cfg = windows_config(data)
            Path(cfg['disk']).write_bytes(b'user-disk')
            Path(cfg['seed']).write_bytes(b'user-seed')
            original_disk = Path(cfg['disk'])
            cfg.update(qemu_executable=str(install / 'qemu-system-x86_64.exe'),
                       qemu_data_dir=str(install / 'share'))
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata),
                                         'LOCALAPPDATA': str(data.parent)}, clear=False):
                command = environment.command(cfg, check=False)
                again = environment.command(cfg, check=False)
            for argv in (command, again):
                for item in argv:
                    self.assertTrue(item.isascii(), item)
                self.assertNotIn('Александр', '\n'.join(argv))
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
            self.assertTrue(str(disk).startswith(str(programdata.resolve())) or programdata.resolve() in disk.resolve().parents)

    def test_unwritable_programdata_uses_an_ascii_install_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Александр' / 'Claude Isolate'
            data.mkdir(parents=True)
            blocked = root / 'not-a-directory'
            blocked.write_text('x', encoding='ascii')
            install = root / 'ASCIIInstall'
            cfg = windows_config(data)
            Path(cfg['disk']).write_bytes(b'disk')
            Path(cfg['seed']).write_bytes(b'seed')
            cfg['qemu_executable'] = r'C:\ASCIIInstall\qemu-system-x86_64.exe'
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(blocked), 'LOCALAPPDATA': str(data.parent)}), \
                 patch('windows.qemu_paths.default_install_dir', return_value=install):
                command = environment.command(cfg, check=False)
            for item in command:
                self.assertTrue(item.isascii(), item)
            self.assertIn(str(install.resolve()), str(Path(cfg['disk']).resolve()))

    def test_no_ascii_directory_refuses_to_pass_the_profile_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Александр' / 'Claude Isolate'
            data.mkdir(parents=True)
            blocked = root / 'not-a-directory'
            blocked.write_text('x', encoding='ascii')
            cfg = windows_config(data)
            Path(cfg['disk']).write_bytes(b'disk')
            Path(cfg['seed']).write_bytes(b'seed')
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(blocked), 'LOCALAPPDATA': str(data.parent)}), \
                 patch('windows.qemu_paths.default_install_dir', return_value=data.parent):
                with self.assertRaisesRegex(RuntimeError, 'нелатинск'):
                    environment.command(cfg, check=False)
            self.assertTrue(Path(cfg['disk']).name == 'desktop.qcow2')
            self.assertIn('Александр', str(cfg['disk']))

    def test_ascii_profile_keeps_its_own_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'Claude Isolate'
            programdata = root / 'ProgramData'
            cfg = windows_config(data)
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.dict(os.environ, {'PROGRAMDATA': str(programdata)}):
                environment.command(cfg, check=False)
            self.assertEqual(Path(cfg['disk']).parent, data)
            self.assertFalse(programdata.exists())

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

    def test_running_qemu_without_output_still_reports_the_timeout(self):
        process = Mock()
        process.poll.return_value = None
        owner = Control({'disk': 'disk', 'qmp_pipe': 'claude-isolate-' + 'ab' * 16}, process)
        with patch('windows.control.time.monotonic', side_effect=[0, 100]), \
             self.assertRaisesRegex(RuntimeError, 'QEMU не подготовил канал управления вовремя$'):
            owner.wait_ready(1)
