"""Background work that made typing in the guest lag under software emulation."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import environment
import guest_disk
import network_guard
from windows import backend, guest_update
from windows import clipboard_image
from windows.clipboard_bridge import ClipboardBridge


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


offline_update = load('offline_update_latency', ROOT / 'guest/offline-update.py')
guest_bridge = load('guest_clipboard_bridge_latency', ROOT / 'guest/clipboard-bridge.py')

GRUB = ('menuentry "Ubuntu" {\n'
        '\tlinux\t/vmlinuz-6.8.0-142-generic root=LABEL=cloudimg-rootfs ro  console=tty1 console=ttyS0\n'
        '}\n'
        'menuentry "Ubuntu, recovery" {\n'
        '\tlinux\t/vmlinuz root=LABEL=cloudimg-rootfs ro recovery nomodeset console=tty1 console=ttyS0 no_timer_check\n'
        '}\n'
        '# comment without the console pair\n')


class KernelArgumentTests(unittest.TestCase):
    def test_tsc_clock_and_timer_check_are_added_once(self):
        updated = guest_disk.add_kernel_args(GRUB)
        lines = updated.splitlines()
        self.assertTrue(lines[1].endswith('console=tty1 console=ttyS0 no_timer_check tsc=reliable'))
        self.assertTrue(lines[4].endswith('console=tty1 console=ttyS0 tsc=reliable no_timer_check'))
        self.assertEqual(updated.count('tsc=reliable'), 2)
        self.assertEqual(guest_disk.add_kernel_args(updated), updated)
        self.assertEqual(lines[-1], '# comment without the console pair')
        self.assertEqual(guest_disk.add_kernel_args(GRUB.encode()), updated.encode())

    def test_guest_and_host_apply_the_same_rule(self):
        self.assertEqual(offline_update.add_kernel_args(GRUB, guest_disk.KERNEL_ARGS),
                         guest_disk.add_kernel_args(GRUB))

    def test_offline_update_patches_the_mounted_boot_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grub = root / 'boot/grub/grub.cfg'
            grub.parent.mkdir(parents=True)
            grub.write_text(GRUB, encoding='utf-8')
            self.assertTrue(offline_update.patch_bootloader(root, guest_disk.KERNEL_ARGS))
            self.assertEqual(grub.read_text(encoding='utf-8'), guest_disk.add_kernel_args(GRUB))
            self.assertFalse(offline_update.patch_bootloader(root, guest_disk.KERNEL_ARGS))
            self.assertFalse(offline_update.patch_bootloader(root, ['quiet; rm -rf /']))

    def test_a_linked_bootloader_config_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'root'
            outside = Path(temporary) / 'outside.cfg'
            outside.write_text(GRUB, encoding='utf-8')
            (root / 'boot/grub').mkdir(parents=True)
            (root / 'boot/grub/grub.cfg').symlink_to(outside)
            self.assertFalse(offline_update.patch_bootloader(root, guest_disk.KERNEL_ARGS))
            self.assertEqual(outside.read_text(encoding='utf-8'), GRUB)

    def test_existing_guests_receive_the_options_and_keep_them_on_kernel_updates(self):
        content = guest_update.payload({})
        self.assertEqual(content['kernel_args'], list(guest_disk.KERNEL_ARGS))
        dropin = next(item for item in content['files'] if item['path'] == '/etc/default/grub.d/99-claude-isolate.cfg')
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT no_timer_check tsc=reliable"',
                      dropin['content'])
        source = (ROOT / 'windows/guest_update.py').read_text(encoding='utf-8')
        self.assertIn('mount -t ext4 -o nodev,nosuid,noexec "$boot" /target/boot', source)
        self.assertIn('umount /target/boot', source)
        self.assertNotEqual(guest_update.REVISION, '0.4.2')


class GuestServiceTests(unittest.TestCase):
    def test_snapd_is_quiet_on_updated_guests_too(self):
        units = (ROOT / 'guest/quiet-units.txt').read_text().split()
        for unit in ('snapd.service', 'snapd.socket', 'snapd.seeded.service', 'pollinate.service'):
            self.assertIn(unit, units)
        self.assertTrue(any(item['path'] == '/etc/systemd/system/snapd.service' and item['content'] == ''
                            for item in guest_update.payload({})['files']))

    def test_preinstall_does_not_mask_a_unit_it_already_emptied(self):
        # systemctl mask refuses an existing regular file and would fail the image build.
        units = set((ROOT / 'guest/quiet-units.txt').read_text().split())
        for line in (ROOT / 'guest/preinstall.sh').read_text().splitlines():
            if line.startswith('systemctl mask '):
                self.assertEqual(units & set(line.split()[2:]), set(), line)


class LeaseCheckTests(unittest.TestCase):
    def test_relay_loops_share_one_answer_briefly(self):
        with tempfile.TemporaryDirectory() as temporary:
            lease = str(Path(temporary) / 'lease.json')
            calls = []

            def counted(path, now=None):
                calls.append(path)
                return True
            with patch.object(network_guard, 'permitted', side_effect=counted):
                for _ in range(200):
                    self.assertTrue(network_guard.permitted_now(lease))
                self.assertEqual(len(calls), 1)
                time.sleep(network_guard.LEASE_CHECK_CACHE * 2)
                network_guard.permitted_now(lease)
                self.assertEqual(len(calls), 2)

    def test_a_revoked_lease_closes_connections_quickly(self):
        with tempfile.TemporaryDirectory() as temporary:
            lease = Path(temporary) / 'lease.json'
            network_guard.publish(lease, network_guard.classify({'ip': '8.8.8.8', 'country': 'US'}))
            self.assertTrue(network_guard.permitted_now(str(lease)))
            network_guard.revoke(lease, reason='test')
            deadline = time.monotonic() + network_guard.LEASE_CHECK_CACHE + network_guard.RELAY_INTERVAL + .2
            while network_guard.permitted_now(str(lease)):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(network_guard.RELAY_INTERVAL)


class StatusPollTests(unittest.TestCase):
    def test_boot_log_is_read_again_only_after_it_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'boot.log'
            log.write_bytes(b'\x1b[32mCLAUDE-ISOLATION: desktop-ready\x1b[0m\n')
            backend._boot_cache.clear()
            raw, plain = backend.boot_tail(str(log))
            self.assertIn('CLAUDE-ISOLATION: desktop-ready', plain)
            self.assertNotIn('\x1b', plain)
            with patch.object(backend.Path, 'open', side_effect=AssertionError('read again')):
                self.assertEqual(backend.boot_tail(str(log)), (raw, plain))
            with log.open('ab') as output:
                output.write(b'more\n')
            self.assertIn('more', backend.boot_tail(str(log))[1])
            self.assertEqual(backend.boot_tail(str(Path(temporary) / 'missing.log')), ('', ''))

    def test_window_polls_once_a_second(self):
        self.assertIn('self._poll_timer.start(1000)', (ROOT / 'windows/app_qt_ui.py').read_text(encoding='utf-8'))


class ResourceTests(unittest.TestCase):
    def test_software_emulation_gets_up_to_four_virtual_cpus(self):
        for logical, expected in ((2, 1), (4, 2), (8, 4), (32, 4)):
            with patch.object(backend, 'host_memory', return_value=(16384, 10000)), \
                 patch.object(backend.os, 'cpu_count', return_value=logical):
                self.assertEqual(backend.automatic_resources()['cpus'], expected)


class _Pipe:
    closed = False

    def peek(self):
        return False

    def write(self, data):
        return len(data)

    def close(self):
        self.closed = True


class HostClipboardTests(unittest.TestCase):
    def bridge(self, sequences, rounds):
        bridge = ClipboardBridge.__new__(ClipboardBridge)
        bridge.stop = threading.Event()
        bridge.last_text = bridge.last_png = ''
        bridge.cfg = {'qmp_pipe': 'test'}
        reads = []
        values = iter(sequences)

        def sequence():
            try:
                return next(values)
            except StopIteration:
                bridge.stop.set()
                return sequences[-1]
        bridge.sequence = sequence
        bridge.get_text = lambda: reads.append('text') or 'same'
        bridge.get_png = lambda: reads.append('png') or None
        with patch('windows.clipboard_bridge.gateway_pipe', return_value=_Pipe()), \
             patch.object(bridge.stop, 'wait', return_value=False):
            bridge.run()
        return reads

    def test_clipboard_is_read_only_after_windows_reports_a_change(self):
        reads = self.bridge([7] * 20 + [8] * 20, 40)
        self.assertEqual(reads, ['text', 'png', 'text', 'png'])

    def test_without_a_sequence_number_it_polls_slowly(self):
        moments = iter([0.0, 0.3, 0.6, 2.5, 2.8])
        with patch('windows.clipboard_bridge.time.monotonic', side_effect=lambda: next(moments, 99.0)):
            reads = self.bridge([0, 0, 0, 0, 0], 5)
        self.assertEqual(reads.count('text'), 2)


class ImageConversionTests(unittest.TestCase):
    def test_dib_pixels_become_rgba_and_back(self):
        width, height = 3, 2
        stride = width * 4
        # Bottom-up BGRA rows; alpha 0 means opaque.
        bottom = bytes([1, 2, 3, 0, 4, 5, 6, 128, 7, 8, 9, 255])
        top = bytes([10, 11, 12, 0, 13, 14, 15, 1, 16, 17, 18, 2])
        header = struct.pack('<IiiHHIIiiII', 40, width, height, 1, 32, 0, stride * height, 0, 0, 0, 0)
        png = clipboard_image.dib_to_png(header + bottom + top)
        _, _, rows = clipboard_image.decode_png(png)
        self.assertEqual(rows[0], bytes([12, 11, 10, 255, 15, 14, 13, 1, 18, 17, 16, 2]))
        self.assertEqual(rows[1], bytes([3, 2, 1, 255, 6, 5, 4, 128, 9, 8, 7, 255]))
        dib = clipboard_image.png_to_dib(png)
        self.assertEqual(dib[40:40 + stride], bytes([1, 2, 3, 255, 4, 5, 6, 128, 7, 8, 9, 255]))

    def test_24_bit_dib_with_row_padding(self):
        width, height = 2, 1
        row = bytes([1, 2, 3, 4, 5, 6]) + b'\x00\x00'
        header = struct.pack('<IiiHHIIiiII', 40, width, -height, 1, 24, 0, len(row), 0, 0, 0, 0)
        _, _, rows = clipboard_image.decode_png(clipboard_image.dib_to_png(header + row))
        self.assertEqual(rows[0], bytes([3, 2, 1, 255, 6, 5, 4, 255]))


class GuestClipboardBridgeTests(unittest.TestCase):
    def run_loop(self, watch_connects):
        timeouts = []
        reads = []

        class Stop(Exception):
            pass

        def select(read, write, error, timeout):
            timeouts.append(timeout)
            if len(timeouts) >= 4:
                raise Stop()
            return [], [], []

        class Watch:
            def __init__(self):
                self.display = self.fd = None

            def connect(self):
                if watch_connects:
                    self.display, self.fd = object(), 99
                return watch_connects

            def changed(self):
                return False

        module = guest_bridge
        with patch.object(module.os.path, 'exists', return_value=True), \
             patch.object(module.os, 'open', return_value=5), patch.object(module.os, 'close'), \
             patch.object(module, 'load_framing'), patch.object(module, 'SelectionWatch', Watch), \
             patch.object(module.select, 'select', side_effect=select), \
             patch.object(module, 'xclip', side_effect=lambda args, data=None: reads.append(args)):
            with self.assertRaises(Stop):
                module.main()
        return timeouts, reads

    def test_without_x_events_the_guest_polls_every_two_seconds(self):
        timeouts, reads = self.run_loop(False)
        self.assertEqual(timeouts[0], 0)
        self.assertEqual(timeouts[1:], [guest_bridge.FALLBACK_POLL] * 3)
        self.assertGreaterEqual(guest_bridge.FALLBACK_POLL, 2)

    def test_with_x_events_the_guest_sleeps_until_something_changes(self):
        timeouts, reads = self.run_loop(True)
        self.assertEqual(timeouts, [0, None, None, None])
        # One read when the bridge starts, none while waiting.
        self.assertEqual(reads, [['-o'], ['-o', '-t', 'image/png']])


if __name__ == '__main__':
    unittest.main()
