"""Two-way shared folder sync over SFTP on localhost:2222 (Windows QEMU has no virtfs)."""
import os
import socket
import sys
import threading
import time
from pathlib import Path

REMOTE_ROOT = '/home/claude/Shared'


class SharedSync:
    def __init__(self, cfg, key_path):
        self.cfg = cfg
        self.key_path = Path(key_path)
        self.local_root = Path(cfg['disk']).parent / 'shared'
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def run(self):
        try:
            client = self.connect()
            if not client:
                return
            sftp = client.open_sftp()
            try:
                self.ensure_remote(sftp)
                while not self.stop.is_set():
                    self.sync_once(sftp)
                    self.stop.wait(2.0)
            finally:
                sftp.close()
                client.close()
        except Exception as error:
            print('Windows shared folder sync: ' + str(error), file=sys.stderr, flush=True)

    def connect(self):
        import paramiko
        deadline = time.monotonic() + 600
        while not self.stop.is_set() and time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', 2222), timeout=2):
                    pass
            except OSError:
                self.stop.wait(2)
                continue
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(_LocalhostKeys())
            try:
                client.connect('127.0.0.1', port=2222, username='claude',
                               key_filename=str(self.key_path), look_for_keys=False,
                               allow_agent=False, timeout=10, banner_timeout=60,
                               auth_timeout=30)
                return client
            except (paramiko.SSHException, OSError):
                self.stop.wait(2)
        return None

    def ensure_remote(self, sftp):
        parts = Path(REMOTE_ROOT).parts
        current = ''
        for part in parts[1:]:
            current += '/' + part
            try:
                sftp.stat(current)
            except OSError:
                sftp.mkdir(current)

    def sync_once(self, sftp):
        local = self.snapshot(self.local_root)
        remote = self.snapshot_remote(sftp, REMOTE_ROOT)
        for rel in sorted(set(local) | set(remote)):
            local_meta = local.get(rel)
            remote_meta = remote.get(rel)
            if local_meta and (not remote_meta or local_meta > remote_meta):
                self.upload(sftp, rel)
            elif remote_meta and (not local_meta or remote_meta > local_meta):
                self.download(sftp, rel)

    @staticmethod
    def snapshot(root):
        files = {}
        for path in root.rglob('*'):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                files[rel] = (path.stat().st_mtime_ns, path.stat().st_size)
        return files

    def snapshot_remote(self, sftp, remote_root):
        files = {}
        def walk(folder):
            for entry in sftp.listdir_attr(folder):
                name = entry.filename
                if name in ('.', '..'):
                    continue
                path = folder.rstrip('/') + '/' + name
                if stat_is_dir(entry):
                    walk(path)
                else:
                    rel = path[len(remote_root.rstrip('/')) + 1:]
                    files[rel] = (entry.st_mtime * 1_000_000_000, entry.st_size)
        try:
            walk(remote_root.rstrip('/'))
        except OSError:
            pass
        return files

    def upload(self, sftp, rel):
        source = self.local_root / rel
        target = REMOTE_ROOT.rstrip('/') + '/' + rel
        target_dir = os.path.dirname(target)
        self.ensure_remote_path(sftp, target_dir)
        sftp.put(str(source), target)

    def download(self, sftp, rel):
        target = self.local_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        sftp.get(REMOTE_ROOT.rstrip('/') + '/' + rel, str(target))

    def ensure_remote_path(self, sftp, folder):
        if not folder or folder == '/':
            return
        parts = Path(folder).parts
        current = ''
        for part in parts[1:]:
            current += '/' + part
            try:
                sftp.stat(current)
            except OSError:
                sftp.mkdir(current)


def stat_is_dir(entry):
    import stat
    return stat.S_ISDIR(entry.st_mode)


class _LocalhostKeys:
    """Remember the host key only for the slirp forward on 127.0.0.1."""

    def missing_host_key(self, client, hostname, key):
        if hostname not in ('127.0.0.1', 'localhost'):
            raise OSError('Refusing SSH host ' + str(hostname))
        client.get_host_keys().add(hostname, key.get_name(), key)
