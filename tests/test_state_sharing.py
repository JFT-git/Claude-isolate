"""Keep Windows delete-sharing races from permanently killing network checks."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network_guard as guard


def approved():
    return guard.classify({'ip': '8.8.8.8', 'country': 'US'})


class StateSharingTests(unittest.TestCase):
    def test_held_reader_does_not_block_replace_and_new_reads_see_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / 'Тест😀'
            data.mkdir()
            lease = data / 'lease.json'
            guard.publish(lease, approved())
            with guard._state_stream(lease) as held:
                guard.publish(lease, {'allowed': False, 'reason': 'closed'})
                self.assertTrue(json.load(held)['allowed'])
                self.assertFalse(guard.read_state(lease)['allowed'])
                self.assertFalse(guard.permitted(lease))
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_monitor_pauses_after_write_failure_and_recovers_with_fresh_check(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory) / 'lease.json'
            guard.publish(lease, approved())
            stop = threading.Event()
            publish = guard.publish
            writes = 0
            probes = 0
            def flaky_publish(*args):
                nonlocal writes
                writes += 1
                if writes == 1:
                    raise PermissionError('simulated Windows sharing violation')
                return publish(*args)
            def probe(*args):
                nonlocal probes
                probes += 1
                if probes == 2:
                    self.assertFalse(guard.permitted(lease))
                    self.assertTrue(lease.with_suffix('.paused').exists())
                if probes == 3:
                    self.assertTrue(guard.permitted(lease))
                    stop.set()
                return approved()
            with patch.object(guard, 'CHECK_INTERVAL', .001), \
                    patch.object(guard, 'publish', side_effect=flaky_publish), \
                    patch.object(guard, 'probe', side_effect=probe):
                guard.monitor(None, lease, stop, 'system', approved())
            self.assertEqual(probes, 3)
            self.assertTrue(guard.permitted(lease))
            self.assertFalse(lease.with_suffix('.revoked').exists())

    def test_oversized_status_is_not_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory) / 'lease.json'
            lease.write_text(' ' * 65537 + '{}', encoding='utf-8')
            self.assertFalse(guard.permitted(lease))
            with self.assertRaises(ValueError):
                guard.read_state(lease)

    @unittest.skipUnless(os.name == 'nt', 'Windows file-sharing stress test')
    def test_parallel_windows_readers_do_not_kill_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory) / 'lease.json'
            guard.publish(lease, approved())
            stop = threading.Event()
            errors = []
            def reader():
                try:
                    while not stop.is_set():
                        guard.read_state(lease)
                        guard.permitted(lease)
                except Exception as error:
                    errors.append(error)
            threads = [threading.Thread(target=reader) for _ in range(8)]
            for thread in threads:
                thread.start()
            try:
                for _ in range(100):
                    guard.publish(lease, approved())
            finally:
                stop.set()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertFalse(errors)
            self.assertTrue(guard.permitted(lease))


if __name__ == '__main__':
    unittest.main()
