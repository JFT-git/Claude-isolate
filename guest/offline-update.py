"""Apply trusted product files to an offline Ubuntu root; never edit /home."""
import json
import os
from pathlib import Path
import sys
import tempfile


def destination(root, name):
    relative = Path(name.lstrip('/'))
    if not name.startswith(('/etc/', '/usr/local/', '/var/lib/claude-isolate/')) or '..' in relative.parts:
        raise ValueError('Update destination is outside product directories')
    target = root / relative
    # Refuse links even within the guest: updating a redirected system path
    # could overwrite profile data. Linux's /usr merge is outside these paths.
    for component in (target, *target.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError('Update destination contains a symbolic link: ' + name)
    return target


def apply(root, payload):
    root = root.resolve(strict=True)
    release = root / 'etc/os-release'
    # /etc/os-release is a standard symlink on Ubuntu. Read its canonical
    # vendor file instead, confined to the mounted guest root.
    if release.is_symlink():
        if os.readlink(release) not in ('../usr/lib/os-release', '/usr/lib/os-release'):
            raise ValueError('Unexpected OS release link')
        release = root / 'usr/lib/os-release'
        if release.is_symlink() or release.parent.resolve() != root / 'usr/lib':
            raise ValueError('Redirected OS release file')
    else:
        release = destination(root, '/etc/os-release')
    if not release.exists():
        raise ValueError('Missing Ubuntu release')
    if 'ID=ubuntu' not in release.read_text().splitlines():
        raise ValueError('Only existing Ubuntu environments can be updated')
    passwd = destination(root, '/etc/passwd').read_text().splitlines()
    if not any(line.startswith('claude:') for line in passwd):
        # A previously prepared cloud image may never have booted. Its new
        # seed will create the desktop user on first boot; no user data exists.
        if (root / 'var/lib/cloud/instance').exists() or (root / 'etc/cloud/cloud-init.disabled').exists() or not (root / 'etc/cloud/cloud.cfg').is_file():
            raise ValueError('The target disk is not an initialized Claude Isolate environment or pristine cloud image')
    links = []
    for unit, target in payload['enable']:
        if '/' in unit or not unit.endswith('.service') or target not in ('multi-user.target', 'graphical.target'):
            raise ValueError('Invalid service enable target')
        folder = '/etc/systemd/system/' + target + '.wants/'
        destination(root, folder + '.check')
        link = root / (folder.lstrip('/') + unit)
        expected = '/etc/systemd/system/' + unit
        if link.is_symlink():
            if os.readlink(link) not in (expected, '../' + unit):
                raise ValueError('Unexpected enabled unit destination: ' + unit)
        elif link.exists():
            raise ValueError('Unexpected enabled unit file: ' + unit)
        links.append((link, expected))
    files = list(payload['files'])
    # Preflight every target before making the first change.
    targets = [(destination(root, item['path']), item) for item in files]
    env = destination(root, '/etc/environment')
    original = env.read_text() if env.exists() else ''
    proxy_names = {'http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'}
    lines = [line for line in original.splitlines() if line.split('=', 1)[0].strip() not in proxy_names]
    lines += [key + '="http://10.0.2.100:7890"' for key in sorted(proxy_names)]
    targets.append((env, dict(content='\n'.join(lines) + '\n', permissions='0644')))
    for path, item in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.claude-update-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
                output.write(item['content'])
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, int(item['permissions'], 8))
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    # Enable independent services without executing any code from the old OS.
    for link, expected in links:
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(expected)
    if hasattr(os, 'sync'):
        os.sync()


if __name__ == '__main__':
    apply(Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text()))
