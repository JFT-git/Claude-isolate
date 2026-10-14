"""Two-way shared folder sync over SSH on the localhost port (Windows QEMU has no virtfs).

Windows uses paramiko's SFTP, which is bundled. macOS and Linux run with the
standard library only and use the system ssh client instead.

Both copies keep the file's modification time, so a copied file is not seen
as newer on the other side and sent back on the next pass. A file deleted on
one side while the VM runs is deleted on the other side too, unless it was
changed there meanwhile. Deletions are not remembered between sessions: a
re-created guest with an empty folder must never wipe the host's files.
"""
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

REMOTE_ROOT = '/home/claude/Shared'
PARTIAL = '.claude-partial'
INTERVAL = 2.0
# Listing the guest costs it a process (or an SSH session) each time, which is
# expensive under software emulation. While nothing changes, ask less often;
# a change on the host is still sent within INTERVAL.
IDLE_INTERVAL = 16.0
# A failed SSH handshake costs the guest a key exchange; back off up to this.
RETRY_LIMIT = 60.0
WINDOWS_DEVICES = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{n}' for n in range(1, 10)),
                   *(f'LPT{n}' for n in range(1, 10))}


class SharedSync:
    def __init__(self, cfg, key_path, local_root=None):
        from windows.qemu_paths import app_directory
        self.cfg = cfg
        self.key_path = Path(key_path)
        self.local_root = Path(local_root) if local_root else app_directory(cfg) / 'shared'
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        # rel -> (mtime seconds, size) both sides held after the last pass.
        self.synced = {}
        self.reported = set()

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def report(self, message):
        if message not in self.reported:
            self.reported.add(message)
            print(message, file=sys.stderr, flush=True)

    def run(self):
        # The guest may boot long after the sync thread starts (slow hosts, restore
        # from a snapshot), and the SSH session can drop: keep reconnecting until closed.
        while not self.stop.is_set():
            transport = None
            try:
                transport = self.connect()
                if not transport:
                    continue
                transport.ensure_root()
                interval, previous, last_pass = INTERVAL, None, None
                while not self.stop.is_set():
                    local = self.snapshot(self.local_root)
                    now = time.monotonic()
                    if local != previous or last_pass is None or now - last_pass >= interval:
                        changed = self.sync_once(transport, local)
                        previous = self.snapshot(self.local_root)
                        last_pass = now
                        interval = INTERVAL if changed else min(interval * 2, IDLE_INTERVAL)
                    self.stop.wait(INTERVAL)
            except Exception as error:
                self.report('Shared folder sync: ' + type(error).__name__ + ': ' + str(error))
                self.stop.wait(5.0)
            finally:
                if transport:
                    transport.close()

    def connect(self):
        from environment import ssh_port
        port = ssh_port(self.cfg)
        delay = 2.0
        while not self.stop.is_set():
            if not self.key_path.is_file():
                # Without the key every attempt is a wasted handshake.
                self.report('Shared folder sync: no SSH key at ' + str(self.key_path))
                self.stop.wait(RETRY_LIMIT)
                continue
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=2):
                    pass
            except OSError:
                self.stop.wait(2)
                continue
            try:
                return open_transport(port, self.key_path)
            except Exception as error:
                message = 'Shared folder sync connect: ' + type(error).__name__ + ': ' + str(error)
                # sshd is not up yet while a slow guest boots: paramiko reports
                # a missing banner, the ssh client exits with code 255.
                if 'banner' not in message.casefold() and 'code 255' not in message:
                    self.report(message)
                self.stop.wait(delay)
                delay = min(delay * 2, RETRY_LIMIT)
        return None

    @staticmethod
    def snapshot(root):
        files = {}
        for path in root.rglob('*'):
            if path.name.endswith(PARTIAL) or not path.is_file():
                continue
            status = path.stat()
            files[path.relative_to(root).as_posix()] = (int(status.st_mtime), status.st_size)
        return files

    def sync_once(self, transport, local=None):
        """One pass in both directions. True if anything was copied or deleted."""
        local = self.snapshot(self.local_root) if local is None else local
        before = dict(self.synced)
        # A failed listing raises: a partial one would look like deletions.
        remote = transport.listing()
        # A side that is suddenly empty (an unmounted or re-created folder)
        # must not delete the other side's files.
        trust_local = bool(local) or len(self.synced) <= 3
        trust_remote = bool(remote) or len(self.synced) <= 3
        for rel in sorted(set(local) | set(remote) | set(self.synced)):
            if self.stop.is_set():
                return
            if not self.safe_name(rel):
                # Names come from the untrusted guest: never let one point
                # outside the shared folder on the host.
                self.report('Shared folder sync: skipped unsafe name ' + repr(rel))
                continue
            mine, theirs, agreed = local.get(rel), remote.get(rel), self.synced.get(rel)
            try:
                if mine and theirs:
                    if mine == theirs:
                        self.synced[rel] = mine
                    elif newer_side(mine, theirs, agreed) == 'local':
                        self.upload(transport, rel, mine)
                    else:
                        self.download(transport, rel, theirs)
                elif mine:
                    if agreed == mine and trust_remote:
                        self.delete_local(rel)
                    else:
                        self.upload(transport, rel, mine)
                elif theirs:
                    if agreed == theirs and trust_local:
                        transport.remove(rel)
                        self.synced.pop(rel, None)
                    else:
                        self.download(transport, rel, theirs)
                else:
                    self.synced.pop(rel, None)
            except (OSError, ValueError) as error:
                # One file that cannot be copied (open in another program, a
                # name Windows does not allow) must not stop the others.
                self.report(f'Shared folder sync: {rel}: {error}')
        return self.synced != before or local != self.snapshot(self.local_root)

    def safe_name(self, rel):
        parts = rel.split('/')
        if not rel or any(part in ('', '.', '..') or part.endswith(PARTIAL) for part in parts):
            return False
        if any(ord(char) < 32 for char in rel):
            return False
        if os.name == 'nt' and any(char in rel for char in '\\:*?"<>|'):
            return False  # Separators, drive letters, NTFS streams, wildcards.
        if os.name == 'nt' and any(part.split('.')[0].upper() in WINDOWS_DEVICES or part[-1] in '. '
                                   for part in parts):
            return False  # CON, NUL, COM1...: Windows opens a device, not a file.
        root = os.path.realpath(self.local_root)
        target = os.path.realpath(os.path.join(root, *parts))
        return os.path.commonpath([root, target]) == root and target != root

    def upload(self, transport, rel, meta):
        transport.upload(rel, self.local_root / rel, meta[0])
        self.synced[rel] = meta

    def download(self, transport, rel, meta):
        target = self.local_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + PARTIAL)
        try:
            transport.download(rel, partial)
            os.utime(partial, (meta[0], meta[0]))
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        self.synced[rel] = meta

    def delete_local(self, rel):
        (self.local_root / rel).unlink(missing_ok=True)
        self.synced.pop(rel, None)


def newer_side(mine, theirs, agreed):
    """Which copy wins when both exist and differ."""
    if agreed == theirs:
        return 'local'  # Only the host copy changed since the last pass.
    if agreed == mine:
        return 'remote'
    return 'local' if mine[0] >= theirs[0] else 'remote'


def open_transport(port, key):
    try:
        import paramiko
    except ImportError:
        paramiko = None
    if paramiko is not None:
        return SftpTransport(paramiko, port, key)
    if shutil.which('ssh'):
        return SshTransport(port, key)
    raise RuntimeError('общая папка недоступна: не найден клиент ssh')


def remote_path(rel):
    return REMOTE_ROOT + '/' + rel


class SftpTransport:
    """paramiko SFTP; used by the bundled Windows worker."""

    def __init__(self, paramiko, port, key):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(_LocalhostKeys())
        try:
            self.client.connect('127.0.0.1', port=port, username='claude',
                                key_filename=str(key), look_for_keys=False,
                                allow_agent=False, timeout=10, banner_timeout=60,
                                auth_timeout=30)
            self.sftp = self.client.open_sftp()
        except BaseException:
            self.client.close()
            raise

    def ensure_root(self):
        self.ensure_directory(REMOTE_ROOT)

    def ensure_directory(self, folder):
        current = ''
        for part in Path(folder).parts[1:]:
            current += '/' + part
            try:
                self.sftp.stat(current)
            except OSError:
                self.sftp.mkdir(current)

    def listing(self):
        import stat
        files = {}

        def walk(folder, prefix):
            for entry in self.sftp.listdir_attr(folder):
                name = entry.filename
                if name in ('.', '..') or name.endswith(PARTIAL):
                    continue
                if stat.S_ISDIR(entry.st_mode):
                    walk(folder + '/' + name, prefix + name + '/')
                elif stat.S_ISREG(entry.st_mode):
                    files[prefix + name] = (int(entry.st_mtime), entry.st_size)
        walk(REMOTE_ROOT, '')
        return files

    def upload(self, rel, source, mtime):
        target = remote_path(rel)
        partial = target + PARTIAL
        self.ensure_directory(target.rsplit('/', 1)[0])
        self.sftp.put(str(source), partial)
        self.sftp.utime(partial, (mtime, mtime))
        self.sftp.posix_rename(partial, target)

    def download(self, rel, target):
        self.sftp.get(remote_path(rel), str(target))

    def remove(self, rel):
        try:
            self.sftp.remove(remote_path(rel))
        except FileNotFoundError:
            pass

    def close(self):
        try:
            self.sftp.close()
        finally:
            self.client.close()


class SshTransport:
    """The system ssh client, for macOS and Linux hosts without paramiko.

    One multiplexed connection carries every command of a session.
    """

    def __init__(self, port, key):
        # A private directory per session; the socket name is short because a
        # Unix socket path is limited to 104 bytes on macOS.
        self.control = tempfile.mkdtemp(prefix='cis-')
        self.command = ['ssh', '-p', str(port), '-i', str(key), '-o', 'BatchMode=yes',
                        '-o', 'IdentitiesOnly=yes', '-o', 'PreferredAuthentications=publickey',
                        # The forward exists only on 127.0.0.1 for the VM this
                        # process started; its host key changes with the guest.
                        '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                        '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
                        '-o', 'ControlMaster=auto', '-o', 'ControlPath=' + os.path.join(self.control, 'mux'),
                        '-o', 'ControlPersist=60', 'claude@127.0.0.1']
        try:
            self.run('true')
        except BaseException:
            self.close()
            raise

    def run(self, script, stdin=None, stdout=subprocess.PIPE, timeout=60):
        result = subprocess.run(self.command + [script], stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                stdout=stdout, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            detail = result.stderr.decode('utf-8', errors='replace').strip()
            raise OSError(detail or f'ssh exited with code {result.returncode}')
        return result.stdout

    def ensure_root(self):
        self.run('mkdir -p ' + shlex.quote(REMOTE_ROOT))

    def listing(self):
        output = self.run('cd ' + shlex.quote(REMOTE_ROOT) + ' && find . -type f ! -name '
                          + shlex.quote('*' + PARTIAL) + " -printf '%P\\0%T@\\0%s\\0'")
        fields = output.split(b'\0')
        if fields and fields[-1] == b'':
            fields.pop()
        if len(fields) % 3:
            raise ValueError('Unexpected file listing from the guest')
        files = {}
        for index in range(0, len(fields), 3):
            name = fields[index].decode('utf-8', errors='surrogateescape')
            files[name] = (int(float(fields[index + 1])), int(fields[index + 2]))
        return files

    def upload(self, rel, source, mtime):
        target = remote_path(rel)
        partial = target + PARTIAL
        script = ('mkdir -p ' + shlex.quote(target.rsplit('/', 1)[0]) + ' && cat > ' + shlex.quote(partial)
                  + ' && touch -m -d @' + str(int(mtime)) + ' ' + shlex.quote(partial)
                  + ' && mv -f ' + shlex.quote(partial) + ' ' + shlex.quote(target))
        with open(source, 'rb') as data:
            self.run(script, stdin=data, timeout=60 + os.fstat(data.fileno()).st_size / 1_000_000)

    def download(self, rel, target):
        with open(target, 'wb') as output:
            self.run('cat ' + shlex.quote(remote_path(rel)), stdout=output, timeout=None)

    def remove(self, rel):
        self.run('rm -f -- ' + shlex.quote(remote_path(rel)))

    def close(self):
        try:
            subprocess.run(self.command[:-1] + ['-O', 'exit', self.command[-1]],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(self.control, ignore_errors=True)


_LOCAL_HOST = re.compile(r'(127\.0\.0\.1|localhost)|\[(127\.0\.0\.1|localhost)\]:[0-9]{1,5}')


class _LocalhostKeys:
    """Remember the host key only for the slirp forward on 127.0.0.1."""

    def missing_host_key(self, client, hostname, key):
        # paramiko names a non-default port host '[127.0.0.1]:2222'.
        if not _LOCAL_HOST.fullmatch(str(hostname)):
            raise OSError('Refusing SSH host ' + str(hostname))
        client.get_host_keys().add(hostname, key.get_name(), key)
