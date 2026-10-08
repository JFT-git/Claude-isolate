"""Fast start: saved VM memory is used once, only for the same VM and disk."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import environment
from network_guard import read_state, write_state
from windows import backend, state
from windows.control import Control, request

COMMAND = ['qemu', '-m', '3072', '-smp', '2', '-accel', 'tcg,thread=multi']


def environment_config(directory):
    path, cfg = backend.config(Path(directory))
    Path(cfg['disk']).write_bytes(b'disk')
    Path(cfg['seed']).write_bytes(b'seed')
    return path, cfg


class SavedStateTests(unittest.TestCase):
    def test_state_is_usable_only_for_the_same_command_and_unchanged_disk(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(state.subprocess, 'run') as tool:
            _, cfg = environment_config(temporary)
            state.record(cfg, COMMAND + ['-S'])
            self.assertTrue(state.usable(cfg, COMMAND))
            tool.assert_not_called()
            self.assertFalse(state.usable(cfg, COMMAND[:2] + ['4096'] + COMMAND[3:]))
            self.assertFalse(state.exists(cfg))
            # An invalid state's snapshot is removed from the disk image.
            self.assertEqual(tool.call_args.args[0][1:4], ['snapshot', '-d', state.TAG])

            state.record(cfg, COMMAND)
            os.utime(cfg['disk'], ns=(1, 1))
            self.assertFalse(state.usable(cfg, COMMAND))
            self.assertFalse(state.exists(cfg))

    def test_missing_metadata_is_never_usable_and_needs_no_image_tool(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(state.subprocess, 'run') as tool:
            _, cfg = environment_config(temporary)
            self.assertFalse(state.usable(cfg, COMMAND))
            tool.assert_not_called()

    def test_disk_uses_a_stable_node_name_for_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = backend.config(Path(temporary))
            with patch.object(environment.platform, 'system', return_value='Windows'):
                cmd = environment.command(dict(cfg, qemu_executable=sys.executable), check=False)
            disk = next(item for item in cmd if 'desktop.qcow2' in item)
            self.assertIn('node-name=' + state.NODE, disk)

    def qemu(self, script, commands, failures):
        client, server = socket.socketpair()
        jobs = {}
        def run():
            try:
                with server.makefile('rwb', buffering=0) as pipe:
                    pipe.write(b'{"QMP":{"version":{}}}\n')
                    while True:
                        line = pipe.readline()
                        if not line:
                            return
                        command = json.loads(line)
                        name, arguments = command['execute'], command.get('arguments') or {}
                        commands.append((name, arguments))
                        if name.startswith('snapshot-'):
                            jobs[arguments['job-id']] = script(name, arguments)
                            result = {}
                        elif name == 'query-jobs':
                            result = [dict(id=identity, status='concluded', **({'error': error} if error else {}))
                                      for identity, error in jobs.items()]
                        elif name == 'quit':
                            return
                        else:
                            result = {'running': True} if name == 'query-status' else {}
                        pipe.write(json.dumps({'id': command['id'], 'return': result}).encode() + b'\n')
            except Exception as error:
                failures.append(error)
            finally:
                server.close()
        threading.Thread(target=run, daemon=True).start()
        return client

    def control(self, cfg, client, restore=None):
        process = Mock()
        process.poll.return_value = None
        stream = client.makefile('rwb', buffering=0)
        owner = Control(cfg, process, reader_factory=lambda pipe: client.makefile('rb', buffering=0),
                        connector=lambda path: stream, restore=restore)
        return owner, process

    def test_restore_loads_snapshot_resumes_guest_and_removes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = environment_config(temporary)
            write_state(state.metadata(cfg), {'identity': 'x'})
            commands, failures = [], []
            client = self.qemu(lambda name, arguments: None, commands, failures)
            owner, process = self.control(cfg, client, restore=True)
            try:
                owner.start()
                self.assertTrue(owner.wait_ready(5))
                self.assertTrue(owner.restored)
                self.assertTrue(read_state(owner.files['ready'])['restored'])
                names = [name for name, _ in commands]
                load = names.index('snapshot-load')
                self.assertLess(load, names.index('cont'))
                self.assertLess(names.index('cont'), names.index('snapshot-delete'))
                self.assertEqual(commands[load][1]['tag'], state.TAG)
                self.assertEqual(commands[load][1]['devices'], [state.NODE])
                self.assertFalse(state.exists(cfg), 'A resumed guest must not reuse its saved memory')
            finally:
                process.poll.return_value = 0
                owner.close()
                client.close()
            self.assertFalse(failures)

    def test_failed_load_does_not_resume_the_paused_guest(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = environment_config(temporary)
            commands, failures = [], []
            client = self.qemu(lambda name, arguments: 'Snapshot not found', commands, failures)
            owner, process = self.control(cfg, client, restore=True)
            process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 1)
            try:
                owner.start()
                with self.assertRaisesRegex(RuntimeError, 'не загрузилось'):
                    owner.wait_ready(5)
                self.assertFalse(owner.restored)
                self.assertNotIn('cont', [name for name, _ in commands])
            finally:
                process.poll.return_value = 0
                owner.close()
                client.close()

    def test_suspend_saves_snapshot_and_quits_qemu(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = environment_config(temporary)
            commands, failures = [], []
            client = self.qemu(lambda name, arguments: None, commands, failures)
            owner, process = self.control(cfg, client)
            try:
                owner.start()
                self.assertTrue(owner.wait_ready(5))
                self.assertEqual(request(cfg, 'suspend', timeout=5), {'saved': True})
                deadline = time.monotonic() + 5
                while 'quit' not in [name for name, _ in commands] and time.monotonic() < deadline:
                    time.sleep(.02)
                names = [name for name, _ in commands]
                self.assertLess(names.index('stop'), names.index('snapshot-save'))
                save = names.index('snapshot-save')
                self.assertEqual(commands[save][1]['vmstate'], state.NODE)
                self.assertEqual(names[-1], 'quit')
                self.assertTrue(owner.suspended)
            finally:
                process.poll.return_value = 0
                owner.close()
                client.close()
            self.assertFalse(failures)

    def test_failed_save_resumes_guest_and_removes_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = environment_config(temporary)
            commands, failures = [], []
            client = self.qemu(lambda name, arguments: 'No space left' if name == 'snapshot-save' else None,
                               commands, failures)
            owner, process = self.control(cfg, client)
            try:
                owner.start()
                self.assertTrue(owner.wait_ready(5))
                with self.assertRaisesRegex(RuntimeError, 'Не удалось сохранить'):
                    request(cfg, 'suspend', timeout=5)
                names = [name for name, _ in commands]
                self.assertEqual(names[-1], 'cont')
                self.assertEqual(names[-2], 'job-dismiss')
                self.assertIn('snapshot-delete', names[names.index('snapshot-save'):])
                self.assertNotIn('quit', names)
                self.assertFalse(owner.suspended)
                self.assertFalse(state.exists(cfg))
            finally:
                process.poll.return_value = 0
                owner.close()
                client.close()
            self.assertFalse(failures)

    def launch(self, cfg, path, process):
        result = environment.network_guard.classify({'ip': '1.1.1.1', 'country': 'US'})
        control = Mock(restored=False, suspended=False)
        with patch.object(sys, 'argv', ['core', 'start', '--config', str(path)]), \
             patch.object(environment, 'command', return_value=list(COMMAND)), \
             patch.object(environment.platform, 'system', return_value='Windows'), \
             patch.object(environment.network_guard, 'probe', return_value=result), \
             patch.object(environment.network_guard, 'monitor'), \
             patch.object(environment.subprocess, 'Popen', return_value=process) as start, \
             patch('windows.control.Control', return_value=control) as owner, \
             patch('windows.serial_gateway.Gateway'), patch('builtins.print'):
            try:
                environment.main(raise_errors=True)
            finally:
                self.started = start.call_args.args[0]
                self.restore = owner.call_args.kwargs.get('restore')

    def test_unloadable_state_raises_restore_failed_for_a_normal_boot(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = environment_config(temporary)
            state.record(cfg, COMMAND)
            process = Mock()
            process.poll.return_value = 1
            process.returncode = 1
            with self.assertRaises(environment.RestoreFailed):
                self.launch(cfg, path, process)
            self.assertEqual(self.started[-1], '-S')
            self.assertTrue(self.restore)

    def test_normal_boot_without_state_starts_the_guest_immediately(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = environment_config(temporary)
            process = Mock()
            process.poll.return_value = 0
            process.returncode = 0
            self.launch(cfg, path, process)
            self.assertNotIn('-S', self.started)
            self.assertIsNone(self.restore)

    def test_backend_falls_back_to_a_normal_boot_after_restore_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = environment_config(temporary)
            write_state(state.metadata(cfg), {'identity': 'x'})
            with patch.object(backend, 'acceleration', return_value='tcg'), \
                 patch.object(environment, 'main', side_effect=[environment.RestoreFailed('x'), None]) as launch, \
                 patch.object(state.subprocess, 'run') as tool, \
                 patch.object(sys, 'argv', ['core']), patch.object(backend, 'emit'):
                backend.start_environment(path, cfg)
            self.assertEqual(launch.call_count, 2)
            self.assertFalse(state.exists(cfg))
            self.assertEqual(tool.call_args.args[0][1:4], ['snapshot', '-d', state.TAG])

    def test_automatic_resources_keep_the_saved_memory_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, cfg = environment_config(temporary)
            cfg.update(memory_mb=2048, cpus=2, resources_mode='auto')
            write_state(state.metadata(cfg), {'identity': 'x'})
            with patch.object(backend, 'automatic_resources', return_value=dict(memory_mb=3072, cpus=2)), \
                 patch.object(backend, 'warn_memory'), patch.object(state.subprocess, 'run'):
                backend.configure_resources(path, cfg)
                self.assertEqual(cfg['memory_mb'], 2048)
                state.discard(cfg)
                backend.configure_resources(path, cfg)
                self.assertEqual(cfg['memory_mb'], 3072)

    def test_restored_guest_is_reported_ready_without_a_boot_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, cfg = environment_config(temporary)
            from windows.control import paths as control_paths
            write_state(control_paths(cfg)['ready'], dict(session='s', ready=True, restored=True))
            self.assertEqual(backend.status(cfg, True)['message'], 'Рабочий стол готов')


@unittest.skipIf(os.name == 'nt', 'The guest proxy reads Linux file descriptors')
class GuestProxyClockTests(unittest.TestCase):
    def test_acknowledgement_sets_the_guest_clock_and_returns_the_banner(self):
        spec = importlib.util.spec_from_file_location('serial_stream', environment.ROOT / 'guest/serial-stream.py')
        guest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guest)
        host, device = socket.socketpair()
        def respond():
            marker = host.recv(4096)
            nonce = marker.split(b'SYNC-')[1].strip()
            host.sendall(b'\x00CLAUDE-RESET\nstale' + b'\x00CLAUDE-ACK-' + nonce + b' 1700000000123\nSSH-2.0-host\r\n')
        threading.Thread(target=respond, daemon=True).start()
        clock = Mock()
        try:
            self.assertEqual(guest.handshake(device.fileno(), time.monotonic() + 5, clock), b'SSH-2.0-host\r\n')
            clock.assert_called_once_with(1700000000123)
        finally:
            host.close()
            device.close()


if __name__ == '__main__':
    unittest.main()
