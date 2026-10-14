"""Shared folder: no endless re-copying, deletions follow, guest names stay inside."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows import shared_sync
from windows.shared_sync import SharedSync


class FolderTransport:
    """The guest's /home/claude/Shared as a local folder; counts transfers."""

    def __init__(self, root):
        self.root = Path(root)
        self.transfers = []

    def ensure_root(self):
        self.root.mkdir(parents=True, exist_ok=True)

    def listing(self):
        return SharedSync.snapshot(self.root)

    def upload(self, rel, source, mtime):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.utime(target, (mtime, mtime))
        self.transfers.append(('up', rel))

    def download(self, rel, target):
        shutil.copyfile(self.root / rel, target)
        self.transfers.append(('down', rel))

    def remove(self, rel):
        (self.root / rel).unlink(missing_ok=True)
        self.transfers.append(('rm', rel))


class SyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.host = base / 'host'
        self.guest = FolderTransport(base / 'guest')
        self.guest.ensure_root()
        self.sync = SharedSync({'disk': str(base / 'desktop.qcow2')}, base / 'key', local_root=self.host)

    def write(self, root, rel, text, mtime):
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        os.utime(path, (mtime, mtime))
        return path

    def test_copied_files_are_not_sent_back_and_forth(self):
        self.write(self.host, 'docs/a.txt', 'host', 1_700_000_000)
        self.write(self.guest.root, 'b.txt', 'guest', 1_700_000_100)
        self.sync.sync_once(self.guest)
        self.assertEqual(sorted(self.guest.transfers), [('down', 'b.txt'), ('up', 'docs/a.txt')])
        self.guest.transfers.clear()
        for _ in range(3):
            self.sync.sync_once(self.guest)
        self.assertEqual(self.guest.transfers, [])
        self.assertEqual((self.guest.root / 'docs/a.txt').read_text(encoding='utf-8'), 'host')
        self.assertEqual((self.host / 'b.txt').read_text(encoding='utf-8'), 'guest')
        self.assertEqual(int((self.host / 'b.txt').stat().st_mtime), 1_700_000_100)

    def test_a_change_on_either_side_wins_over_the_older_copy(self):
        self.write(self.host, 'a.txt', 'v1', 1_700_000_000)
        self.sync.sync_once(self.guest)
        self.write(self.guest.root, 'a.txt', 'v2 from guest', 1_700_000_050)
        self.sync.sync_once(self.guest)
        self.assertEqual((self.host / 'a.txt').read_text(encoding='utf-8'), 'v2 from guest')
        self.write(self.host, 'a.txt', 'v3 host', 1_700_000_060)
        self.sync.sync_once(self.guest)
        self.assertEqual((self.guest.root / 'a.txt').read_text(encoding='utf-8'), 'v3 host')

    def test_deletions_follow_during_a_session(self):
        for name in ('a.txt', 'b.txt'):
            self.write(self.host, name, name, 1_700_000_000)
        self.sync.sync_once(self.guest)
        (self.host / 'a.txt').unlink()
        (self.guest.root / 'b.txt').unlink()
        self.sync.sync_once(self.guest)
        self.assertFalse((self.guest.root / 'a.txt').exists())
        self.assertFalse((self.host / 'b.txt').exists())
        self.assertEqual(self.sync.synced, {})

    def test_a_file_changed_on_the_other_side_is_not_deleted(self):
        self.write(self.host, 'a.txt', 'v1', 1_700_000_000)
        self.sync.sync_once(self.guest)
        (self.host / 'a.txt').unlink()
        self.write(self.guest.root, 'a.txt', 'edited in guest', 1_700_000_500)
        self.sync.sync_once(self.guest)
        self.assertEqual((self.host / 'a.txt').read_text(encoding='utf-8'), 'edited in guest')

    def test_a_suddenly_empty_guest_folder_does_not_wipe_the_host(self):
        for index in range(6):
            self.write(self.host, f'f{index}.txt', 'keep', 1_700_000_000)
        self.sync.sync_once(self.guest)
        shutil.rmtree(self.guest.root)
        self.guest.ensure_root()
        self.sync.sync_once(self.guest)
        self.assertEqual(len(list(self.host.glob('*.txt'))), 6)
        self.assertEqual(len(list(self.guest.root.glob('*.txt'))), 6)

    def test_new_session_never_deletes(self):
        self.write(self.host, 'only-host.txt', 'x', 1_700_000_000)
        self.write(self.guest.root, 'only-guest.txt', 'y', 1_700_000_000)
        self.sync.sync_once(self.guest)
        self.assertTrue((self.guest.root / 'only-host.txt').exists())
        self.assertTrue((self.host / 'only-guest.txt').exists())

    def test_guest_names_cannot_leave_the_shared_folder(self):
        class Hostile(FolderTransport):
            def listing(self):
                return {'../escape.txt': (1, 1), 'a/../../escape2.txt': (1, 1), '/etc/x': (1, 1)}
        hostile = Hostile(self.guest.root)
        with patch('builtins.print'):
            self.sync.sync_once(hostile)
        self.assertEqual(hostile.transfers, [])
        self.assertFalse((self.host.parent / 'escape.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows file names')
    def test_windows_device_and_stream_names_are_refused(self):
        for name in ('nul', 'CON.txt', 'a:stream', 'C:/x', 'dir\\..\\x', 'trailing.'):
            self.assertFalse(self.sync.safe_name(name), name)

    def test_one_failing_file_does_not_stop_the_others(self):
        class Flaky(FolderTransport):
            def upload(self, rel, source, mtime):
                if rel == 'bad.txt':
                    raise PermissionError('locked')
                super().upload(rel, source, mtime)
        flaky = Flaky(self.guest.root)
        self.write(self.host, 'bad.txt', 'x', 1_700_000_000)
        self.write(self.host, 'good.txt', 'y', 1_700_000_000)
        with patch('builtins.print'):
            self.sync.sync_once(flaky)
        self.assertTrue((self.guest.root / 'good.txt').exists())

    def test_downloads_never_leave_a_partial_file(self):
        class Broken(FolderTransport):
            def download(self, rel, target):
                Path(target).write_text('half', encoding='utf-8')
                raise OSError('connection lost')
        broken = Broken(self.guest.root)
        self.write(self.guest.root, 'big.bin', 'complete', 1_700_000_000)
        with patch('builtins.print'):
            self.sync.sync_once(broken)
        self.assertEqual(list(self.host.iterdir()), [])


def gnu_tools():
    try:
        find = subprocess.run(['find', '.', '-maxdepth', '0', '-printf', ''], capture_output=True)
        touch = subprocess.run(['touch', '--version'], capture_output=True)
        return find.returncode == 0 and touch.returncode == 0
    except OSError:
        return False


@unittest.skipUnless(os.name != 'nt' and gnu_tools(), 'the guest runs GNU find and touch')
class SshTransportTests(unittest.TestCase):
    """Runs SshTransport's remote commands through a fake ssh on this host."""

    def test_round_trip_keeps_names_times_and_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            remote = base / 'guest Shared'
            bin_dir = base / 'bin'
            bin_dir.mkdir()
            fake = bin_dir / 'ssh'
            # The last argument is the remote shell command; options are ignored.
            fake.write_text('#!/bin/sh\nfor last; do :; done\ncase "$*" in *" -O exit "*) exit 0;; esac\n'
                            'exec /bin/sh -c "$last"\n', encoding='utf-8')
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            with patch.dict(os.environ, {'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}), \
                 patch.object(shared_sync, 'REMOTE_ROOT', str(remote)):
                transport = shared_sync.SshTransport(2222, base / 'key')
                try:
                    transport.ensure_root()
                    source = base / "it's a file.txt"
                    source.write_text('привет', encoding='utf-8')
                    transport.upload("dir with space/it's a file.txt", source, 1_700_000_000)
                    listing = transport.listing()
                    self.assertEqual(listing, {"dir with space/it's a file.txt": (1_700_000_000, len('привет'.encode()))})
                    target = base / 'copy'
                    transport.download("dir with space/it's a file.txt", target)
                    self.assertEqual(target.read_text(encoding='utf-8'), 'привет')
                    transport.remove("dir with space/it's a file.txt")
                    self.assertEqual(transport.listing(), {})
                finally:
                    transport.close()


if __name__ == '__main__':
    unittest.main()
