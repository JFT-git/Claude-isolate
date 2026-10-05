import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import environment
from windows import backend
from windows import gnupg


class WindowsBackendTests(unittest.TestCase):
    def test_legacy_guest_upgrade_preserves_old_disks_and_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            path, cfg = backend.config(data)
            cfg.pop('guest_gateway_version')
            backend.write_config(path, cfg)
            old_disk, old_seed = Path(cfg['disk']), Path(cfg['seed'])
            old_disk.write_bytes(b'previous-user-disk')
            old_seed.write_bytes(b'previous-seed')
            base = data / 'base.img'
            base.touch()
            def prepare(replacement, image, digest):
                Path(replacement['disk']).write_bytes(b'new-disk')
                Path(replacement['seed']).write_bytes(b'new-seed')
            with patch.object(backend, 'emit'), patch.object(backend, 'find_tool', return_value='gpg'), \
                 patch.object(backend.ubuntu_image, 'download', return_value=(base, 'digest')) as download, \
                 patch.object(backend.environment, 'prepare', side_effect=prepare) as build:
                backend.prepare(data, cfg)
                backend.prepare(data, cfg)
                download.assert_called_once()
                build.assert_called_once()
            self.assertEqual(old_disk.read_bytes(), b'previous-user-disk')
            self.assertEqual(old_seed.read_bytes(), b'previous-seed')
            self.assertEqual(environment.load_config(path), cfg)
            self.assertNotEqual(Path(cfg['disk']), old_disk)
            backup = environment.load_config(data / 'environment-before-gateway-upgrade.json')
            self.assertEqual(Path(backup['disk']), old_disk)

    def test_failed_guest_upgrade_keeps_existing_configuration_and_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            path, cfg = backend.config(data)
            cfg.pop('guest_gateway_version')
            backend.write_config(path, cfg)
            original = dict(cfg)
            Path(cfg['disk']).write_bytes(b'old-disk')
            Path(cfg['seed']).write_bytes(b'old-seed')
            with patch.object(backend, 'emit'), patch.object(backend, 'find_tool', return_value='gpg'), \
                 patch.object(backend.ubuntu_image, 'download', side_effect=OSError('Offline')):
                with self.assertRaises(OSError):
                    backend.prepare(data, cfg)
            self.assertEqual(cfg, original)
            self.assertEqual(environment.load_config(path), original)
            self.assertEqual(Path(cfg['disk']).read_bytes(), b'old-disk')

    def test_gateway_boot_command_installs_independent_enabled_service(self):
        from windows.serial_gateway import boot_command
        command = boot_command(environment.ROOT)
        self.assertEqual(command[:2], ['sh', '-c'])
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / 'gateway.sh'
            script.write_text(command[2], encoding='utf-8')
            if os.name != 'nt':
                subprocess.run(['sh', '-n', str(script)], check=True)
        self.assertIn('WantedBy=multi-user.target', command[2])
        self.assertIn('Restart=always', command[2])
        self.assertIn('enable --now claude-gateway.service', command[2])
        self.assertNotIn('while true', command[2])

    def test_ready_status_after_reboot_without_first_install_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = backend.config(Path(temporary))
            boot = Path(cfg['boot_log'])
            cases = [
                ('Starting lightdm.service - Light Display Manager.', False),
                ('[ OK ] Started \x1b[0;1;39mlightdm.service\x1b[0m - Light Display Manager.', True),
                ('CLAUDE-ISOLATION: desktop-ready', True),
            ]
            for log, ready in cases:
                with self.subTest(log=log):
                    boot.write_text(log, encoding='utf-8')
                    self.assertEqual(backend.status(cfg, running=True)['message'] == 'Рабочий стол готов', ready)

    def test_failed_whpx_retries_once_with_same_exit_ip_and_remembers_tcg(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = backend.config(Path(temporary))
            error = subprocess.CalledProcessError(3489660927, ['qemu'])
            error.initial_exit_ip = '8.8.8.8'
            error.network_locked = False
            with patch.object(backend, 'acceleration', return_value='whpx'), \
                 patch.object(environment, 'main', side_effect=[error, None]) as launch, \
                 patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
                backend.start_environment(path, cfg)
            self.assertEqual(launch.call_args_list[1].kwargs,
                             {'raise_errors': True, 'expected_exit_ip': '8.8.8.8'})
            saved = environment.load_config(path)
            self.assertTrue(saved['whpx_failed'])
            self.assertEqual(saved['whpx_failure_code'], 3489660927)
            with patch.object(backend, 'acceleration', return_value='whpx') as detect:
                self.assertEqual(backend.select_acceleration(saved), 'tcg')
                detect.assert_not_called()

    def test_network_failure_and_explicit_hardware_mode_do_not_trigger_retry(self):
        for failure, mode in [(RuntimeError('IP blocked'), 'auto'),
                              (subprocess.CalledProcessError(1, ['qemu']), 'whpx'),
                              (KeyboardInterrupt(), 'auto')]:
            with self.subTest(mode=mode, failure=type(failure).__name__), \
                 tempfile.TemporaryDirectory() as temporary:
                path, cfg = backend.config(Path(temporary))
                cfg['acceleration_mode'] = mode
                with patch.object(backend, 'acceleration', return_value='whpx'), \
                     patch.object(environment, 'main', side_effect=failure) as launch, \
                     patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
                    with self.assertRaises(type(failure)):
                        backend.start_environment(path, cfg)
                self.assertEqual(launch.call_count, 1)
                self.assertNotIn('whpx_failed', environment.load_config(path))

    def test_a_permanent_network_block_prevents_automatic_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = backend.config(Path(temporary))
            error = subprocess.CalledProcessError(3489660927, ['qemu'])
            error.initial_exit_ip, error.network_locked = '8.8.8.8', True
            with patch.object(backend, 'acceleration', return_value='whpx'), \
                 patch.object(environment, 'main', side_effect=error) as launch, \
                 patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
                with self.assertRaisesRegex(RuntimeError, 'Автоматический перезапуск отменён'):
                    backend.start_environment(path, cfg)
            self.assertEqual(launch.call_count, 1)

    def test_late_crash_is_not_automatically_restarted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = backend.config(Path(temporary))
            with patch.object(backend, 'acceleration', return_value='whpx'), \
                 patch.object(environment, 'main', side_effect=subprocess.CalledProcessError(1, ['qemu'])) as launch, \
                 patch.object(backend.time, 'monotonic', side_effect=[0, 181]), \
                 patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
                with self.assertRaises(subprocess.CalledProcessError):
                    backend.start_environment(path, cfg)
            self.assertEqual(launch.call_count, 1)

    def test_compatibility_setting_survives_launch_and_invalid_mode_is_rejected(self):
        with patch.object(backend, 'acceleration') as detect:
            self.assertEqual(backend.select_acceleration({'acceleration_mode': 'tcg'}), 'tcg')
            detect.assert_not_called()
            with self.assertRaises(ValueError):
                backend.select_acceleration({'acceleration_mode': 'whpx,ssd=off'})

    def test_whpx_does_not_advertise_nested_amd_virtualization(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = backend.config(Path(temporary))
            with patch.object(environment.platform, 'system', return_value='Windows'):
                cmd = environment.command(dict(cfg, accelerator='whpx'), check=False)
            self.assertEqual(cmd[cmd.index('-cpu') + 1], 'qemu64,svm=off')

    def test_recovery_cannot_launch_if_exit_ip_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = backend.config(Path(temporary))
            Path(cfg['disk']).touch()
            Path(cfg['seed']).touch()
            result = environment.network_guard.classify({'ip': '1.1.1.1', 'country': 'US'})
            with patch.object(sys, 'argv', ['core', 'start', '--config', str(path)]), \
                 patch.object(environment, 'command', return_value=['qemu']), \
                 patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.object(environment.network_guard, 'probe', return_value=result), \
                 patch.object(environment.subprocess, 'Popen') as process:
                with self.assertRaisesRegex(RuntimeError, 'IP изменился'):
                    environment.main(raise_errors=True, expected_exit_ip='8.8.8.8')
            process.assert_not_called()
            self.assertFalse(environment.network_guard.permitted(cfg['network_status']))

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

    def test_channel_close_eof_still_reaps_worker_and_releases_capacity(self):
        from windows.serial_gateway import Gateway
        gateway = Gateway({'network_mode': 'system', 'web_access': 'public'}, {})
        channel, process = Mock(), Mock()
        channel.recv.return_value = b''
        channel.close.side_effect = EOFError('SSH connection ended')
        process.stdout.read1.return_value = b''
        process.poll.return_value = None
        self.assertTrue(gateway.capacity.acquire(blocking=False))
        with patch('windows.serial_gateway.subprocess.Popen', return_value=process):
            gateway.channel(channel)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        process.stdout.close.assert_called_once()
        self.assertNotIn(process, gateway.processes)
        self.assertTrue(all(gateway.capacity.acquire(blocking=False) for _ in range(64)))
        self.assertFalse(gateway.capacity.acquire(blocking=False))

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
            self.assertIn('virtio-gpu-pci,edid=off,xres=1920,yres=1200', cmd)
            self.assertIn('usb-mouse', cmd)
            self.assertNotIn('usb-tablet', cmd)
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
