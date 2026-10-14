"""The macOS app and Linux CLI run on a plain Python without third-party packages."""
import ast
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('macos_build', ROOT / 'macos/build.py')
macos_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(macos_build)

# Imported only by the Windows build, or guarded by an ImportError fallback.
OPTIONAL = {'paramiko', 'cryptography', 'pycdlib', 'pefile', 'PySide6', 'impacket', 'certifi'}


@unittest.skipIf(os.name == 'nt', 'macOS and Linux runtime')
class PosixRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name) / 'runtime'
        macos_build.copy_runtime(self.runtime)

    def run_plain(self, code, cwd=None):
        # -I -S: no site-packages at all, like Homebrew's or the system Python.
        return subprocess.run([sys.executable, '-I', '-S', '-c', code], cwd=cwd or self.runtime,
                              capture_output=True, text=True, timeout=120)

    def test_every_module_the_mac_app_imports_is_bundled(self):
        bundled = {path.stem for path in self.runtime.glob('*.py')} | {'windows', 'macos'}
        bundled |= {path.stem for path in (self.runtime / 'macos').glob('*.py')}
        missing = set()
        for path in self.runtime.rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                         else [])
                for name in names:
                    top = name.split('.')[0]
                    if top not in sys.stdlib_module_names and top not in bundled and top not in OPTIONAL:
                        missing.add(f'{path.relative_to(self.runtime)}: {name}')
        self.assertEqual(missing, set())

    def test_start_path_imports_without_third_party_packages(self):
        code = ('import sys; sys.path[:0] = ["", "macos"]\n'
                'import backend, environment, host_access, guest_disk, vm_bundle\n'
                'from windows import shared_sync, ssh_keys, clipboard_framing, qemu_paths\n'
                'print("ok")')
        result = self.run_plain(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout)

    @unittest.skipUnless(shutil.which('ssh-keygen'), 'needs OpenSSH ssh-keygen')
    def test_ssh_key_is_created_without_cryptography(self):
        with tempfile.TemporaryDirectory() as data:
            code = ('import sys; sys.path.insert(0, "")\n'
                    'from host_access import prepare_access\n'
                    f'cfg = {{}}; prepare_access(cfg, {str(data)!r}); print(cfg["ssh_pubkey"])')
            result = self.run_plain(code)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith('ssh-ed25519 '))
            key = Path(data) / 'tools' / 'ssh_host_key'
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)

    def test_shared_folder_uses_the_ssh_client_without_paramiko(self):
        code = ('import sys; sys.path.insert(0, "")\n'
                'from windows import shared_sync\n'
                'shared_sync.SshTransport = lambda port, key: ("ssh", port)\n'
                'print(shared_sync.open_transport(2222, "key"))')
        result = self.run_plain(code)
        if shutil.which('ssh'):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("('ssh', 2222)", result.stdout)


if __name__ == '__main__':
    unittest.main()
