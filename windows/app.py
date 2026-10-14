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
        # Qt 6 declares per-monitor DPI awareness itself. An earlier
        # SetProcessDPIAware() locks the process to system DPI first, so Qt
        # fails to change it and the window is blurry on a second monitor.
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
        from windows import app_qt
        app, window = app_qt.Application(args.data.resolve())
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
        # The window process has no console: without a dialog and a log file
        # a failure here looks like the program silently not starting.
        log = None
        try:
            import traceback
            folder = args.data.resolve()
            folder.mkdir(parents=True, exist_ok=True)
            log = folder / 'launcher-error.log'
            with log.open('a', encoding='utf-8') as output:
                output.write(traceback.format_exc() + '\n')
        except Exception:
            log = None
        text = ('Claude Isolate не удалось открыть окно: ' + str(error)
                + ('\n\nПодробности: ' + str(log) if log else ''))
        try:
            user = ctypes.WinDLL('user32')
            user.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            user.MessageBoxW(None, text, 'Claude Isolate', 0x10)  # MB_ICONERROR
        except Exception:
            print(text, file=sys.stderr)
        raise SystemExit(1)
    finally:
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
