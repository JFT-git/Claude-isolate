"""Multiplex filtered relay connections over a private QEMU named pipe.

There is no TCP listener, remote shell, file transfer, or host filesystem API.
OpenSSH in the guest multiplexes channels; each channel runs the existing relay
over ordinary Windows pipes, avoiding libslirp's unsupported socket spawning.
"""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time


def boot_command(root):
    stream = (root / 'guest/serial-stream.py').read_text(encoding='utf-8')
    proxy = shlex.join(['python3', '-c', stream])
    command = ['/usr/bin/ssh', '-N', '-T', '-oBatchMode=yes',
               '-oPreferredAuthentications=none', '-oStrictHostKeyChecking=no',
               '-oUserKnownHostsFile=/dev/null', '-oExitOnForwardFailure=yes',
               '-oServerAliveInterval=10', '-oServerAliveCountMax=2',
               '-oProxyCommand=' + proxy,
               '-L', '10.0.2.100:7890:claude.gateway:7890', 'claude-gateway@private-vm']
    return ['sh', '-c',
            'ip address replace 10.0.2.100/32 dev lo; '
            '(while true; do ' + shlex.join(command) +
            '; sleep 1; done) </dev/null >/run/claude-gateway.log 2>&1 &']


class Pipe:
    def __init__(self, pipe):
        self.pipe = pipe

    def recv(self, count):
        return self.pipe.read(count)

    def send(self, data):
        return self.pipe.write(data)

    def settimeout(self, value):
        pass  # The QEMU pipe closes when the owned VM exits.

    def close(self):
        if os.name == 'nt':
            import ctypes
            import msvcrt
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            try:
                api.CancelIoEx(msvcrt.get_osfhandle(self.pipe.fileno()), None)
            except (ValueError, OSError):
                pass
        self.pipe.close()


class Gateway:
    def __init__(self, cfg, env):
        self.cfg, self.env = cfg, env
        self.stop = threading.Event()
        self.transport = None
        self.processes = set()
        self.lock = threading.Lock()
        self.capacity = threading.BoundedSemaphore(64)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def channel(self, channel):
        process = None
        try:
            command = ([sys.executable, 'relay'] if getattr(sys, 'frozen', False)
                       else [sys.executable, str(Path(__file__).resolve().parents[1] / 'environment.py'), 'relay'])
            command += ['--mode', self.cfg['network_mode'], '--web-access', self.cfg['web_access']]
            if self.cfg['network_mode'] == 'proxy':
                command += ['--port', str(self.cfg['proxy_port'])]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       env=self.env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            with self.lock:
                self.processes.add(process)
            def upload():
                try:
                    while not self.stop.is_set():
                        data = channel.recv(32768)
                        if not data:
                            break
                        process.stdin.write(data)
                        process.stdin.flush()
                except (OSError, EOFError):
                    pass
                finally:
                    process.stdin.close()
            threading.Thread(target=upload, daemon=True).start()
            while not self.stop.is_set():
                data = process.stdout.read1(32768)
                if not data:
                    break
                channel.sendall(data)
        except (OSError, EOFError):
            pass
        finally:
            channel.close()
            if process:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
                process.stdout.close()
                with self.lock:
                    self.processes.discard(process)
            self.capacity.release()

    def serve(self, stream):
        import paramiko
        gateway = self
        class Server(paramiko.ServerInterface):
            def check_auth_none(self, username):
                # This transport exists only inside the owned VM's serial pipe.
                return paramiko.AUTH_SUCCESSFUL if username == 'claude-gateway' else paramiko.AUTH_FAILED

            def check_channel_direct_tcpip_request(self, chanid, origin, destination):
                if destination == ('claude.gateway', 7890) and gateway.capacity.acquire(blocking=False):
                    return paramiko.OPEN_SUCCEEDED
                return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_global_request(self, kind, message):
                return kind == 'keepalive@openssh.com'
        transport = paramiko.Transport(stream)
        self.transport = transport
        transport.add_server_key(paramiko.RSAKey.generate(2048))
        transport.start_server(server=Server())
        try:
            while not self.stop.is_set() and transport.is_active():
                channel = transport.accept(.5)
                if channel:
                    threading.Thread(target=self.channel, args=(channel,), daemon=True).start()
        finally:
            transport.close()

    def run(self):
        pipe = None
        try:
            deadline = time.monotonic() + 60
            while not self.stop.is_set():
                try:
                    pipe = open('\\\\.\\pipe\\' + self.cfg['qmp_pipe'] + '-gateway', 'r+b', buffering=0)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Private Windows gateway pipe did not start')
                    self.stop.wait(.2)
            if pipe:
                self.serve(Pipe(pipe))
        except Exception as error:
            print('Windows private gateway: ' + str(error), file=sys.stderr, flush=True)
        finally:
            if pipe and not pipe.closed:
                pipe.close()
            # Losing the bridge leaves the VM with no Internet path.

    def close(self):
        self.stop.set()
        if self.transport:
            self.transport.close()
        with self.lock:
            for process in list(self.processes):
                if process.poll() is None:
                    process.terminate()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
