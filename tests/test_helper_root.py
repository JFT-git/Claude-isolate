import os
from pathlib import Path
import runpy
import tempfile
import unittest

verify = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'guest/helper-root.py'))['verify']


@unittest.skipUnless(hasattr(os, 'makedev'), 'Guest Unix device identifiers')
class HelperRootTests(unittest.TestCase):
    def check_partition(self, disk, number):
        with tempfile.TemporaryDirectory() as temporary:
            sysfs = Path(temporary)
            expected = sysfs / 'devices/block/vda'
            expected.mkdir(parents=True)
            actual = sysfs / ('devices/block/' + disk)
            actual.mkdir(parents=True, exist_ok=True)
            part = actual / (disk + str(number))
            part.mkdir()
            (part / 'partition').write_text(str(number))
            (sysfs / 'class/block').mkdir(parents=True)
            (sysfs / 'class/block/vda').symlink_to(expected)
            (sysfs / 'dev/block').mkdir(parents=True)
            (sysfs / 'dev/block/254:1').symlink_to(part)
            verify(os.makedev(254, 1), sysfs)

    def test_original_root_partition(self):
        self.check_partition('vda', 1)

    def test_renumbered_root_partition(self):
        self.check_partition('vda', 4)

    def test_target_disk_cannot_be_helper_root(self):
        with self.assertRaises(RuntimeError):
            self.check_partition('vdb', 1)

    def test_missing_root_device_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError):
                verify(os.makedev(254, 1), Path(temporary))
