"""Console worker, keeping the QEMU relay's binary streams out of the GUI."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import environment
from windows import backend


if __name__ == '__main__':
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        # Do not make system QEMU/GnuPG load the Python bundle's DLL versions.
        import ctypes
        library = ctypes.WinDLL('kernel32', use_last_error=True)
        library.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        if not library.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding='utf-8')
    if len(sys.argv) > 1 and sys.argv[1] == 'cli':
        sys.argv.pop(1)
        if '--start-gate' in sys.argv:
            sys.argv.remove('--start-gate')
            if sys.stdin.readline().strip() != 'GO':
                raise SystemExit(1)
        environment.main()
    elif len(sys.argv) > 1 and sys.argv[1] in ('relay', 'plan', 'check', 'country'):
        environment.main()
    else:
        backend.main()
