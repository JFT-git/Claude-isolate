"""Native Windows launcher UI built with Qt (PySide6)."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QComboBox, QMessageBox, QGroupBox,
    QFrame, QSizePolicy,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windows import backend
from windows.job import Job


def worker_command(action, data):
    command = ([str(Path(sys.executable).with_name('Claude Isolate Core.exe'))]
               if getattr(sys, 'frozen', False) else [sys.executable, str(ROOT / 'windows/core.py')])
    return command + [action, '--data', str(data)]


class StreamThread(QThread):
    """Reads the worker process stdout and forwards JSON messages."""
    message = Signal(dict)
    log_line = Signal(str)

    def __init__(self, process, data):
        super().__init__()
        self.process = process
        self.data = data

    def run(self):
        with (self.data / 'launcher.log').open('a', encoding='utf-8') as log:
            for line in self.process.stdout:
                log.write(line)
                log.flush()
                self.log_line.emit(line.strip())
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        self.message.emit(value)
                except ValueError:
                    pass


class FinishThread(QThread):
    """Waits for a stop/suspend subprocess to finish."""
    finished_with = Signal(str, bool)  # (message, returncode)

    def __init__(self, process, timeout):
        super().__init__()
        self.process = process
        self.timeout = timeout

    def run(self):
        try:
            output, _ = self.process.communicate(timeout=self.timeout)
            try:
                message = json.loads(output.splitlines()[-1])['message']
            except (ValueError, KeyError, IndexError):
                message = output.strip()
            self.finished_with.emit(message or '', bool(self.process.returncode))
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            self.finished_with.emit('Linux пока не отвечает на остановку. Повторите позже.', True)


class MainWindow(QMainWindow):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.task = None
        self.job = None
        self.stream_thread = None
        self.finish_thread = None
        self.auxiliary = None
        self.vm_started = False
        self.restarting = False
        self.closing = False
        self.stopping = False
        self.last_error = None

        self.setWindowTitle('Claude Isolate')
        self.setMinimumSize(680, 560)
        self.resize(720, 580)
        self._build_ui()
        self._load_config()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(500)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)
        layout.setContentsMargins(28, 28, 28, 28)

        # Header
        title = QLabel('Claude Isolate')
        title.setFont(QFont('Segoe UI', 24, QFont.Weight.Bold))
        layout.addWidget(title)

        subtitle = QLabel('Отдельный Linux для Claude Desktop и Firefox')
        subtitle.setStyleSheet('color: #666;')
        layout.addWidget(subtitle)

        # Status
        self.status_label = QLabel('Готов к работе')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet('font-size: 14px; padding: 4px 0;')
        layout.addWidget(self.status_label)

        self.network_label = QLabel('Включите системный VPN перед запуском среды.')
        self.network_label.setWordWrap(True)
        self.network_label.setStyleSheet('color: #888; padding: 2px 0;')
        layout.addWidget(self.network_label)

        # Progress
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        layout.addWidget(self.progress)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.start_btn = QPushButton('▶  Запустить среду')
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setStyleSheet(self._primary_btn_style())
        self.start_btn.clicked.connect(self.start)
        btn_row.addWidget(self.start_btn)

        self.stop_btn = QPushButton('⏸  Остановить')
        self.stop_btn.setMinimumHeight(40)
        self.stop_btn.clicked.connect(lambda: self.stop('suspend'))
        btn_row.addWidget(self.stop_btn)

        self.restart_btn = QPushButton('🔄  Перезапустить')
        self.restart_btn.setMinimumHeight(40)
        self.restart_btn.clicked.connect(lambda: self.stop('stop', restart=True))
        btn_row.addWidget(self.restart_btn)

        self.poweroff_btn = QPushButton('⏹  Выключить')
        self.poweroff_btn.setMinimumHeight(40)
        self.poweroff_btn.clicked.connect(lambda: self.stop('stop'))
        btn_row.addWidget(self.poweroff_btn)
        layout.addLayout(btn_row)

        # Settings group
        settings = QGroupBox('Настройки')
        settings_layout = QVBoxLayout(settings)
        settings_layout.setSpacing(10)

        res_row = QHBoxLayout()
        res_row.addWidget(QLabel('Ресурсы:'))
        self.resources = QComboBox()
        self.resources.addItems(['Автоматически: по памяти ПК',
                                 'Минимально: 1 ГБ / 1 CPU',
                                 'Экономно: 3 ГБ / 2 CPU',
                                 'Стандартно: 6 ГБ / 4 CPU'])
        self.resources.currentIndexChanged.connect(self.set_resources)
        res_row.addWidget(self.resources, 1)
        settings_layout.addLayout(res_row)

        acc_row = QHBoxLayout()
        acc_row.addWidget(QLabel('Запуск:'))
        self.acceleration = QComboBox()
        self.acceleration.addItems(['Автоматически',
                                    'Совместимый: без гипервизора',
                                    'Аппаратный: WHPX'])
        self.acceleration.currentIndexChanged.connect(self.set_acceleration)
        acc_row.addWidget(self.acceleration, 1)
        settings_layout.addLayout(acc_row)

        self.memory_note = QLabel('')
        self.memory_note.setWordWrap(True)
        self.memory_note.setStyleSheet('color: #b8860b; font-size: 12px;')
        settings_layout.addWidget(self.memory_note)
        layout.addWidget(settings)

        # Shared folder hint
        shared = self.data / 'shared'
        shared_hint = QLabel(f'📁 Общая папка: {shared}\n'
                            'Файлы здесь видны в госте как /home/claude/Shared')
        shared_hint.setWordWrap(True)
        shared_hint.setStyleSheet('color: #666; font-size: 12px; padding: 4px 8px; '
                                  'background: #f5f5f5; border-radius: 4px;')
        layout.addWidget(shared_hint)

        # Open folder button
        open_btn = QPushButton('📂  Открыть папку среды и журналы')
        open_btn.setFlat(True)
        open_btn.clicked.connect(lambda: os.startfile(str(self.data)))
        layout.addWidget(open_btn)

        # Footer
        footer = QLabel(
            'Первый запуск загружает готовую среду Linux. Python, QEMU и GnuPG включены. '
            '«Остановить» сохраняет память Linux, и следующий запуск продолжит работу за секунды. '
            '«Выключить полностью» завершает Linux и удаляет сохранённое состояние.')
        footer.setWordWrap(True)
        footer.setStyleSheet('color: #999; font-size: 11px; padding: 8px 0;')
        layout.addWidget(footer)

        layout.addStretch()

    def _primary_btn_style(self):
        return '''
            QPushButton {
                background-color: #7c3aed;
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #6d28d9;
            }
            QPushButton:pressed {
                background-color: #5b21b6;
            }
            QPushButton:disabled {
                background-color: #ccc;
                color: #888;
            }
        '''

    def _load_config(self):
        _, cfg = backend.config(self.data)
        mode = cfg.get('resources_mode')
        idx = {'auto': 0, 'minimal': 1, 'economy': 2, 'standard': 3}.get(mode)
        self.resources.setCurrentIndex(idx if idx is not None else
                                      1 if cfg['memory_mb'] <= 1024 else 2 if cfg['memory_mb'] <= 3072 else 3)
        self.acceleration.setCurrentIndex(backend.ACCELERATION_MODES.index(cfg.get('acceleration_mode', 'auto')))

    def start(self):
        if self.task:
            return
        self.status_label.setText('Подготовка и запуск среды…')
        self._launch('start')

    def _launch(self, action):
        if self.task:
            return
        self.vm_started = False
        self.last_error = None
        self.memory_note.setText('')
        self.job = Job()
        try:
            self.task = subprocess.Popen(
                worker_command(action, self.data) + ['--start-gate'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.job.assign(self.task)
            self.task.stdin.write('GO\n')
            self.task.stdin.flush()
            self.task.stdin.close()
            self.stream_thread = StreamThread(self.task, self.data)
            self.stream_thread.message.connect(self._on_message, Qt.ConnectionType.QueuedConnection)
            self.stream_thread.start()
            self.progress.setVisible(True)
        except BaseException:
            if self.task:
                self.task.terminate()
                self.task.wait(timeout=10)
            self.task = None
            self.job.close()
            self.job = None
            raise

    def _on_message(self, value):
        if value.get('stop_finished'):
            self.auxiliary = None
        if value.get('stop_error'):
            self.restarting = self.closing = self.stopping = False
        if value.get('running') is True:
            self.vm_started = True
        elif value.get('running') is False:
            self.vm_started = False
        msg = value.get('message')
        if msg:
            if value.get('memory_warning'):
                self.memory_note.setText(msg)
            self.status_label.setText(msg)
            if value.get('error'):
                self.last_error = msg
        if value.get('accelerator') == 'tcg':
            self.network_label.setText(
                'Совместимый режим без гипервизора: загрузка и установка '
                'могут занимать больше времени. VPN должен оставаться включён.')
            if self.acceleration.currentIndex() == 0:
                self.memory_note.setText(
                    'Необязательно: компонент Windows «Платформа низкоуровневой '
                    'оболочки Windows» и перезагрузка ускоряют Linux в несколько раз. '
                    'Без него всё работает.')

    def set_resources(self):
        if self.task:
            return
        try:
            self._launch(('resources-auto', 'minimal', 'economy', 'standard')[self.resources.currentIndex()])
        except (OSError, RuntimeError) as error:
            self.status_label.setText(str(error))

    def set_acceleration(self):
        if self.task:
            return
        try:
            self._launch('accel-' + backend.ACCELERATION_MODES[self.acceleration.currentIndex()])
        except (OSError, RuntimeError) as error:
            self.status_label.setText(str(error))

    def stop(self, action='suspend', restart=False):
        if self.stopping or self.auxiliary:
            return
        if not self.vm_started:
            if action == 'stop' and not self.task:
                self.auxiliary = subprocess.Popen(
                    worker_command('stop', self.data),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW)
                self.finish_thread = FinishThread(self.auxiliary, 30)
                self.finish_thread.finished_with.connect(self._on_finish)
                self.finish_thread.start()
            return
        self.restarting = restart
        self.stopping = True
        self.status_label.setText(
            'Закрываю сеть и сохраняю состояние Linux…' if action == 'suspend'
            else 'Закрываю сеть и корректно завершаю Linux…')
        try:
            self.auxiliary = subprocess.Popen(
                worker_command(action, self.data),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as error:
            self.status_label.setText(str(error))
            self.stopping = self.restarting = self.closing = False
            return
        self.finish_thread = FinishThread(self.auxiliary, 900 if action == 'suspend' else 10)
        self.finish_thread.finished_with.connect(self._on_finish)
        self.finish_thread.start()

    def _on_finish(self, message, returncode):
        if returncode:
            self.status_label.setText(message or 'Linux пока не отвечает на остановку. Повторите позже.')
        elif message and not self.vm_started:
            self.status_label.setText(message)
        self.auxiliary = None
        self.stopping = False
        if self.closing:
            self.close()
        if self.restarting:
            self.restarting = False
            self.start()

    def _poll(self):
        if self.task and self.task.poll() is not None:
            returncode = self.task.returncode
            self.task = None
            self.vm_started = False
            self.stopping = False
            if self.job:
                self.job.close()
                self.job = None
            self.progress.setVisible(False)
            if returncode and not self.restarting:
                self.status_label.setText(
                    self.last_error or
                    'Не удалось завершить действие. Подробности: launcher.log в папке среды.')
            if self.closing:
                QApplication.quit()
                return
        if self.vm_started:
            _, cfg = backend.config(self.data)
            state = backend.status(cfg, True)
            if not self.stopping and not self.closing and not self.restarting:
                self.status_label.setText(state['message'])
            network = state['network']
            self.network_label.setText(
                ('🌐 Сеть открыта' if network.get('allowed') else '🔒 Сеть закрыта') +
                ': ' + str(network.get('reason', '')))
        running = self.vm_started and not self.closing and not self.restarting and not self.stopping
        self.start_btn.setEnabled(not self.task and not self.closing)
        self.stop_btn.setEnabled(running and not self.auxiliary)
        self.restart_btn.setEnabled(running and not self.auxiliary)
        if not self.vm_started and not self.task:
            _, cfg = backend.config(self.data)
            saved = backend.status(cfg)['saved_state']
        else:
            saved = False
        self.poweroff_btn.setEnabled((running or saved) and not self.auxiliary)
        self.resources.setEnabled(not self.task)
        self.acceleration.setEnabled(not self.task)

    def closeEvent(self, event):
        if self.vm_started:
            self.closing = True
            self.stop()
            event.ignore()
        else:
            if self.job:
                self.job.close()
            event.accept()


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
        app = QApplication(sys.argv)
        app.setApplicationName('Claude Isolate')
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
        window = MainWindow(args.data.resolve())
        window.show()
        if args.smoke_test:
            QApplication.processEvents()
            args.smoke_test.write_text(json.dumps(
                {'gui': True, 'title': window.windowTitle(), 'version': backend.VERSION}),
                encoding='utf-8')
            return
        sys.exit(app.exec())
    except Exception as error:
        if args.smoke_test:
            args.smoke_test.write_text(json.dumps({'gui': False, 'error': str(error)}),
                                      encoding='utf-8')
            raise SystemExit(1)
        QMessageBox.critical(None, 'Claude Isolate', str(error))
        raise SystemExit(1)
    finally:
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
