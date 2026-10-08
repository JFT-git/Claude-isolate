"""Multiplex filtered relay connections over a private QEMU named pipe.

There is no TCP listener, remote shell, file transfer, or host filesystem API.
OpenSSH in the guest multiplexes channels; each channel runs the existing
relay filter in a thread of this process, so a new guest connection does not
start another executable.
"""
import re
import shlex
import socket
import sys
import threading
import time

import environment
from windows.pipe import gateway_pipe

PORT_ID = 'gatewayport'
SYNC_PREFIX = b'\x00CLAUDE-SYNC-'
RESET_MARKER = b'\x00CLAUDE-RESET\n'
SYNC_MARKER = re.compile(re.escape(SYNC_PREFIX) + rb'([0-9a-f]{32})\n')


def guest_files(root):
    stream = (root / 'guest/serial-stream.py').read_text(encoding='utf-8')
    stream_path = '/usr/local/lib/claude-isolate/serial-stream.py'
    proxy = shlex.join(['/usr/bin/python3', stream_path])
    # Software emulation can stall the guest for many seconds; allow a minute
    # before OpenSSH abandons the session.
    command = ['/usr/bin/ssh', '-N', '-T', '-oBatchMode=yes',
               '-oPreferredAuthentications=none', '-oStrictHostKeyChecking=no',
               '-oUserKnownHostsFile=/dev/null', '-oExitOnForwardFailure=yes',
               '-oServerAliveInterval=10', '-oServerAliveCountMax=6',
               '-oProxyCommand=' + proxy,
               '-L', '10.0.2.100:7890:claude.gateway:7890', 'claude-gateway@private-vm']
    service = ('[Unit]\nDescription=Private isolated Windows gateway\n'
               'After=systemd-udevd.service\n[Service]\nType=simple\n'
               'ExecStartPre=/usr/sbin/ip address replace 10.0.2.100/32 dev lo\n'
               'ExecStart=' + shlex.join(command) + '\nRestart=always\nRestartSec=1\n'
               '[Install]\nWantedBy=multi-user.target\n')
    return [dict(path=stream_path, content=stream, permissions='0700'),
            dict(path='/etc/systemd/system/claude-gateway.service', content=service, permissions='0644')]


def boot_command(root):
    stream, service = guest_files(root)
    stream_path = stream['path']
    # A cloud-init background process disappears on reboot, and production
    # disables cloud-init after setup. Persist an independently enabled unit.
    return ['sh', '-c', 'set -eu; install -d -m 700 /usr/local/lib/claude-isolate; '
            'printf %s ' + shlex.quote(stream['content']) + ' > ' + stream_path + '; '
            'chmod 700 ' + stream_path + '; printf %s ' + shlex.quote(service['content']) +
            ' > /etc/systemd/system/claude-gateway.service; '
            'systemctl daemon-reload; systemctl enable claude-gateway.service; '
            # bootcmd runs before basic.target. Waiting here for a regular
            # service (which starts after basic.target) creates an ordering
            # deadlock. Queue it and let cloud-init release the early stage.
            'systemctl --no-block start claude-gateway.service']


class SessionStream:
    """One SSH session on the serial pipe, which outlives it.

    paramiko must not close the pipe. A new guest session marker ends this
    session at once; the marker and what follows it are kept in `carry`.
    """
    def __init__(self, stream, prefix=b'', nonce=None):
        self.stream = stream
        self.prefix, self.carry, self.nonce = prefix, b'', nonce
        self.tail = b''
        self.ended = False

    @property
    def _closed(self):
        return self.stream._closed

    def recv(self, count):
        while not self.ended:
            if self.prefix:
                data, self.prefix = self.prefix[:count], self.prefix[count:]
            else:
                data = self.stream.recv(count)
            if not data:
                return data
            combined = self.tail + data
            index = combined.find(SYNC_PREFIX)
            if index < 0:
                self.tail = combined[-(len(SYNC_PREFIX) - 1):]
                return data
            match = SYNC_MARKER.match(combined, index)
            if match and match.group(1) == self.nonce and index >= len(self.tail):
                # The guest repeated this session's marker before reading our
                # acknowledgement; it is not part of the SSH stream.
                data = combined[len(self.tail):index] + combined[match.end():]
                self.tail = b''
                if data:
                    return data
                continue
            self.carry, self.ended = combined[index:], True
        return b''

    def send(self, data):
        return self.stream.send(data)

    def settimeout(self, value):
        self.stream.settimeout(value)

    def close(self):
        pass


class Gateway:
    def __init__(self, cfg, env):
        self.cfg, self.env = cfg, env
        self.stop = threading.Event()
        self.transport = None
        self.channels = set()
        self.lock = threading.Lock()
        self.capacity = threading.BoundedSemaphore(64)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def port_event(self, event):
        # QEMU reports when the guest closes the serial port, i.e. when its
        # OpenSSH session ended. End our side promptly instead of waiting for
        # the next session marker to break the old transport.
        data = event.get('data') or {}
        if event.get('event') == 'VSERPORT_CHANGE' and data.get('id') == PORT_ID and not data.get('open'):
            transport = self.transport
            if transport:
                transport.close()

    def channel(self, channel):
        with self.lock:
            self.channels.add(channel)
        try:
            def read(size, timeout=None):
                channel.settimeout(timeout)
                try:
                    return channel.recv(size)
                except socket.timeout as error:
                    raise TimeoutError from error
            def write(data):
                if self.stop.is_set():
                    raise OSError('Gateway stopped')
                channel.sendall(data)
            environment.relay_stream(read, write, self.cfg.get('proxy_port'), self.cfg['network_mode'],
                                     self.cfg['web_access'], self.env.get('CLAUDE_NETWORK_LEASE'))
        except (OSError, EOFError):
            pass
        except Exception as error:
            print('Windows private gateway channel: ' + str(error), file=sys.stderr, flush=True)
        finally:
            try:
                channel.close()
            except (OSError, EOFError):
                pass
            with self.lock:
                self.channels.discard(channel)
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
        # The named pipe exists before the guest boots. Slow TCG boots must
        # not consume SSH's usual 15-second network handshake deadline.
        transport.banner_timeout = 600
        transport.handshake_timeout = 600
        transport.auth_timeout = 60
        self.transport = transport
        # Generated per VM session; ECDSA keys take milliseconds, unlike RSA.
        transport.add_server_key(paramiko.ECDSAKey.generate())
        transport.start_server(server=Server())
        try:
            while not self.stop.is_set() and transport.is_active():
                channel = transport.accept(.5)
                if channel:
                    threading.Thread(target=self.channel, args=(channel,), daemon=True).start()
        finally:
            transport.close()

    def synchronize(self, stream, buffer=b''):
        """Wait for the guest proxy's session marker; see guest/serial-stream.py.

        Everything before the newest marker belongs to an abandoned session.
        Returns the session nonce and bytes received after its marker, or
        None if the stream ended.
        """
        stream.settimeout(.5)
        while not self.stop.is_set():
            markers = list(SYNC_MARKER.finditer(buffer))
            if markers:
                nonce = markers[-1].group(1)
                # Our clock lets a restored guest correct its time.
                acknowledgement = (b'\x00CLAUDE-ACK-' + nonce + b' '
                                   + str(int(time.time() * 1000)).encode() + b'\n')
                while acknowledgement:
                    acknowledgement = acknowledgement[stream.send(acknowledgement):]
                return nonce, buffer[markers[-1].end():]
            try:
                chunk = stream.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                return None
            buffer = (buffer + chunk)[-8192:]
        return None

    def sessions(self, stream):
        # The guest restarts OpenSSH after a lost session; accept each new
        # session on the same serial stream for the lifetime of the VM.
        carry = b''
        # End any session the guest still holds from before this process
        # started (a restored VM); a new guest proxy discards this marker.
        data = RESET_MARKER
        while data:
            data = data[stream.send(data):]
        while not self.stop.is_set() and not stream.closed:
            session = None
            try:
                synchronized = self.synchronize(stream, carry)
                if synchronized is None:
                    break
                nonce, remainder = synchronized
                session = SessionStream(stream, remainder, nonce)
                self.serve(session)
            except Exception as error:
                if self.stop.is_set() or stream.closed:
                    break
                print('Windows private gateway session: ' + str(error), file=sys.stderr, flush=True)
            carry = session.carry if session else b''

    def run(self):
        pipe = None
        try:
            deadline = time.monotonic() + 60
            while not self.stop.is_set():
                try:
                    pipe = gateway_pipe('\\\\.\\pipe\\' + self.cfg['qmp_pipe'] + '-gateway')
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Private Windows gateway pipe did not start')
                    self.stop.wait(.2)
            if pipe:
                self.sessions(pipe)
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
            channels = list(self.channels)
        for channel in channels:
            try:
                channel.close()
            except (OSError, EOFError):
                pass
        if self.thread.is_alive():
            self.thread.join(timeout=5)
