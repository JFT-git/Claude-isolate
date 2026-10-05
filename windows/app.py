"""Native Windows launcher UI with no external Python requirement."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windows import backend
from windows.job import Job


def worker_command(action, data):
    command = ([str(Path(sys.executable).with_name('Claude Isolate Core.exe'))]
               if getattr(sys, 'frozen', False) else [sys.executable, str(ROOT / 'windows/core.py')])
    return command + [action, '--data', str(data)]


class Application:
    def __init__(self, root, data):
        self.root, self.data = root, data
        self.task = None
        self.job = None
        self.vm_started = False
        self.restarting = False
        self.closing = False
        self.auxiliary = None
        self.last_error = None
        self.messages = queue.Queue()
        root.title('Claude Isolate')
        root.geometry('700x420')
        root.minsize(620, 400)
        root.option_add('*Font', '{Segoe UI} 10')
        style = ttk.Style(root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Claude Isolate', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Отдельный Linux для Claude Desktop и Firefox').pack(anchor='w', pady=(2, 20))
        self.message = tk.StringVar(value='Готов к работе')
        ttk.Label(frame, textvariable=self.message, wraplength=640).pack(anchor='w')
        self.network = tk.StringVar(value='Включите системный VPN перед запуском среды.')
        ttk.Label(frame, textvariable=self.network, wraplength=640).pack(anchor='w', pady=(8, 14))
        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.pack(fill='x', pady=(0, 14))
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x')
        self.start_button = ttk.Button(buttons, text='Запустить среду', command=self.start)
        self.start_button.pack(side='left', padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text='Остановить', command=self.stop)
        self.stop_button.pack(side='left', padx=(0, 8))
        self.restart_button = ttk.Button(buttons, text='Перезапустить среду', command=lambda: self.stop(restart=True))
        self.restart_button.pack(side='left')
        resources = ttk.Frame(frame)
        resources.pack(fill='x', pady=(18, 8))
        ttk.Label(resources, text='Ресурсы:').pack(side='left')
        self.resources = ttk.Combobox(resources, state='readonly', width=30,
                                      values=['Экономно: 3 ГБ / 2 CPU', 'Стандартно: 6 ГБ / 4 CPU'])
        self.resources.pack(side='left', padx=8)
        _, cfg = backend.config(data)
        self.resources.current(0 if cfg['memory_mb'] <= 3072 else 1)
        self.resources.bind('<<ComboboxSelected>>', self.set_resources)
        ttk.Button(frame, text='Открыть папку среды и журналы',
                   command=lambda: os.startfile(str(data))).pack(anchor='w', pady=(8, 0))
        ttk.Label(frame, text='Первый запуск загружает Ubuntu и устанавливает приложения. '
                  'Для QEMU и GnuPG может появиться системный запрос Windows.',
                  wraplength=640).pack(anchor='w', pady=(14, 0))
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll()

    def stream(self, process):
        with (self.data / 'launcher.log').open('a', encoding='utf-8') as log:
            for line in process.stdout:
                log.write(line)
                log.flush()
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        self.messages.put(value)
                except ValueError:
                    pass

    def launch(self, action):
        if self.task:
            return
        self.vm_started = False
        self.last_error = None
        self.job = Job()
        try:
            self.task = subprocess.Popen(worker_command(action, self.data) + ['--start-gate'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
            self.job.assign(self.task)
            self.task.stdin.write('GO\n')
            self.task.stdin.flush()
            self.task.stdin.close()
            threading.Thread(target=self.stream, args=(self.task,), daemon=True).start()
            self.progress.start()
        except BaseException:
            if self.task:
                self.task.terminate()
                self.task.wait(timeout=10)
            self.task = None
            self.job.close()
            self.job = None
            raise

    def start(self):
        self.message.set('Подготовка и запуск среды…')
        try:
            self.launch('start')
        except (OSError, RuntimeError) as error:
            self.message.set(str(error))

    def set_resources(self, event=None):
        try:
            self.launch('economy' if self.resources.current() == 0 else 'standard')
        except (OSError, RuntimeError) as error:
            self.message.set(str(error))

    def stop(self, restart=False):
        if not self.vm_started or self.auxiliary:
            return
        self.restarting = restart
        self.message.set('Закрываю сеть и корректно завершаю Linux…')
        self.auxiliary = subprocess.Popen(worker_command('stop', self.data),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
        def finish():
            process = self.auxiliary
            try:
                output, _ = process.communicate(timeout=10)
                if process.returncode:
                    self.messages.put({'error': True, 'stop_error': True,
                                       'message': output.strip() or 'Linux пока не отвечает на остановку. Повторите позже.'})
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.messages.put({'error': True, 'stop_error': True,
                                   'message': 'Linux пока не отвечает на остановку. Повторите позже.'})
            finally:
                self.messages.put({'stop_finished': True})
        threading.Thread(target=finish, daemon=True).start()

    def close(self):
        if self.vm_started:
            self.closing = True
            self.stop()
        else:
            # The owned tree dies if the controller closes during setup.
            if self.job:
                self.job.close()
            self.root.destroy()

    def poll(self):
        while not self.messages.empty():
            value = self.messages.get_nowait()
            if value.get('stop_finished'):
                self.auxiliary = None
            if value.get('stop_error'):
                self.restarting = self.closing = False
            if value.get('running'):
                self.vm_started = True
            if value.get('message'):
                self.message.set(value['message'])
                if value.get('error'):
                    self.last_error = value['message']
            if value.get('accelerator') == 'tcg':
                self.network.set('Доступна программная эмуляция. Для быстрого запуска включите '
                                 '«Платформа гипервизора Windows» и перезагрузите компьютер.')
        if self.task and self.task.poll() is not None:
            returncode = self.task.returncode
            self.task = None
            self.vm_started = False
            self.job.close()
            self.job = None
            self.progress.stop()
            if returncode and not self.restarting:
                self.message.set(self.last_error or 'Не удалось завершить действие. Подробности: launcher.log в папке среды.')
            if self.closing:
                self.root.destroy()
                return
            if self.restarting:
                self.restarting = False
                self.start()
        if self.vm_started:
            _, cfg = backend.config(self.data)
            state = backend.status(cfg, True)
            if not self.auxiliary and not self.closing and not self.restarting:
                self.message.set(state['message'])
            network = state['network']
            self.network.set(('Сеть открыта' if network.get('allowed') else 'Сеть закрыта') +
                             ': ' + str(network.get('reason', '')))
        running = self.vm_started and not self.closing and not self.restarting
        self.start_button.configure(state='disabled' if self.task or self.closing else 'normal')
        for button in (self.stop_button, self.restart_button):
            button.configure(state='normal' if running and not self.auxiliary else 'disabled')
        self.resources.configure(state='disabled' if self.task else 'readonly')
        self.root.after(500, self.poll)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '.')) / 'Claude Isolate')
    parser.add_argument('--smoke-test', type=Path)
    args = parser.parse_args()
    try:
        # Let Tk use Windows' display scaling instead of bitmap stretching.
        import ctypes
        ctypes.WinDLL('user32').SetProcessDPIAware()
        root = tk.Tk()
        app = Application(root, args.data.resolve())
        if args.smoke_test:
            root.update_idletasks()
            args.smoke_test.write_text(json.dumps({'gui': True, 'title': root.title(),
                                                   'version': backend.VERSION}), encoding='utf-8')
            root.destroy()
        else:
            root.mainloop()
    except Exception as error:
        if args.smoke_test:
            args.smoke_test.write_text(json.dumps({'gui': False, 'error': str(error)}), encoding='utf-8')
            raise SystemExit(1)
        messagebox.showerror('Claude Isolate', str(error))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
