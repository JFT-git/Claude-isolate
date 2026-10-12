"""Native Windows launcher entry point.

PySide6 (Qt) is imported lazily via app_qt so that tests, PyInstaller
analysis, and other tooling that never launch the GUI do not require
the Qt runtime to be installed at import time.
"""
import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windows import backend


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path,
                        default=Path(os.environ.get('LOCALAPPDATA', '.')) / 'Claude Isolate')
    parser.add_argument('--smoke-test', type=Path)
    args = parser.parse_args()
    mutex = None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        ctypes.WinDLL('user32').SetProcessDPIAware()
        # Import Qt only when actually launching the GUI.
        from windows import app_qt
        app, window = app_qt.Application(args.data.resolve())
        name = 'Local\\Claude-Isolate-' + hashlib.sha256(
            os.path.normcase(str(args.data.resolve())).encode()).hexdigest()
        mutex = kernel.CreateMutexW(None, 0, name)
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            user = ctypes.WinDLL('user32')
            user.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
            user.FindWindowW.restype = ctypes.c_void_p
            user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            existing = user.FindWindowW(None, 'Claude Isolate')
            if existing:
                user.ShowWindow(existing, 9)
                user.SetForegroundWindow(existing)
            return
        if args.smoke_test:
            app_qt.QApplication.processEvents()
            args.smoke_test.write_text(app_qt.smoke_report(window), encoding='utf-8')
            return
        app_qt.run(app, window)
    except Exception as error:
        if args.smoke_test:
            args.smoke_test.write_text(
                '{"gui": false, "error": "' + str(error).replace('"', "'") + '"}',
                encoding='utf-8')
            raise SystemExit(1)
        try:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(None, 'Claude Isolate', str(error))
        except Exception:
            print(str(error), file=sys.stderr)
        raise SystemExit(1)
    finally:
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
