import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import environment
from session_lock import exclusive
from windows import backend, guest_update

spec = importlib.util.spec_from_file_location('offline_update', environment.ROOT / 'guest/offline-update.py')
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


class GuestUpdateTests(unittest.TestCase):
    def guest(self, root):
        (root / 'etc').mkdir()
        (root / 'etc/os-release').write_text('ID=ubuntu\n')
        (root / 'etc/passwd').write_text('claude:x:1000:1000::/home/claude:/bin/bash\n')
        (root / 'etc/environment').write_text('CUSTOM=value\nhttps_proxy="old"\n')
        home = root / 'home/claude/.config'
        home.mkdir(parents=True)
        (home / 'preferences.json').write_bytes(b'{"language":"fr", "theme":"custom"}')
        (home / 'account-state').write_bytes(b'local-test-fixture')
        (root / 'etc/cloud').mkdir()
        (root / 'etc/cloud/cloud-init.disabled').touch()

    @unittest.skipIf(os.name == 'nt', 'Guest installation uses Linux filesystem semantics')
    def test_offline_update_preserves_profiles_and_environment_with_cloud_init_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.guest(root)
            _, cfg = backend.config(root / 'controller')
            before = {str(p.relative_to(root)): p.read_bytes() for p in (root / 'home').rglob('*') if p.is_file()}
            content = guest_update.payload(cfg)
            offline.apply(root, content)
            offline.apply(root, content)
            after = {str(p.relative_to(root)): p.read_bytes() for p in (root / 'home').rglob('*') if p.is_file()}
            self.assertEqual(after, before)
            self.assertTrue((root / 'etc/cloud/cloud-init.disabled').is_file())
            self.assertIn('CUSTOM=value', (root / 'etc/environment').read_text())
            self.assertNotIn('"old"', (root / 'etc/environment').read_text())
            self.assertEqual((root / 'etc/claude-isolate/revision').read_text().strip(), guest_update.REVISION)
            self.assertTrue((root / 'etc/systemd/system/multi-user.target.wants/claude-gateway.service').is_symlink())

    @unittest.skipIf(os.name == 'nt', 'Unprivileged Windows symlinks are not universally available')
    def test_update_rejects_symlinks_to_profile_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.guest(root)
            _, cfg = backend.config(root / 'controller')
            (root / 'usr').mkdir()
            (root / 'usr/local').symlink_to(root / 'home/claude/.config', target_is_directory=True)
            with self.assertRaises(ValueError):
                offline.apply(root, guest_update.payload(cfg))
            self.assertEqual((root / 'etc/environment').read_text(), 'CUSTOM=value\nhttps_proxy="old"\n')
            self.assertFalse((root / 'etc/claude-isolation.nft').exists())

    def transaction(self, temporary):
        data = Path(temporary)
        path, cfg = backend.config(data)
        cfg.update(qemu_executable=str(data / 'qemu-system-x86_64'), guest_revision='0.3.12')
        backend.write_config(path, cfg)
        Path(cfg['disk']).write_bytes(b'original-user-image')
        folder = data / 'original-seed-files'
        folder.mkdir()
        (folder / 'meta-data').write_bytes(b'instance-id: existing-instance\n')
        (folder / 'user-data').write_text('#cloud-config\n{}\n')
        backend.seed_iso(folder, Path(cfg['seed']))
        base = data / 'base.qcow2'
        base.write_bytes(b'verified-base')
        snapshots = {}
        calls = []
        def image(cfg, *args):
            calls.append(args)
            if args[0] == 'info':
                return json.dumps(dict(format='qcow2', snapshots=[dict(name=n) for n in snapshots]))
            if args[:2] == ('snapshot', '-c'):
                snapshots[args[2]] = Path(cfg['disk']).read_bytes()
            if args[:2] == ('snapshot', '-a'):
                Path(cfg['disk']).write_bytes(snapshots[args[2]])
            return ''
        return data, path, cfg, base, image, calls

    def test_upgrade_keeps_disk_path_and_seed_identity_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, path, cfg, base, image, calls = self.transaction(temporary)
            original_path = cfg['disk']
            original_seed = Path(cfg['seed']).read_bytes()
            def maintain(cfg, base, temporary, content):
                Path(cfg['disk']).write_bytes(b'updated-user-image')
            with patch.object(guest_update, 'image_command', side_effect=image), \
                 patch.object(ubuntu_image := guest_update.ubuntu_image, 'download', return_value=(base, 'digest')), \
                 patch.object(ubuntu_image, 'matches', return_value=True), \
                 patch.object(backend, 'find_tool', return_value='gpg'), patch.object(backend, 'emit'), \
                 patch.object(guest_update, 'maintenance', side_effect=maintain) as maintain_mock:
                guest_update.upgrade(data, cfg)
                guest_update.upgrade(data, cfg)
            maintain_mock.assert_called_once()
            self.assertEqual(cfg['disk'], original_path)
            self.assertEqual(Path(cfg['guest_update_seed_backup']).read_bytes(), original_seed)
            import io
            import pycdlib
            metadata = io.BytesIO()
            iso = pycdlib.PyCdlib()
            iso.open(cfg['seed'])
            iso.get_file_from_iso_fp(metadata, rr_path='/meta-data')
            iso.close()
            self.assertEqual(metadata.getvalue(), b'instance-id: existing-instance\n')
            self.assertEqual(environment.load_config(path)['guest_revision'], guest_update.REVISION)
            self.assertFalse((data / 'guest-update-transaction.json').exists())
            self.assertEqual(sum(args[:2] == ('snapshot', '-c') for args in calls), 1)

    def test_failed_or_cancelled_update_restores_disk_and_configuration(self):
        for failure in (RuntimeError('Update failed'), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                data, path, cfg, base, image, calls = self.transaction(temporary)
                original = dict(cfg)
                original_seed = Path(cfg['seed']).read_bytes()
                def maintain(*args):
                    Path(cfg['disk']).write_bytes(b'partial-write')
                    raise failure
                with patch.object(guest_update, 'image_command', side_effect=image), \
                     patch.object(guest_update.ubuntu_image, 'download', return_value=(base, 'digest')), \
                     patch.object(guest_update.ubuntu_image, 'matches', return_value=True), \
                     patch.object(backend, 'find_tool', return_value='gpg'), patch.object(backend, 'emit'), \
                     patch.object(guest_update, 'maintenance', side_effect=maintain):
                    with self.assertRaises(type(failure)):
                        guest_update.upgrade(data, cfg)
                self.assertEqual(Path(cfg['disk']).read_bytes(), b'original-user-image')
                self.assertEqual(Path(cfg['seed']).read_bytes(), original_seed)
                self.assertEqual(environment.load_config(path), original)
                self.assertEqual(cfg, original)
                self.assertFalse((data / 'guest-update-transaction.json').exists())

    def test_configuration_commit_failure_restores_modified_seed_and_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, path, cfg, base, image, _ = self.transaction(temporary)
            original_seed = Path(cfg['seed']).read_bytes()
            original = dict(cfg)
            real_write = backend.write_config
            def write(destination, content):
                if destination == path and content.get('guest_revision') == guest_update.REVISION:
                    raise OSError('Simulated failed configuration commit')
                real_write(destination, content)
            def maintain(*args):
                Path(cfg['disk']).write_bytes(b'updated-root')
            with patch.object(guest_update, 'image_command', side_effect=image), \
                 patch.object(guest_update.ubuntu_image, 'download', return_value=(base, 'digest')), \
                 patch.object(guest_update.ubuntu_image, 'matches', return_value=True), \
                 patch.object(backend, 'find_tool', return_value='gpg'), patch.object(backend, 'emit'), \
                 patch.object(backend, 'write_config', side_effect=write), \
                 patch.object(guest_update, 'maintenance', side_effect=maintain):
                with self.assertRaises(OSError):
                    guest_update.upgrade(data, cfg)
            self.assertEqual(Path(cfg['seed']).read_bytes(), original_seed)
            self.assertEqual(Path(cfg['disk']).read_bytes(), b'original-user-image')
            self.assertEqual(environment.load_config(path), original)

    def test_running_disk_cannot_be_upgraded(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, _, cfg, _, _, _ = self.transaction(temporary)
            with exclusive(Path(cfg['disk']).with_suffix('.launch.lock')), \
                 patch.object(guest_update.ubuntu_image, 'download') as download:
                with self.assertRaises(RuntimeError):
                    guest_update.upgrade(data, cfg)
                download.assert_not_called()

    def test_crash_recovery_and_committed_transaction_does_not_undo_user_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, path, cfg, _, image, calls = self.transaction(temporary)
            original = dict(cfg)
            image(cfg, 'snapshot', '-c', 'recovery', cfg['disk'])
            transaction = dict(disk=cfg['disk'], snapshot='recovery', revision=guest_update.REVISION,
                               configuration=original, phase='updating')
            journal = data / 'guest-update-transaction.json'
            backend.write_config(journal, transaction)
            Path(cfg['disk']).write_bytes(b'interrupted-update')
            with patch.object(guest_update, 'image_command', side_effect=image), patch.object(backend, 'emit'):
                guest_update.recover(data, cfg)
            self.assertEqual(Path(cfg['disk']).read_bytes(), b'original-user-image')
            cfg.update(guest_revision=guest_update.REVISION, guest_update_snapshot='recovery')
            backend.write_config(path, cfg)
            backend.write_config(journal, transaction)
            Path(cfg['disk']).write_bytes(b'user-edits-after-success')
            with patch.object(guest_update, 'image_command') as tool:
                guest_update.recover(data, cfg)
                tool.assert_not_called()
            self.assertEqual(Path(cfg['disk']).read_bytes(), b'user-edits-after-success')

    def test_installer_upgrade_does_not_create_or_download_first_guest(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(guest_update.ubuntu_image, 'download') as download, \
             patch.object(backend, 'emit'), \
             patch('sys.argv', ['core', 'upgrade', '--data', temporary]):
            backend.main()
            download.assert_not_called()
            self.assertFalse((Path(temporary) / 'environment.json').exists())

    def test_maintenance_starts_without_user_disk_and_has_no_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, _, cfg, base, _, _ = self.transaction(temporary)
            helper = data / 'helper'
            helper.mkdir()
            with patch.object(guest_update, 'image_command'), \
                 patch.object(backend, 'seed_iso'), \
                 patch.object(guest_update, 'run_helper', side_effect=subprocess.TimeoutExpired('qemu', 1200)) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    guest_update.maintenance(cfg, base, helper, guest_update.payload(cfg))
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('-nic') + 1], 'none')
            self.assertNotIn('-virtfs', command)
            self.assertNotIn('-netdev', command)
            self.assertFalse(any(cfg['disk'] in argument for argument in command))
            self.assertIn('pcie-root-port,id=update-port,chassis=1,slot=1', command)

    def test_broken_helper_control_reaps_process_before_returning_for_rollback(self):
        import io
        with tempfile.TemporaryDirectory() as temporary:
            data, _, cfg, _, _, _ = self.transaction(temporary)
            class Process:
                returncode = None
                stdin = io.BytesIO()
                stdout = io.BytesIO()
                def poll(self):
                    return self.returncode
                def kill(self):
                    self.returncode = -1
                def wait(self, timeout):
                    return self.returncode
            process = Process()
            with patch.object(guest_update.subprocess, 'Popen', return_value=process):
                if os.name == 'nt':
                    from windows import job
                    with patch.object(job, 'Job'):
                        with self.assertRaises(RuntimeError):
                            guest_update.run_helper(['qemu'], cfg, data / 'boot.log', 'ready')
                else:
                    with self.assertRaises(RuntimeError):
                        guest_update.run_helper(['qemu'], cfg, data / 'boot.log', 'ready')
            self.assertEqual(process.returncode, -1)
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)

    def test_user_disk_is_attached_only_after_helper_root_ready(self):
        import queue
        with tempfile.TemporaryDirectory() as temporary:
            data, _, cfg, _, _, _ = self.transaction(temporary)
            log = data / 'boot.log'
            commands = []
            messages = queue.Queue()
            messages.put(dict(QMP={}))
            class Output:
                def __iter__(self):
                    return self
                def __next__(self):
                    message = messages.get(timeout=2)
                    if message is None:
                        raise StopIteration
                    return (json.dumps(message) + '\n').encode()
                def close(self):
                    pass
            class Input:
                def write(self, content):
                    message = json.loads(content)
                    commands.append(message)
                    messages.put(dict(id=message['id'], **{'return': {}}))
                    if message['execute'] == 'device_add':
                        process.returncode = 0
                        messages.put(None)
                def flush(self):
                    pass
                def close(self):
                    pass
            class Process:
                returncode = None
                stdin, stdout = Input(), Output()
                def poll(self):
                    return self.returncode
            process = Process()
            def sleep(_):
                if log.exists():
                    return
                self.assertEqual([c['execute'] for c in commands], ['qmp_capabilities'])
                log.write_text('helper-root-ready')
            with patch.object(guest_update.subprocess, 'Popen', return_value=process), \
                 patch.object(guest_update.time, 'sleep', side_effect=sleep):
                if os.name == 'nt':
                    from windows import job
                    with patch.object(job, 'Job'):
                        self.assertEqual(guest_update.run_helper(['qemu'], cfg, log, 'helper-root-ready'), 0)
                else:
                    self.assertEqual(guest_update.run_helper(['qemu'], cfg, log, 'helper-root-ready'), 0)
            self.assertEqual([c['execute'] for c in commands], ['qmp_capabilities', 'blockdev-add', 'device_add'])
            self.assertEqual(commands[1]['arguments']['file']['filename'], str(Path(cfg['disk']).resolve()))
