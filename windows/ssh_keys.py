"""Host SSH client keys for localhost guest access (no external ssh-keygen)."""
import csv
import io
import os
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

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
