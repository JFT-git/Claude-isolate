import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from network_guard import read_state, write_state
from windows.control import Control, request


class WindowsControlTests(unittest.TestCase):
    def test_owner_connects_without_an_external_client_and_survives_repeated_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = {'disk': str(Path(temporary) / 'disk'), 'qmp_pipe': 'private-test'}
            process = Mock()
            process.poll.return_value = None
            client, server = socket.socketpair()
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
            server.settimeout(5)
            stream = client.makefile('rwb', buffering=0)
            commands = []
            failures = []
            events_sent = threading.Event()
            def qemu():
                try:
                    with server.makefile('rwb', buffering=0) as pipe:
                        pipe.write(b'{"QMP":{"version":{}}}\n')
                        for _ in range(5):
                            command = json.loads(pipe.readline())
                            commands.append(command['execute'])
                            result = {'running': True} if command['execute'] == 'query-status' else {}
                            pipe.write(json.dumps({'id': command['id'], 'return': result}).encode() + b'\n')
                            # A burst larger than a Windows pipe's buffer must
                            # not block the VM between external requests.
                            if len(commands) == 2:
                                for _ in range(100):
                                    pipe.write(b'{"event":"VSERPORT_CHANGE","data":{"padding":"' + b'x' * 512 + b'"}}\n')
                                events_sent.set()
                except Exception as error:
                    failures.append(error)
                finally:
                    server.close()
            peer = threading.Thread(target=qemu, daemon=True)
            peer.start()
            owner = Control(cfg, process, reader_factory=lambda pipe: client.makefile('rb', buffering=0),
                            connector=lambda path: stream)
            try:
                owner.start()
                self.assertTrue(owner.wait_ready(2))
                # No external request was needed to complete negotiation.
                self.assertEqual(commands, ['qmp_capabilities', 'query-status'])
                self.assertTrue(events_sent.wait(5), 'Events blocked QEMU without an external request')
                for _ in range(2):
                    self.assertTrue(request(cfg, 'query-status', timeout=2)['running'])
                self.assertEqual(request(cfg, 'system_powerdown', timeout=2), {})
                owner.close()
            finally:
                process.poll.return_value = 0
                owner.close()
                client.close()
                server.close()
                peer.join(2)
            self.assertFalse(failures)
            self.assertEqual(commands, ['qmp_capabilities', 'query-status',
                                        'query-status', 'query-status', 'system_powerdown'])
            self.assertFalse(read_state(owner.files['ready'])['ready'])

    def test_stale_sessions_and_arbitrary_qmp_commands_are_not_forwarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = {'disk': str(Path(temporary) / 'disk'), 'qmp_pipe': 'private-test'}
            process = Mock()
            process.poll.return_value = None
            owner = Control(cfg, process)
            # Test the mailbox loop without opening a real Windows pipe.
            pipe = Mock()
            pipe.readline.return_value = b'{"QMP":{}}\n'
            with patch.object(owner, 'exchange', return_value={'running': True}) as exchange, \
                 patch.object(owner, 'start_reader'):
                thread = threading.Thread(target=owner.serve, args=(pipe,), daemon=True)
                thread.start()
                self.assertTrue(owner.wait_ready(2))
                write_state(owner.files['request'], {'session': 'old-launch', 'id': uuid.uuid4().hex,
                                                     'execute': 'system_powerdown'})
                time.sleep(.12)
                self.assertEqual(exchange.call_count, 2)
                identity = uuid.uuid4().hex
                write_state(owner.files['request'], {'session': owner.session, 'id': identity,
                                                     'execute': 'human-monitor-command'})
                deadline = time.monotonic() + 2
                while not owner.files['response'].exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIn('error', read_state(owner.files['response']))
                self.assertEqual(exchange.call_count, 2)
                with self.assertRaises(ValueError):
                    request(cfg, 'quit')
                owner.stop.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
