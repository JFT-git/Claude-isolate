"""Lazy Qt launcher facade (PySide6 lives in app_qt_ui)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windows import backend


def Application(data):
    from windows import app_qt_ui
    return app_qt_ui.Application(data)


def run(app, window):
    from windows import app_qt_ui
    app_qt_ui.run(app, window)


def smoke_report(window):
    return json.dumps({'gui': True, 'title': window.windowTitle(), 'version': backend.VERSION})


def __getattr__(name):
    if name == 'QApplication':
        from PySide6.QtWidgets import QApplication
        return QApplication
    raise AttributeError(name)
