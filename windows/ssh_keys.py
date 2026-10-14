"""Host SSH client keys for localhost guest access.

The Windows build bundles cryptography and needs no ssh-keygen. macOS and
Linux run with the standard library only and use the system ssh-keygen.
"""
import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

COMMENT = 'claude-isolate'


def restrict_private_key(path):
    """Windows OpenSSH refuses a key that other users can read: keep only the owner."""
    if sys.platform != 'win32':
        return
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    who = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True,
                         timeout=30, creationflags=flags, check=True)
    sid = next(csv.reader(io.StringIO(who.stdout.strip())))[1]
    subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', '*' + sid + ':F'],
                   capture_output=True, timeout=30, creationflags=flags, check=True)


def ensure_client_keypair(path):
    """Create an Ed25519 OpenSSH keypair at path if missing; return the public key line."""
    path = Path(path)
    public = path.with_suffix('.pub')
    if path.is_file() and public.is_file():
        restrict_private_key(path)
        return public.read_text(encoding='utf-8').strip() + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return generate_with_ssh_keygen(path)
    private = ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption()))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    restrict_private_key(path)
    public_line = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH).decode('ascii') + ' ' + COMMENT
    public.write_text(public_line + '\n', encoding='utf-8')
    return public_line + '\n'


def generate_with_ssh_keygen(path):
    tool = shutil.which('ssh-keygen')
    if not tool:
        raise RuntimeError('Не найден ssh-keygen: установите OpenSSH-клиент (на Linux пакет openssh-client).')
    public = path.with_suffix('.pub')
    # Create both files next to the target, then publish them together.
    with tempfile.TemporaryDirectory(prefix='.key-', dir=path.parent) as folder:
        staged = Path(folder) / 'key'
        subprocess.run([tool, '-q', '-t', 'ed25519', '-N', '', '-C', COMMENT, '-f', str(staged)],
                       check=True, capture_output=True, timeout=60)
        os.chmod(staged, 0o600)
        os.replace(staged.with_suffix('.pub'), public)
        os.replace(staged, path)
    return public.read_text(encoding='utf-8').strip() + '\n'
