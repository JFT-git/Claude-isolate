"""Fetch an Ubuntu image only after checking its signed manifest; repair stale caches."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import urllib.request

FINGERPRINT = 'D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81'
BASE = 'https://cloud-images.ubuntu.com/noble/current/'


def fetch(url, target):
    if not url.startswith('https://'):
        raise ValueError('HTTPS required')
    with urllib.request.urlopen(url, timeout=60) as response, target.open('wb') as output:  # nosec B310 # Fixed vendor HTTPS URLs; redirect scheme checked.
        if not response.url.startswith('https://'):
            raise ValueError('Insecure redirect')
        shutil.copyfileobj(response, output, 1024 * 1024)


def matches(path, digest):
    if not path.is_file():
        return False
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest() == digest


def download(directory, arch, gpg):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    filename = 'noble-server-cloudimg-' + ('arm64' if arch == 'aarch64' else 'amd64') + '.img'
    sums, signature = directory / 'SHA256SUMS', directory / 'SHA256SUMS.gpg'
    key = directory / 'ubuntu-signing-key.asc'
    fetch(BASE + sums.name, sums)
    fetch(BASE + signature.name, signature)
    fetch('https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x' + FINGERPRINT, key)
    home = directory / 'verification'
    home.mkdir(mode=0o700, exist_ok=True)
    common = [gpg, '--homedir', str(home), '--batch', '--no-autostart']
    try:
        subprocess.run(common + ['--import', str(key)], check=True, capture_output=True)
        verified = subprocess.run(common + ['--status-fd', '1', '--verify', str(signature), str(sums)],
                                  check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
    except subprocess.CalledProcessError as error:
        detail = error.stderr or ''
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        raise RuntimeError('Ubuntu signature verification failed: ' + detail.strip()[:2000]) from error
    if not any(line.startswith('[GNUPG:] VALIDSIG ' + FINGERPRINT + ' ') for line in verified.stdout.splitlines()):
        raise RuntimeError('Ubuntu signing key mismatch')
    entries = [parts[0] for line in sums.read_text().splitlines()
               if len(parts := line.split()) == 2 and parts[1].lstrip('*') == filename]
    if len(entries) != 1 or len(entries[0]) != 64 or any(c not in '0123456789abcdef' for c in entries[0]):
        raise RuntimeError('Missing or ambiguous Ubuntu image checksum')
    digest = entries[0]
    image = directory / filename
    if not matches(image, digest):
        partial = image.with_suffix('.partial')
        try:
            fetch(BASE + filename, partial)
            if not matches(partial, digest):
                raise RuntimeError('Ubuntu image checksum mismatch; retry preparation for a refreshed manifest')
            partial.replace(image)
        finally:
            partial.unlink(missing_ok=True)
    return image, digest
