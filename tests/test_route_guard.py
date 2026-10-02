import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network_guard as guard
from route_guard import changed, RouteWatcher


def event(kind, flags=0):
    return struct.pack('=HBBHHI', 12, 5, kind, 0, 0, flags)


class RouteTests(unittest.TestCase):
    def test_route_interface_and_address_changes_trigger(self):
        for kind in (1, 2, 3, 12, 13, 14, 18):
            self.assertTrue(changed(event(kind)))

    def test_route_queries_and_neighbor_cache_are_not_network_changes(self):
        for packet in (event(4), event(7), event(1, 0x400), event(2, 0x20000)):
            self.assertFalse(changed(packet))

    def test_failed_route_command_is_not_a_change(self):
        packet = bytearray(92)
        struct.pack_into('=HBB', packet, 0, 92, 5, 1)
        struct.pack_into('=i', packet, 24, 17) # EEXIST
        self.assertFalse(changed(packet))
        struct.pack_into('=i', packet, 24, 0)
        self.assertTrue(changed(packet))

    def test_truncated_unknown_version_fail_closed(self):
        for packet in (b'bad', struct.pack('=HBB', 30, 5, 1), struct.pack('=HBB', 4, 99, 4)):
            self.assertTrue(changed(packet))

    def test_route_event_revokes_even_after_concurrent_successful_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'lease.json'
            result = guard.classify({'ip':'8.8.8.8','country':'US'})
            guard.publish(path, result)
            source, sender = socket.socketpair()
            blocked = threading.Event()
            def revoke(reason):
                guard.revoke(path, reason=reason)
                blocked.set()
            watcher = RouteWatcher(revoke, source)
            watcher.start()
            try:
                start = time.monotonic()
                sender.sendall(event(2))
                self.assertTrue(blocked.wait(.5))
                print('Route-event processing: %.1f ms' % ((time.monotonic()-start)*1000))
                guard.publish(path, result)  # a probe finishing late cannot reopen
                self.assertFalse(guard.permitted(path))
            finally:
                watcher.close();sender.close()

    def test_watcher_loss_fails_closed(self):
        source, sender = socket.socketpair()
        blocked = threading.Event()
        watcher = RouteWatcher(lambda _: blocked.set(), source)
        watcher.start(); sender.close()
        self.assertTrue(blocked.wait(.5));watcher.close()

    def test_wall_clock_expiry_covers_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'lease.json'
            guard.publish(path, guard.classify({'ip':'8.8.8.8','country':'US'}))
            data = json.loads(path.read_text());data['wall_expires'] = time.time()-1
            path.write_text(json.dumps(data))
            self.assertFalse(guard.permitted(path))
