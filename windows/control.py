"""Own QEMU's single-client Windows pipe for the entire VM lifetime.

QEMU blocks pipe creation until a client connects, and does not reconnect a
disconnected client. A private, session-bound JSON mailbox lets other launcher
processes request only status and ACPI shutdown from this persistent owner.
"""
import json
import os
from pathlib import Path
import re
import queue
import subprocess
import sys
import threading
import time
import uuid

from environment import QemuStalled
from network_guard import read_state, write_state
from session_lock import exclusive
from windows.pipe import NamedPipe

COMMANDS = ('query-status', 'query-mice', 'system_powerdown', 'suspend')
# QEMU creates its pipes only after Windows has loaded and scanned its many
# DLLs. On a slow disk with an antivirus scanning a new installation, this
# can take more than a minute; keep connecting while QEMU is still alive.
PIPE_TIMEOUT = 180


def decode_qemu_output(data):
    """Decode QEMU stderr.

    UTF-8 is preferred. Otherwise try the Windows ANSI code page, which is how
    QEMU prints a localized "path not found". cp1251 is the Russian ANSI page;
    it is also tried explicitly so a log captured there still decodes when the
    process reading it is not running under that page.
    """
    if not data:
        return ''
    encodings = ['utf-8']
    if os.name == 'nt':
        encodings.append('mbcs')
    encodings.append('cp1251')
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        # cp1252 (and most mbcs pages) can decode every byte, so a Russian
        # message captured as cp1251 would look like Latin-1 mojibake. Prefer
        # the cp1251 reading when that is what the bytes actually are.
        if encoding == 'mbcs':
            try:
                as_cp1251 = data.decode('cp1251')
            except UnicodeDecodeError:
                as_cp1251 = None
            if as_cp1251 and as_cp1251 != text and _looks_like_mojibake(text) and not _looks_like_mojibake(as_cp1251):
                return as_cp1251.strip()
        return text.strip()
    return data.decode('utf-8', errors='replace').strip()


def _looks_like_mojibake(text):
    # "Системе" encoded as cp1251 and read as cp1252 starts with these letters.
    return 'Ñ' in text or 'Ã' in text or 'Ð' in text


def qemu_output_text(path):
    """QEMU writes its ANSI-code-page error to stderr. Show that text, not only a timeout."""
    if not path:
        return ''
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ''
    return decode_qemu_output(data)


def qemu_exit_message(code, path):
    detail = qemu_output_text(path)
    if len(detail) > 1800:
        detail = detail[-1800:]
    if detail:
        return f'QEMU завершился с кодом {code}: {detail}'
    return f'QEMU завершился с кодом {code} и не подготовил канал управления'


def paths(cfg):
    directory = Path(cfg['disk']).parent
    return {key: directory / ('control-' + key + '.json')
            for key in ('ready', 'request', 'response')}


def request(cfg, execute, timeout=None):
    if timeout is None:
        timeout = 900 if execute == 'suspend' else 10
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
    def __init__(self, cfg, process, reader_factory=None, connector=None, restore=None, output_path=None):
        self.cfg, self.process = cfg, process
        self.output_path = output_path
        # True to load the saved state into a QEMU started paused (-S).
        self.restore = restore
        self.restored = False
        self.suspended = False
        self.files = paths(cfg)
        self.session = uuid.uuid4().hex
        self.stop = threading.Event()
        self.ready = threading.Event()
        # True once this owner holds QEMU's control pipe.
        self.connected = False
        self.error = None
        self.replies = queue.Queue(maxsize=128)
        self.reader = None
        self.reader_factory = reader_factory or (lambda pipe: pipe)
        self.connector = connector or NamedPipe
        self.on_event = None
        # ACPI shutdown can be ignored after a restored TCG guest. Quit QEMU
        # if it is still running once this grace period ends.
        self.poweroff_grace = 120
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        write_state(self.files['ready'], dict(session=self.session, ready=False))
        self.thread.start()

    def wait_ready(self, timeout=PIPE_TIMEOUT):
        if self.restore:
            timeout = max(timeout, 600)
        deadline = time.monotonic() + timeout
        while not self.ready.wait(.05):
            if self.process.poll() is not None:
                if self.process.returncode:
                    raise RuntimeError(qemu_exit_message(self.process.returncode, self.output_path))
                return False
            if self.error:
                failure = QemuStalled if isinstance(self.error, QemuStalled) else RuntimeError
                raise failure('Не удалось открыть канал управления QEMU: ' + str(self.error))
            if time.monotonic() >= deadline:
                if self.process.poll() is not None:
                    if self.process.returncode:
                        raise RuntimeError(qemu_exit_message(self.process.returncode, self.output_path))
                    return False
                detail = qemu_output_text(self.output_path)
                message = 'QEMU не подготовил канал управления вовремя'
                if detail:
                    message += ': ' + (detail[-1800:] if len(detail) > 1800 else detail)
                raise (RuntimeError if self.connected else QemuStalled)(message)
        return True

    def read_replies(self, pipe):
        try:
            while not self.stop.is_set():
                line = pipe.readline(65537)
                if not line or len(line) > 65536:
                    raise RuntimeError('Канал управления QEMU прерван')
                reply = json.loads(line)
                if isinstance(reply, dict) and 'id' in reply:
                    self.replies.put_nowait(reply)
                elif isinstance(reply, dict) and 'event' in reply and self.on_event:
                    try:
                        self.on_event(reply)
                    except Exception as error:
                        print('QMP event handler: ' + str(error), file=sys.stderr, flush=True)
                # QMP events must be drained even with no pending command.
                # Otherwise synchronous Windows pipe writes can block QEMU's
                # main loop (e.g. VSERPORT_CHANGE while the guest boots).
        except Exception as error:
            self.error = error
        finally:
            pipe.close()

    def escalate_poweroff(self, pipe):
        def quit_if_stuck():
            if self.stop.is_set() or self.process.poll() is not None:
                return
            try:
                data = json.dumps(dict(execute='quit', id=uuid.uuid4().hex)).encode() + b'\n'
                while data and self.process.poll() is None and not self.stop.is_set():
                    data = data[pipe.write(data):]
            except OSError:
                pass
        timer = threading.Timer(self.poweroff_grace, quit_if_stuck)
        timer.daemon = True
        timer.start()

    def start_reader(self, pipe):
        # NamedPipe supports concurrent OVERLAPPED reads and writes.
        stream = self.reader_factory(pipe)
        self.reader = threading.Thread(target=self.read_replies, args=(stream,), daemon=True)
        self.reader.start()

    def exchange(self, pipe, execute, arguments=None, timeout=10):
        identity = uuid.uuid4().hex
        message = dict(execute=execute, id=identity)
        if arguments is not None:
            message['arguments'] = arguments
        data = json.dumps(message).encode() + b'\n'
        while data:
            data = data[pipe.write(data):]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = self.replies.get(timeout=.1)
            except queue.Empty:
                if self.error:
                    raise RuntimeError(str(self.error))
                continue
            if reply.get('id') != identity:
                continue
            if 'error' in reply:
                raise RuntimeError(str(reply['error']))
            return reply['return']
        raise RuntimeError('Нет ответа QEMU на команду управления')

    def serve(self, pipe):
        line = pipe.readline(65537)
        if len(line) > 65536 or 'QMP' not in json.loads(line):
            raise RuntimeError('Неверный ответ канала управления QEMU')
        self.start_reader(pipe)
        self.exchange(pipe, 'qmp_capabilities')
        self.exchange(pipe, 'query-status')
        if self.restore:
            self.load(pipe)
        write_state(self.files['ready'], dict(session=self.session, ready=True, restored=self.restored))
        self.ready.set()
        last = None
        while not self.stop.wait(.05) and self.process.poll() is None:
            if self.error:
                raise RuntimeError(str(self.error))
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
            execute = command.get('execute')
            if execute not in COMMANDS + ('quit',) or set(command) != {'session', 'id', 'execute'}:
                reply['error'] = 'Unsupported Windows control command'
            elif command['execute'] == 'suspend':
                try:
                    reply['return'] = self.save(pipe)
                except RuntimeError as error:
                    reply['error'] = str(error)
                write_state(self.files['response'], reply)
                if self.suspended:
                    # QEMU exits without answering; its exit ends this loop.
                    try:
                        data = json.dumps(dict(execute='quit', id=uuid.uuid4().hex)).encode() + b'\n'
                        while data:
                            data = data[pipe.write(data):]
                    except OSError:
                        pass
                continue
            elif execute == 'system_powerdown':
                reply['return'] = self.exchange(pipe, 'system_powerdown')
                write_state(self.files['response'], reply)
                self.escalate_poweroff(pipe)
                continue
            elif command['execute'] == 'quit':
                # Used only to abandon the hidden first-run boot. QEMU exits
                # without replying, so answer first.
                reply['return'] = {}
                write_state(self.files['response'], reply)
                try:
                    data = json.dumps(dict(execute='quit', id=uuid.uuid4().hex)).encode() + b'\n'
                    while data:
                        data = data[pipe.write(data):]
                except OSError:
                    pass
                continue
            else:
                reply['return'] = self.exchange(pipe, command['execute'])
            write_state(self.files['response'], reply)

    def job(self, pipe, execute, arguments, timeout):
        """Run a QMP job to completion; return its error text or None."""
        identity = 'claude-' + uuid.uuid4().hex[:12]
        self.exchange(pipe, execute, dict(arguments, **{'job-id': identity}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = self.exchange(pipe, 'query-jobs')
            current = next((item for item in jobs if item.get('id') == identity), None)
            if current and current.get('status') == 'concluded':
                self.exchange(pipe, 'job-dismiss', {'id': identity})
                return current.get('error')
            time.sleep(.2)
        return 'timeout'

    def snapshot(self, pipe, execute, timeout):
        from windows import state
        arguments = dict(tag=state.TAG, devices=[state.NODE])
        if execute != 'snapshot-delete':
            arguments['vmstate'] = state.NODE
        return self.job(pipe, execute, arguments, timeout)

    def load(self, pipe):
        """Load the saved guest into this QEMU (started paused with -S)."""
        error = self.snapshot(pipe, 'snapshot-load', 600)
        if error:
            raise RuntimeError('Сохранённое состояние не загрузилось: ' + str(error))
        self.exchange(pipe, 'cont')
        self.restored = True
        # The guest now changes the disk; its saved memory is obsolete. QEMU
        # holds the image, so remove the snapshot through QEMU itself.
        from windows import state
        state.discard(self.cfg, delete_snapshot=False)
        self.snapshot(pipe, 'snapshot-delete', 600)

    def save(self, pipe):
        """Pause the guest and store its memory in a disk image snapshot."""
        from windows import state
        state.discard(self.cfg, delete_snapshot=False)
        self.exchange(pipe, 'stop')
        try:
            # An earlier incomplete save may have left the same tag.
            self.snapshot(pipe, 'snapshot-delete', 600)
            error = self.snapshot(pipe, 'snapshot-save', 900)
        except RuntimeError as failure:
            error = str(failure)
        if error:
            try:
                self.snapshot(pipe, 'snapshot-delete', 600)
            finally:
                self.exchange(pipe, 'cont')
            raise RuntimeError('Не удалось сохранить состояние Linux: ' + str(error))
        self.suspended = True
        return {'saved': True}

    def run(self):
        pipe = None
        try:
            deadline = time.monotonic() + PIPE_TIMEOUT
            while not self.stop.is_set() and self.process.poll() is None:
                try:
                    pipe = self.connector('\\\\.\\pipe\\' + self.cfg['qmp_pipe'])
                    self.connected = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise QemuStalled('Windows QMP pipe did not start')
                    self.stop.wait(.05)
            if pipe:
                self.serve(pipe)
        except Exception as error:
            self.error = error
            if not self.stop.is_set() and self.process.poll() is None:
                # Control loss must not leave an unmanageable owned VM alive.
                # QEMU can close its pipe just before a normal process exit.
                # Give that exit time to complete instead of changing it into
                # a spurious crash and retrying a cleanly stopped guest.
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
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
        if self.reader:
            self.reader.join(timeout=5)
