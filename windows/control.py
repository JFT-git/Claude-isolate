"""Own QEMU's single-client Windows pipe for the entire VM lifetime.

QEMU blocks pipe creation until a client connects, and does not reconnect a
disconnected client. A private, session-bound JSON mailbox lets other launcher
processes request only status and ACPI shutdown from this persistent owner.
"""
import json
from pathlib import Path
import re
import threading
import time
import uuid

from network_guard import read_state, write_state
from session_lock import exclusive

COMMANDS = ('query-status', 'system_powerdown')


def paths(cfg):
    directory = Path(cfg['disk']).parent
    return {key: directory / ('control-' + key + '.json')
            for key in ('ready', 'request', 'response')}


def request(cfg, execute, timeout=10):
    if execute not in COMMANDS:
        raise ValueError('Unsupported Windows control command')
    files = paths(cfg)
    with exclusive(files['request'].with_suffix('.lock')):
        state = read_state(files['ready'])
        if not isinstance(state, dict) or not state.get('ready'):
            raise RuntimeError('Канал управления Linux ещё не готов или уже закрыт')
        identity = uuid.uuid4().hex
        session = state['session']
        write_state(files['request'], dict(session=session, id=identity, execute=execute))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = read_state(files['response'])
                if isinstance(reply, dict) and reply.get('id') == identity and reply.get('session') == session:
                    if 'error' in reply:
                        raise RuntimeError(str(reply['error']))
                    return reply['return']
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        raise RuntimeError('Нет ответа контроллера Linux; подробности в launcher.log')


class Control:
    def __init__(self, cfg, process):
        self.cfg, self.process = cfg, process
        self.files = paths(cfg)
        self.session = uuid.uuid4().hex
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        write_state(self.files['ready'], dict(session=self.session, ready=False))
        self.thread.start()

    def wait_ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        while not self.ready.wait(.05):
            if self.process.poll() is not None:
                return False
            if self.error:
                raise RuntimeError('Не удалось открыть канал управления QEMU: ' + str(self.error))
            if time.monotonic() >= deadline:
                raise RuntimeError('QEMU не подготовил канал управления за 60 секунд')
        return True

    @staticmethod
    def exchange(pipe, execute):
        identity = uuid.uuid4().hex
        pipe.write(json.dumps(dict(execute=execute, id=identity)).encode() + b'\n')
        for _ in range(100):
            line = pipe.readline(65537)
            if not line or len(line) > 65536:
                raise RuntimeError('Канал управления QEMU прерван')
            reply = json.loads(line)
            if not isinstance(reply, dict) or reply.get('id') != identity:
                continue
            if 'error' in reply:
                raise RuntimeError(str(reply['error']))
            return reply['return']
        raise RuntimeError('Нет ответа QEMU на команду управления')

    def serve(self, pipe):
        line = pipe.readline(65537)
        if len(line) > 65536 or 'QMP' not in json.loads(line):
            raise RuntimeError('Неверный ответ канала управления QEMU')
        self.exchange(pipe, 'qmp_capabilities')
        self.exchange(pipe, 'query-status')
        write_state(self.files['ready'], dict(session=self.session, ready=True))
        self.ready.set()
        last = None
        while not self.stop.wait(.05) and self.process.poll() is None:
            try:
                command = read_state(self.files['request'])
            except (OSError, ValueError):
                continue
            if (not isinstance(command, dict) or command.get('session') != self.session
                    or not isinstance(command.get('id'), str)
                    or not re.fullmatch('[0-9a-f]{32}', command['id']) or command['id'] == last):
                continue
            last = command['id']
            reply = dict(session=self.session, id=last)
            if command.get('execute') not in COMMANDS or set(command) != {'session', 'id', 'execute'}:
                reply['error'] = 'Unsupported Windows control command'
            else:
                reply['return'] = self.exchange(pipe, command['execute'])
            write_state(self.files['response'], reply)

    def run(self):
        pipe = None
        try:
            deadline = time.monotonic() + 60
            while not self.stop.is_set() and self.process.poll() is None:
                try:
                    pipe = open('\\\\.\\pipe\\' + self.cfg['qmp_pipe'], 'r+b', buffering=0)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Windows QMP pipe did not start')
                    self.stop.wait(.05)
            if pipe:
                self.serve(pipe)
        except Exception as error:
            self.error = error
            if not self.stop.is_set() and self.process.poll() is None:
                # Control loss must not leave an unmanageable owned VM alive.
                self.process.terminate()
        finally:
            if pipe:
                pipe.close()
            write_state(self.files['ready'], dict(session=self.session, ready=False))

    def close(self):
        # The caller reaps QEMU first, releasing any blocking pipe reads.
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)
