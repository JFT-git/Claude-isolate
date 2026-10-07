"""Download a release-pinned, preinstalled guest; keep interrupted transfers retryable."""
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
REPOSITORY = 'https://github.com/JFT-git/Claude-isolate/releases/download/'


def metadata(arch):
    if arch not in ('x86_64', 'aarch64'):
        raise ValueError('Unsupported image architecture')
    path = ROOT / ('guest-image-' + arch + '.json')
    if not path.is_file():
        raise RuntimeError('This build has no prepared guest manifest. Use a complete GitHub release.')
    data = json.loads(path.read_text(encoding='utf-8'))
    if (data.get('arch') != arch or data.get('schema') != 1
            or not re.fullmatch(r'v\d+\.\d+\.\d+', data.get('release', ''))
            or not re.fullmatch(r'[0-9a-f]{64}', data.get('sha256', ''))
            or not isinstance(data.get('size'), int) or not 0 < data['size'] < 2 * 1024**3
            or data.get('filename') != 'Claude-isolate-guest-' + arch + '.qcow2'):
        raise ValueError('Invalid prepared guest manifest')
    return data


def matches(path, data):
    if not path.is_file() or path.stat().st_size != data['size']:
        return False
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest() == data['sha256']


def download(directory, arch, progress=None):
    data = metadata(arch)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / (data['sha256'] + '.qcow2')
    if matches(image, data):
        return image, data['sha256']
    partial = image.with_suffix('.partial')
    url = REPOSITORY + data['release'] + '/' + data['filename']
    for attempt in range(4):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset >= data['size']:
            if matches(partial, data):
                partial.replace(image)
                return image, data['sha256']
            partial.unlink()
            offset = 0
        request = urllib.request.Request(url, headers={'Range': f'bytes={offset}-'} if offset else {})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 # Fixed HTTPS repository, pinned digest, HTTPS redirect checked.
                if not response.url.startswith('https://'):
                    raise ValueError('Insecure image redirect')
                if response.status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match or int(match[1]) != offset or int(match[3]) != data['size']:
                        raise ValueError('Invalid resumed image range')
                elif response.status == 200:
                    offset = 0  # Server ignored Range: replace, never append.
                else:
                    raise ValueError('Unexpected image response')
                with partial.open('ab' if offset else 'wb') as output:
                    received = offset
                    last_report = 0.0
                    while chunk := response.read(1024 * 1024):
                        received += len(chunk)
                        if received > data['size']:
                            raise ValueError('Image exceeds pinned size')
                        output.write(chunk)
                        if progress and time.monotonic() - last_report >= 1:
                            progress(received, data['size'])
                            last_report = time.monotonic()
            if not matches(partial, data):
                if partial.stat().st_size == data['size']:
                    partial.unlink()  # Corrupt complete download must start fresh.
                    raise ValueError('Prepared guest checksum mismatch')
                raise OSError('Incomplete image download')
            partial.replace(image)
            return image, data['sha256']
        except (OSError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError('Prepared guest download failed')
