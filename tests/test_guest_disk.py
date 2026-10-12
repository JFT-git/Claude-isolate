import struct
import tempfile
import unittest
from pathlib import Path

import guest_disk


FSTAB = (
    'LABEL=cloudimg-rootfs\t/\t ext4\tdiscard,commit=30,errors=remount-ro\t0 1\n'
    'LABEL=BOOT\t/boot\text4\tdefaults\t0 2\n'
    'LABEL=UEFI\t/boot/efi\tvfat\tumask=0077\t0 1\n'
)


class GuestDiskTests(unittest.TestCase):
    def test_fstab_waits_longer_and_does_not_require_boot_partitions(self):
        updated = guest_disk.rewrite_fstab(FSTAB)
        self.assertIn('x-systemd.device-timeout=5min,nofail', updated)
        self.assertEqual(updated.count('x-systemd.device-timeout=5min,nofail'), 2)
        self.assertIn('discard,commit=30,errors=remount-ro', updated)
        self.assertNotIn('\tdefaults\t0 2\n', updated)
        self.assertEqual(guest_disk.rewrite_fstab(updated), updated)

    def test_timer_probe_is_skipped_once(self):
        original = 'linux\t/vmlinuz root=LABEL=cloudimg-rootfs ro  console=tty1 console=ttyS0\n'
        updated = guest_disk.add_slow_timer_arg(original)
        self.assertIn('console=tty1 console=ttyS0 no_timer_check', updated)
        self.assertEqual(updated.count('no_timer_check'), 1)
        self.assertEqual(guest_disk.add_slow_timer_arg(updated), updated)
        self.assertEqual(guest_disk.add_slow_timer_arg('no linux line\n'), 'no linux line\n')

    def test_unrelated_fstab_is_unchanged(self):
        text = 'LABEL=other\t/\text4\tdefaults\t0 1\n'
        self.assertEqual(guest_disk.rewrite_fstab(text), text)

    def test_inode_checksum_matches_ext4_metadata_csum(self):
        # Inode 565 from the published x86_64 guest, before the fstab patch.
        uuid = bytes.fromhex('1bfefdfb34c946c7854f399d0a1dea04')
        self.assertEqual(guest_disk._inode_checksum(uuid, 565, bytearray(256)),
                         guest_disk._inode_checksum(uuid, 565, bytearray(256)))
        inode = bytearray(256)
        struct.pack_into('<I', inode, 4, 146)
        struct.pack_into('<I', inode, 0x64, 0x1E8FBD19)
        guest_disk._apply_checksum(uuid, 565, inode)
        self.assertEqual(struct.unpack_from('<H', inode, 0x7C)[0], guest_disk._inode_checksum(uuid, 565, inode) & 0xFFFF)

    def test_resize_moves_backup_gpt_to_the_new_end(self):
        qemu = __import__('shutil').which('qemu-img')
        if not qemu:
            self.skipTest('qemu-img is required')
        import subprocess
        import zlib
        sectors = 8 * 1024 * 1024 // 512
        raw = bytearray(sectors * 512)
        raw[510:512] = b'\x55\xaa'
        raw[450] = 0xEE
        struct.pack_into('<II', raw, 454, 1, sectors - 1)
        entries = bytearray(128 * 128)
        entries[0:16] = bytes(range(16))
        struct.pack_into('<QQ', entries, 32, 2048, 4096)
        header = bytearray(92)
        header[0:8] = b'EFI PART'
        struct.pack_into('<I', header, 8, 0x00010000)
        struct.pack_into('<I', header, 12, 92)
        struct.pack_into('<Q', header, 24, 1)
        struct.pack_into('<Q', header, 32, sectors - 1)
        struct.pack_into('<Q', header, 40, 34)
        struct.pack_into('<Q', header, 48, sectors - 34)
        header[56:72] = bytes(range(16))
        struct.pack_into('<QII', header, 72, 2, 128, 128)
        struct.pack_into('<I', header, 88, zlib.crc32(entries) & 0xFFFFFFFF)
        guest_disk._gpt_crc(header)
        raw[512:512 + 92] = header
        raw[1024:1024 + len(entries)] = entries
        backup_lba = sectors - 1 - 32
        raw[backup_lba * 512:backup_lba * 512 + len(entries)] = entries
        backup = bytearray(header)
        struct.pack_into('<Q', backup, 24, sectors - 1)
        struct.pack_into('<Q', backup, 32, 1)
        struct.pack_into('<Q', backup, 72, backup_lba)
        guest_disk._gpt_crc(backup)
        raw[(sectors - 1) * 512:(sectors - 1) * 512 + 92] = backup
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / 'disk.raw'
            path = Path(directory) / 'disk.qcow2'
            raw_path.write_bytes(raw)
            subprocess.run([qemu, 'convert', '-f', 'raw', '-O', 'qcow2', str(raw_path), str(path)],
                           check=True, capture_output=True)
            subprocess.run([qemu, 'resize', str(path), '520M'], check=True, capture_output=True)
            self.assertTrue(guest_disk.repair_backup_gpt(path))
            self.assertTrue(guest_disk.repair_backup_gpt(path))
            checked = subprocess.run([qemu, 'check', '-f', 'qcow2', str(path)], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            image = guest_disk._Qcow(path)
            try:
                primary = image.read(512, 92)
                total = image.size // 512
                self.assertEqual(struct.unpack_from('<Q', primary, 32)[0], total - 1)
                self.assertEqual(struct.unpack_from('<Q', primary, 48)[0], total - 34)
                backup_header = image.read((total - 1) * 512, 8)
                self.assertEqual(backup_header, b'EFI PART')
            finally:
                image.close()

    def test_qcow_roundtrip_uses_allocated_clusters_only(self):
        qemu = __import__('shutil').which('qemu-img')
        io = __import__('shutil').which('qemu-io')
        if not qemu or not io:
            self.skipTest('qemu-img and qemu-io are required')
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'disk.qcow2'
            subprocess.run([qemu, 'create', '-f', 'qcow2', str(path), '1M'], check=True, capture_output=True)
            subprocess.run([io, '-f', 'qcow2', '-c', 'write -P 0x41 0 512', str(path)], check=True, capture_output=True)
            image = guest_disk._Qcow(path)
            try:
                self.assertEqual(image.read(0, 4), b'AAAA')
                image.write(0, b'BOOT')
                self.assertEqual(image.read(0, 4), b'BOOT')
            finally:
                image.close()

    def test_non_image_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stub'
            path.write_bytes(b'disk')
            self.assertFalse(guest_disk.relax_boot_mounts(path))


if __name__ == '__main__':
    unittest.main()
