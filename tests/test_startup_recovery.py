"""Launches that used to fail on some PCs: WHPX errors, busy ports, slow hosts."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import environment
import host_access
import network_guard
from windows import backend, state
from windows.shared_sync import _LocalhostKeys


def qemu_failure(code, detail='', ip='8.8.8.8'):
    error = environment.QemuFailed(code, ['qemu'], detail)
    error.initial_exit_ip = ip
    return error


class WhpxFallbackTests(unittest.TestCase):
    def launch(self, failure):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path, cfg = backend.config(Path(temporary.name))
        with patch.object(backend, 'acceleration', return_value='whpx'), \
             patch.object(backend, 'StallWatch', NoWatch), \
             patch.object(environment, 'main', side_effect=[failure, None]) as launch, \
             patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
            try:
                backend.start_environment(path, cfg)
            except subprocess.CalledProcessError as error:
                return launch, environment.load_config(path), error
        return launch, environment.load_config(path), None

    def test_whpx_init_error_before_control_is_ready_falls_back_to_tcg(self):
        launch, saved, error = self.launch(qemu_failure(1, 'qemu-system-x86_64.exe: WHPX: Failed to setup partition'))
        self.assertIsNone(error)
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(launch.call_args_list[1].kwargs['expected_exit_ip'], '8.8.8.8')
        self.assertTrue(saved['whpx_failed'])
        self.assertEqual(saved['accelerator'], 'tcg')

    def test_setting_error_is_shown_and_does_not_switch_to_slow_mode(self):
        detail = "-netdev user,id=isolated: Could not set up host forwarding rule 'tcp:127.0.0.1:2222-:22'"
        launch, saved, error = self.launch(qemu_failure(1, detail))
        self.assertIsNotNone(error)
        self.assertIn('host forwarding', str(error))
        self.assertNotIn('Command', str(error))
        self.assertEqual(launch.call_count, 1)
        self.assertNotIn('whpx_failed', saved)

    def test_crash_code_without_output_still_falls_back(self):
        launch, saved, error = self.launch(qemu_failure(3221225477))
        self.assertIsNone(error)
        self.assertEqual(saved['whpx_failure_code'], 3221225477)

    def test_fallback_keeps_settings_saved_during_the_failed_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = backend.config(Path(temporary))

            calls = []

            def main(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    # The launch moved SSH to another port, then QEMU crashed.
                    environment.save_config(path, dict(environment.load_config(path), ssh_port=2230))
                    raise qemu_failure(5)

            with patch.object(backend, 'acceleration', return_value='whpx'), \
                 patch.object(backend, 'StallWatch', NoWatch), \
                 patch.object(environment, 'main', side_effect=main), \
                 patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
                backend.start_environment(path, cfg)
            self.assertEqual(len(calls), 2)
            saved = environment.load_config(path)
            self.assertEqual(saved['ssh_port'], 2230)
            self.assertEqual(saved['accelerator'], 'tcg')

    def test_stalled_whpx_boot_is_abandoned_and_restarted_without_whpx(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = backend.config(Path(temporary))
            calls = []

            class Stalled(NoWatch):
                def __enter__(self):
                    self.stalled = True
                    return self

            def main(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    error = environment.LaunchAbandoned('Запуск Linux прерван')
                    error.initial_exit_ip, error.network_locked = '8.8.8.8', False
                    raise error

            with patch.object(backend, 'acceleration', return_value='whpx'), \
                 patch.object(backend, 'StallWatch', Stalled), \
                 patch.object(environment, 'main', side_effect=main), \
                 patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
                backend.start_environment(path, cfg)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]['expected_exit_ip'], '8.8.8.8')
            self.assertEqual(environment.load_config(path)['whpx_failure_code'], 'stall')
            self.assertFalse(environment.ABORT.is_set())

    def test_user_abort_is_not_mistaken_for_a_stall(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = backend.config(Path(temporary))
            with patch.object(backend, 'acceleration', return_value='whpx'), \
                 patch.object(backend, 'StallWatch', NoWatch), \
                 patch.object(environment, 'main', side_effect=environment.LaunchAbandoned('x')) as launch, \
                 patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
                with self.assertRaises(environment.LaunchAbandoned):
                    backend.start_environment(path, cfg)
            self.assertEqual(launch.call_count, 1)

    def test_stall_watch_fires_only_for_a_silent_guest_that_was_not_restored(self):
        from windows.control import paths
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = backend.config(Path(temporary))
            ready = paths(cfg)['ready']
            for restored, log, expected in [(False, '', True), (True, '', False), (False, 'Linux version 6.8', False)]:
                with self.subTest(restored=restored, log=log):
                    environment.ABORT.clear()
                    Path(cfg['boot_log']).write_text(log, encoding='utf-8')
                    network_guard.write_state(ready, dict(session='s', ready=True, restored=restored))
                    watch = backend.StallWatch(cfg, timeout=.2, poll=.05)
                    with watch:
                        time.sleep(.6)
                    self.assertEqual(watch.stalled, expected)
                    self.assertEqual(environment.ABORT.is_set(), expected)
            environment.ABORT.clear()


class NoWatch:
    def __init__(self, cfg, *args, **kwargs):
        self.stalled = False

    def __enter__(self):
        return self

    def __exit__(self, *details):
        return False


class EarlyQemuExitTests(unittest.TestCase):
    def test_qemu_exit_while_control_starts_is_reported_as_a_qemu_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = backend.config(Path(temporary))
            Path(cfg['disk']).write_bytes(b'disk')
            Path(cfg['seed']).write_bytes(b'seed')
            process = Mock()
            process.poll.return_value = 1
            process.returncode = 1
            control = Mock()
            control.wait_ready.side_effect = RuntimeError('Канал управления QEMU прерван')
            control.restored = False
            with patch.object(sys, 'argv', ['core', 'start', '--config', str(path)]), \
                 patch.object(environment, 'command', return_value=['qemu']), \
                 patch.object(environment, 'choose_ssh_port', return_value=False), \
                 patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.object(environment.subprocess, 'Popen', return_value=process), \
                 patch.object(environment, 'qemu_detail', return_value='failed to initialize whpx: Invalid argument'), \
                 patch('windows.control.Control', return_value=control), \
                 patch('windows.serial_gateway.Gateway'), \
                 patch('windows.clipboard_bridge.ClipboardBridge'), \
                 patch('windows.shared_sync.SharedSync'):
                with self.assertRaises(environment.QemuFailed) as caught:
                    environment.main(raise_errors=True, offline=True)
            self.assertEqual(caught.exception.returncode, 1)
            self.assertIn('whpx', caught.exception.detail)
            self.assertTrue(backend.whpx_failure(caught.exception))
            self.assertEqual(str(caught.exception), 'QEMU завершился с кодом 1: failed to initialize whpx: Invalid argument')

    def test_crash_codes_are_shown_in_hex(self):
        self.assertIn('0xC0000005', str(environment.QemuFailed(3221225477, ['qemu'])))


class StalledQemuTests(unittest.TestCase):
    """QEMU hangs for good if a client connects between CreateNamedPipe and ConnectNamedPipe."""

    def start(self, failures):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path, cfg = backend.config(Path(temporary.name))
        calls = []

        def main(**kwargs):
            calls.append(kwargs)
            if failures:
                raise failures.pop(0)

        with patch.object(backend, 'acceleration', return_value='tcg'), \
             patch.object(environment, 'main', side_effect=main), \
             patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
            backend.start_environment(path, cfg)
        return calls

    def stalled(self, network_locked=False):
        error = environment.QemuStalled('Windows QMP pipe did not start')
        error.initial_exit_ip, error.network_locked = '8.8.8.8', network_locked
        return error

    def test_qemu_that_never_opened_its_pipe_is_started_once_more(self):
        calls = self.start([self.stalled()])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['expected_exit_ip'], '8.8.8.8')

    def test_second_stall_and_network_change_are_reported(self):
        with self.assertRaises(environment.QemuStalled):
            self.start([self.stalled(), self.stalled()])
        with self.assertRaises(environment.QemuStalled):
            self.start([self.stalled(network_locked=True)])

    def test_stall_keeps_the_saved_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = backend.config(Path(temporary))
            Path(cfg['disk']).write_bytes(b'disk')
            Path(cfg['seed']).write_bytes(b'seed')
            process = Mock()
            process.poll.return_value = None
            control = Mock()
            control.wait_ready.side_effect = environment.QemuStalled('Windows QMP pipe did not start')
            control.restored = False
            with patch.object(sys, 'argv', ['core', 'start', '--config', str(path)]), \
                 patch.object(environment, 'command', return_value=['qemu']), \
                 patch.object(environment, 'choose_ssh_port', return_value=False), \
                 patch.object(environment.platform, 'system', return_value='Windows'), \
                 patch.object(environment.subprocess, 'Popen', return_value=process), \
                 patch.object(state, 'usable', return_value=True), \
                 patch('windows.control.Control', return_value=control), \
                 patch('windows.serial_gateway.Gateway'), \
                 patch('windows.clipboard_bridge.ClipboardBridge'), \
                 patch('windows.shared_sync.SharedSync'):
                # Not RestoreFailed, which would discard the saved state.
                with self.assertRaises(environment.QemuStalled):
                    environment.main(raise_errors=True, offline=True)
            process.terminate.assert_called_once()

    def test_control_reports_a_pipe_that_never_appeared_as_a_stall(self):
        from windows.control import Control

        def missing(path):
            raise FileNotFoundError(path)

        with tempfile.TemporaryDirectory() as temporary:
            process = Mock()
            process.poll.return_value = None
            owner = Control({'disk': str(Path(temporary) / 'disk'), 'qmp_pipe': 'claude-isolate-' + 'ab' * 16},
                            process, connector=missing)
            with patch('windows.control.PIPE_TIMEOUT', 0):
                owner.run()
            self.assertIsInstance(owner.error, environment.QemuStalled)
            with self.assertRaises(environment.QemuStalled):
                owner.wait_ready(1)
            # Once connected, a slow answer is not a stall: QEMU may run the guest.
            owner.error, owner.connected = None, True
            with patch('windows.control.time.monotonic', side_effect=[0, 100]), \
                 self.assertRaises(RuntimeError) as caught:
                owner.wait_ready(1)
            self.assertNotIsInstance(caught.exception, environment.QemuStalled)


class SshPortTests(unittest.TestCase):
    def test_a_busy_port_is_replaced_and_a_free_one_is_kept(self):
        with socket.socket() as holder:
            holder.bind(('127.0.0.1', 0))
            holder.listen()
            busy = holder.getsockname()[1]
            cfg = {'ssh_port': busy}
            self.assertFalse(environment.port_available(busy))
            self.assertTrue(environment.choose_ssh_port(cfg))
            self.assertNotEqual(cfg['ssh_port'], busy)
            self.assertTrue(environment.port_available(cfg['ssh_port']))
            chosen = cfg['ssh_port']
            self.assertFalse(environment.choose_ssh_port(cfg))
            self.assertEqual(cfg['ssh_port'], chosen)

    def test_qemu_forwards_the_configured_port_on_every_host(self):
        cfg = environment.load_config(environment.ROOT / 'environment.example.json')
        cfg.update(ssh_port=2244, qmp_socket='/private/tmp/claude-env-test/control.sock')
        for system in ('Darwin', 'Linux', 'Windows'):
            with patch.object(environment.platform, 'system', return_value=system), \
                 patch.object(environment.platform, 'machine', return_value='arm64'), \
                 patch('windows.qemu_paths.adopt_qemu_paths', side_effect=lambda value, **kwargs: value), \
                 patch('windows.qemu_paths.assert_qemu_argv', side_effect=lambda value: value), \
                 patch.object(Path, 'mkdir'):
                cmd = environment.command(dict(cfg, arch='x86_64', firmware=None, qmp_socket=None)
                                          if system == 'Windows' else cfg, check=False)
            net = cmd[cmd.index('-netdev') + 1]
            self.assertIn('hostfwd=tcp:127.0.0.1:2244-:22', net)

    def test_invalid_port_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'environment.json'
            cfg = json.loads((environment.ROOT / 'environment.example.json').read_text())
            for port in (22, 70000, '2222'):
                with self.subTest(port=port):
                    path.write_text(json.dumps(dict(cfg, ssh_port=port)), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        environment.load_config(path)

    def test_shared_folder_accepts_the_localhost_key_on_any_port(self):
        client = Mock()
        _LocalhostKeys().missing_host_key(client, '[127.0.0.1]:2244', Mock())
        client.get_host_keys().add.assert_called_once()
        with self.assertRaises(OSError):
            _LocalhostKeys().missing_host_key(Mock(), '[127.0.0.2]:2244', Mock())
        with self.assertRaises(OSError):
            _LocalhostKeys().missing_host_key(Mock(), '[127.0.0.1]:2244.evil', Mock())


class SavedStateIdentityTests(unittest.TestCase):
    COMMAND = ['C:\\Users\\a\\runtime\\qemu\\qemu-system-x86_64.exe', '-m', '3072',
               '-drive', 'file=Z:\\desktop.qcow2,if=virtio,format=qcow2,node-name=claude-disk',
               '-netdev', 'user,id=isolated,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:2222-:22',
               '-L', 'Z:\\qemu-firmware', '-serial', 'file:Z:\\boot.log']

    def test_drive_letter_port_and_install_path_do_not_discard_the_fast_start(self):
        moved = [item.replace('Z:\\', 'Y:\\').replace(':2222-', ':2230-') for item in self.COMMAND]
        moved[0] = 'D:\\Apps\\qemu-system-x86_64.exe'
        cfg = {'guest_revision': 'r'}
        self.assertEqual(state.identity(cfg, self.COMMAND), state.identity(cfg, moved))
        other_memory = list(self.COMMAND)
        other_memory[2] = '4096'
        self.assertNotEqual(state.identity(cfg, self.COMMAND), state.identity(cfg, other_memory))
        other_disk = [item.replace('if=virtio', 'if=ide') for item in self.COMMAND]
        self.assertNotEqual(state.identity(cfg, self.COMMAND), state.identity(cfg, other_disk))

    def test_a_state_saved_by_an_older_version_is_still_restored(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(state.subprocess, 'run'):
            _, cfg = backend.config(Path(temporary))
            Path(cfg['disk']).write_bytes(b'disk')
            network_guard.write_state(state.metadata(cfg), dict(
                identity=state.legacy_identity(cfg, self.COMMAND), disk=state.disk_stamp(cfg), saved=0))
            self.assertTrue(state.usable(cfg, self.COMMAND))

    def test_a_different_qemu_version_does_not_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            qemu = Path(temporary) / 'qemu-system-x86_64.exe'
            (Path(temporary) / 'VERSION').write_text('11.1.0\n', encoding='utf-8')
            cfg = {'guest_revision': 'r', 'qemu_executable': str(qemu)}
            before = state.identity(cfg, self.COMMAND)
            (Path(temporary) / 'VERSION').write_text('11.2.0\n', encoding='utf-8')
            self.assertNotEqual(before, state.identity(cfg, self.COMMAND))


class ConfigFileTests(unittest.TestCase):
    def test_saved_configuration_is_readable_utf8_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'environment.json'
            cfg = json.loads((environment.ROOT / 'environment.example.json').read_text())
            cfg['disk'] = str(Path(temporary) / 'Александр' / 'desktop.qcow2')
            environment.save_config(path, cfg)
            self.assertIn('Александр', path.read_text(encoding='utf-8'))
            self.assertEqual(environment.load_config(path)['disk'], cfg['disk'])

    @unittest.skipUnless(os.name == 'nt', 'Windows rename semantics')
    def test_configuration_is_replaced_while_another_process_reads_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'environment.json'
            cfg = json.loads((environment.ROOT / 'environment.example.json').read_text())
            environment.save_config(path, cfg)
            with network_guard._state_stream(path):
                environment.save_config(path, dict(cfg, memory_mb=4096))
            self.assertEqual(environment.load_config(path)['memory_mb'], 4096)


class DriveLetterTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'DefineDosDeviceW')
    def test_a_disconnected_network_drive_letter_is_not_taken(self):
        from windows import qemu_paths
        with patch.object(qemu_paths, '_logical_letters', return_value={'C'}), \
             patch.object(qemu_paths, '_remembered_network_letters', return_value={'Z'}), \
             patch.object(qemu_paths, '_query_target', return_value=None), \
             patch.object(qemu_paths, '_define_dos_device') as define, \
             patch.object(qemu_paths, '_targets_match', return_value=True):
            self.assertEqual(qemu_paths.assign_drive(Path('C:/Users/u/AppData/Local/Claude Isolate')), 'Y')
        self.assertEqual(define.call_args.args[0], 'Y')


class QemuWindowTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Win32 windows')
    def test_window_lookup_ignores_other_processes_and_stops_on_request(self):
        import threading
        from windows import window
        self.assertIsNone(window.find_window(0x7FFFFFF0))
        stop = threading.Event()
        stop.set()
        self.assertFalse(window.maximize_when_shown(os.getpid(), stop, timeout=1))


class LinuxHostTests(unittest.TestCase):
    def test_linux_without_kvm_access_uses_software_emulation(self):
        cfg = environment.load_config(environment.ROOT / 'environment.example.json')
        cfg['arch'] = 'x86_64'
        for usable, accel, cpu in [(True, 'kvm', 'host'), (False, 'tcg', 'max')]:
            with self.subTest(kvm=usable), \
                 patch.object(environment.platform, 'system', return_value='Linux'), \
                 patch.object(environment.platform, 'machine', return_value='x86_64'), \
                 patch.object(environment, 'kvm_usable', return_value=usable):
                cmd = environment.command(cfg, check=False)
            self.assertEqual(cmd[cmd.index('-accel') + 1], accel)
            self.assertEqual(cmd[cmd.index('-cpu') + 1], cpu)

    def test_missing_xclip_is_reported_once_instead_of_failing_silently(self):
        with patch.object(host_access.sys, 'platform', 'linux'), \
             patch.object(host_access.shutil, 'which', return_value=None), \
             patch('builtins.print') as shown:
            self.assertTrue(host_access.clipboard_tool_missing())
        self.assertIn('xclip', shown.call_args.args[0])

    def test_linux_clipboard_is_polled_without_a_watcher(self):
        with patch.object(host_access.sys, 'platform', 'linux'):
            changes = host_access.ClipboardChanges()
        self.assertIsNone(changes.process)
        self.assertTrue(changes.changed())
        changes.close()


if __name__ == '__main__':
    unittest.main()
