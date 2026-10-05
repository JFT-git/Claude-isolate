import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import environment
from windows import backend
from windows import gnupg


class WindowsBackendTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('paramiko'), 'Windows bridge dependency not installed')
    def test_serial_transport_multiplexes_only_filtered_gateway_channels(self):
        import concurrent.futures
        import socket
        import threading
        import paramiko
        from windows.serial_gateway import Gateway
        server, client = socket.socketpair()
        gateway = Gateway({'network_mode': 'system', 'web_access': 'public'},
                          dict(os.environ, CLAUDE_NETWORK_LEASE=''))
        thread = threading.Thread(target=gateway.serve, args=(server,), daemon=True)
        thread.start()
        transport = paramiko.Transport(client)
        try:
            transport.start_client(timeout=10)
            transport.auth_none('claude-gateway')
            with self.assertRaises(paramiko.ChannelException):
                transport.open_session(timeout=5)
            with self.assertRaises(paramiko.ChannelException):
                transport.open_channel('direct-tcpip', ('127.0.0.1', 22), ('127.0.0.1', 0), timeout=5)
            def request(host):
                channel = transport.open_channel('direct-tcpip', ('claude.gateway', 7890),
                                                 ('127.0.0.1', 0), timeout=5)
                channel.settimeout(10)
                try:
                    channel.sendall(('CONNECT ' + host + ':443 HTTP/1.1\r\n\r\n').encode())
                    return channel.recv(4096)
                finally:
                    channel.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                local, public = list(pool.map(request, ['127.0.0.1', 'claude.ai']))
            self.assertTrue(local.startswith(b'HTTP/1.1 403'))
            self.assertTrue(public.startswith(b'HTTP/1.1 503'))
        finally:
            transport.close()
            gateway.close()
            thread.join(timeout=5)
            server.close()

    def test_native_gnupg_bad_checksum_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(gnupg.ubuntu_image, 'fetch', side_effect=lambda url, target: target.write_bytes(b'invalid')), \
             patch.object(gnupg.subprocess, 'run') as execute:
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                gnupg.install(Path(temporary))
            execute.assert_not_called()

    def test_gnupg_metadata_cannot_escape_private_directory(self):
        import struct
        for filename in ('k:\\bin\\..\\..\\outside.exe', 'relative.exe'):
            cab = bytearray(36)
            cab[:4] = b'MSCF'
            struct.pack_into('<I', cab, 8, 36)
            xml = ('<root><field cabinetFileId="0">' + filename + '</field></root>').encode()
            with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                gnupg.payload(bytes(cab) + xml)

    def test_cyrillic_paths_and_stop_marker_work_without_utf8_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'Тест'
            path, cfg = backend.config(directory)
            child = ('import sys,environment,network_guard; '
                     'cfg=environment.load_config(sys.argv[1]); '
                     'network_guard.revoke(None,cfg["network_status"],"Остановка среды")')
            subprocess.run([sys.executable, '-c', child, str(path)],
                           env=dict(os.environ, PYTHONUTF8='0'), check=True,
                           cwd=environment.ROOT, capture_output=True, timeout=10)
            marker = Path(cfg['network_status']).with_suffix('.revoked')
            self.assertEqual(marker.read_text(encoding='utf-8'), 'Остановка среды')

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

    def test_windows_uses_private_serial_bridge_instead_of_glib_socket_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = backend.config(Path(temporary))
            executable = r'C:\Users\A B\Claude Isolate Core.exe'
            with patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.object(sys, 'frozen', True, create=True), \
                 patch.object(sys, 'executable', executable):
                cmd = environment.command(cfg, check=False)
            net = cmd[cmd.index('-netdev') + 1]
            self.assertNotIn('guestfwd=', net)
            self.assertIn('restrict=on', net)
            self.assertIn('virtserialport,chardev=gateway,name=claude.gateway', cmd)
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

    def test_git_msys_gpg_is_not_selected_as_native_gnupg(self):
        with patch.object(backend.shutil, 'which', return_value=r'C:\Program Files\Git\usr\bin\gpg.exe'), \
             patch.object(Path, 'is_file', return_value=False):
            self.assertIsNone(backend.find_tool('gpg'))

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
