"""Give the Homebrew VM window a macOS app identity and normal Dock controls."""
import json
from pathlib import Path
import plistlib
import shutil
import subprocess


def provision(data, executable):
    source = Path(executable).resolve()
    app = Path(data) / 'Linux Desktop.app'
    contents = app / 'Contents'
    target = contents / 'MacOS' / source.name
    stamp = Path(data) / '.qemu-bundle-source.json'
    identity = {'source': str(source), 'size': source.stat().st_size,
                'mtime': source.stat().st_mtime_ns}
    try:
        if target.is_file() and json.loads(stamp.read_text()) == identity:
            return str(target)
    except (OSError, ValueError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.chmod(0o755)
    shutil.copyfile(source, target)
    target.chmod(0o755)
    (contents / 'Info.plist').write_bytes(plistlib.dumps({
        'CFBundleExecutable': target.name,
        'CFBundleIdentifier': 'local.claude.environment.linux',
        'CFBundleName': 'Linux Desktop', 'CFBundleDisplayName': 'Linux Desktop',
        'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
        'NSHighResolutionCapable': True,
    }))
    (contents / 'Resources').mkdir(exist_ok=True)
    entitlements = contents / 'Resources/hypervisor.plist'
    entitlements.write_bytes(plistlib.dumps({'com.apple.security.hypervisor': True}))
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--entitlements',
                    str(entitlements), str(app)], check=True, capture_output=True)
    stamp.write_text(json.dumps(identity))
    return str(target)
