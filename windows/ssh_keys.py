"""Host SSH client keys for localhost guest access (no external ssh-keygen)."""
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

COMMENT = 'claude-isolate'


def ensure_client_keypair(path):
    """Create an Ed25519 OpenSSH keypair at path if missing; return the public key line."""
    path = Path(path)
    public = path.with_suffix('.pub')
    if path.is_file() and public.is_file():
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
    public_line = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH).decode('ascii') + ' ' + COMMENT
    public.write_text(public_line + '\n', encoding='utf-8')
    return public_line + '\n'
