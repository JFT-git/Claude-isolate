import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network_guard as guard


def approved(ip='8.8.8.8', country='US'):
    return guard.classify({'ip': ip, 'country': country})


class AddressLockTests(unittest.TestCase):
    def test_same_address_keeps_permission(self):
        policy = guard.SessionPolicy(approved())
        self.assertTrue(policy.evaluate(approved())['allowed'])

    def test_foreign_ip_change_latches_even_if_original_returns(self):
        policy = guard.SessionPolicy(approved())
        changed = policy.evaluate(approved('1.1.1.1', 'NL'))
        self.assertFalse(changed['allowed'])
        self.assertTrue(changed['locked'])
        self.assertFalse(policy.evaluate(approved())['allowed'])
        self.assertTrue(guard.SessionPolicy(approved()).evaluate(approved())['allowed'])

    def test_check_failure_cannot_silently_reopen(self):
        policy = guard.SessionPolicy(approved())
        self.assertFalse(policy.evaluate({'allowed': False})['allowed'])
        self.assertFalse(policy.evaluate(approved())['allowed'])

    def test_monitor_publishes_changed_ip_denial_to_gateway_and_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            lease, status = Path(directory)/'lease', Path(directory)/'status'
            guard.publish(lease, approved())
            with patch.object(guard, 'CHECK_INTERVAL', .005), patch.object(guard, 'probe', return_value=approved('1.1.1.1', 'NL')) as probe:
                guard.monitor(None, lease, threading.Event(), 'system', approved(), status)
            self.assertEqual(probe.call_count, 1)
            self.assertFalse(guard.permitted(lease))
            self.assertTrue(json.loads(status.read_text())['locked'])

    def test_network_event_pauses_and_only_fresh_result_reopens(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory)/'lease'
            events = guard.NetworkEvents(lease)
            guard.publish(lease, approved())
            generation = events.snapshot()
            events.pause()
            self.assertFalse(guard.permitted(lease))
            self.assertFalse(events.verified(generation, approved()))
            self.assertFalse(guard.permitted(lease))
            self.assertTrue(events.verified(events.snapshot(), approved()))
            self.assertTrue(guard.permitted(lease))

    def test_temporary_probe_failure_closes_then_same_ip_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory)/'lease'
            guard.publish(lease, approved())
            stop = threading.Event()
            calls = 0
            def probe(*args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError('temporary outage')
                self.assertFalse(guard.permitted(lease))
                if calls == 3:
                    stop.set()
                return approved()
            def observed_probe(*args):
                if calls == 2:
                    self.assertTrue(guard.permitted(lease))
                    stop.set()
                    return approved()
                return probe(*args)
            with patch.object(guard, 'CHECK_INTERVAL', .005), patch.object(guard, 'probe', side_effect=observed_probe):
                guard.monitor(None, lease, stop, 'system', approved())
            self.assertTrue(guard.permitted(lease))
            self.assertFalse(lease.with_suffix('.revoked').exists())

    def test_paused_check_cannot_override_permanent_block(self):
        with tempfile.TemporaryDirectory() as directory:
            lease=Path(directory)/'lease'
            events=guard.NetworkEvents(lease)
            events.pause()
            guard.revoke(lease)
            events.verified(events.snapshot(), approved())
            self.assertFalse(guard.permitted(lease))

    def test_rate_limit_keeps_gate_closed_and_backs_off(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory)/'lease'
            guard.publish(lease, approved())
            stop = Mock()
            stop.is_set.return_value = False
            def stopped_after_backoff(seconds):
                self.assertEqual(seconds, 120)
                self.assertFalse(guard.permitted(lease))
                return True
            stop.wait.side_effect = stopped_after_backoff
            with patch.object(guard, 'CHECK_INTERVAL', .001), \
                 patch.object(guard, 'probe', side_effect=guard.ProbeUnavailable('429', 120)) as probe:
                guard.monitor(None, lease, stop, 'system', approved())
            probe.assert_called_once()
