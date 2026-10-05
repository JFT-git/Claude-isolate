import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import environment
from windows import backend


class WindowsBackendTests(unittest.TestCase):
    def test_data_and_control_are_local_and_existing_settings_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path, cfg = backend.config(directory)
            self.assertEqual(cfg['arch'], 'x86_64')
            self.assertEqual(Path(cfg['disk']).parent, directory)
            self.assertNotIn('qmp_socket', cfg)
            cfg.update(memory_mb=6144, cpus=4)
            backend.write_config(path, cfg)
            _, again = backend.config(directory)
            self.assertEqual(again, cfg)

    def test_frozen_windows_paths_survive_glib_posix_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = backend.config(Path(temporary))
            executable = r'C:\Users\A B\Claude Isolate Core.exe'
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.object(sys, 'frozen', True, create=True), \
                 patch.object(sys, 'executable', executable):
                cmd = environment.command(cfg, check=False)
            net = cmd[cmd.index('-netdev') + 1]
            parsed = shlex.split(net.split('-cmd:', 1)[1].replace(',,', ','))
            self.assertEqual(parsed[:2], [executable, 'relay'])
            self.assertNotIn('environment.py', parsed)
            self.assertIn('restrict=on', net)
            self.assertIn('pipe:' + cfg['qmp_pipe'], cmd)
            self.assertFalse(any('tcp:' in c for c in cmd if c.startswith('pipe:')))

    def test_accelerator_and_display_cannot_inject_qemu_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = backend.config(Path(temporary))
            with patch.object(environment.platform, 'system', return_value='Windows'):
                for key, value in [('accelerator', 'tcg,-net nic'), ('display', 'vnc=:0')]:
                    with self.assertRaises(ValueError):
                        environment.command(dict(cfg, **{key: value}), check=False)

    def test_missing_winget_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(backend, 'find_tool', return_value=None), \
             patch.object(backend.shutil, 'which', return_value=None), \
             patch.object(backend.subprocess, 'run') as execute:
            with self.assertRaisesRegex(RuntimeError, 'App Installer'):
                backend.dependencies(Path(temporary), {})
            execute.assert_not_called()

    def test_existing_tools_do_not_trigger_reinstallation(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(backend, 'find_tool', side_effect=lambda name: Path(temporary) / (name + '.exe')), \
             patch.dict(os.environ, {'PATH': ''}), \
             patch.object(backend.subprocess, 'run') as execute:
            cfg = {}
            backend.dependencies(Path(temporary), cfg)
            execute.assert_not_called()
            self.assertTrue(cfg['qemu_executable'].endswith('qemu-system-x86_64.exe'))

    @unittest.skipUnless(importlib.util.find_spec('pycdlib'), 'Windows ISO dependency not installed')
    def test_seed_roundtrips_cloud_init_names_and_unicode(self):
        import io
        import pycdlib
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'input'
            directory.mkdir()
            expected = {'user-data': '#cloud-config\n# Русская раскладка\n',
                        'meta-data': 'instance-id: windows-test\n'}
            for name, content in expected.items():
                (directory / name).write_text(content, encoding='utf-8', newline='\n')
            image = Path(temporary) / 'seed.iso'
            backend.seed_iso(directory, image)
            iso = pycdlib.PyCdlib()
            iso.open(str(image))
            try:
                self.assertEqual(iso.pvd.volume_identifier.rstrip(), b'cidata')
                for name, content in expected.items():
                    output = io.BytesIO()
                    iso.get_file_from_iso_fp(output, rr_path='/' + name)
                    self.assertEqual(output.getvalue(), content.encode())
            finally:
                iso.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows process ownership test')
    def test_closing_job_terminates_owned_worker_and_child(self):
        import ctypes
        from ctypes import wintypes
        from windows.job import Job
        code = ('import subprocess,sys,time\n'
                'sys.stdin.readline()\n'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"])\n'
                'print(p.pid,flush=True)\n'
                'time.sleep(120)\n')
        worker = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        job = Job()
        handle = None
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            job.assign(worker)
            worker.stdin.write('GO\n')
            worker.stdin.flush()
            child = int(worker.stdout.readline())
            handle = api.OpenProcess(0x100000, False, child)
            self.assertTrue(handle)
            job.close()
            worker.wait(timeout=10)
            self.assertEqual(api.WaitForSingleObject(handle, 10000), 0)
        finally:
            job.close()
            if handle:
                api.CloseHandle(handle)
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=10)
            worker.stdin.close()
            worker.stdout.close()
