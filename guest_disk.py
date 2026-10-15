"""Patch a resized guest disk so the first boot can reach the desktop.

qemu-img resize grows the virtual disk and leaves the backup GPT where the old
end was. The kernel still creates the partitions, but it logs a broken
alternate header. On a slow software-emulated boot, udev then publishes
LABEL=BOOT and LABEL=UEFI after systemd's 90 second device timeout. Those
fstab entries are required, so systemd enters an emergency shell and the
desktop never starts. The backup header is moved to the new end of the disk,
and the two mounts stay wanted with a longer timeout and nofail.
"""
import os
import struct
import zlib
from pathlib import Path

_CRC32C = []
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = (_crc >> 1) ^ 0x82F63B78 if _crc & 1 else _crc >> 1
    _CRC32C.append(_crc)

_BOOT = 'LABEL=BOOT\t/boot\text4\tdefaults\t0 2\n'
_BOOT_NEW = 'LABEL=BOOT\t/boot\text4\tdefaults,x-systemd.device-timeout=5min,nofail\t0 2\n'
_UEFI = 'LABEL=UEFI\t/boot/efi\tvfat\tumask=0077\t0 1\n'
_UEFI_NEW = 'LABEL=UEFI\t/boot/efi\tvfat\tumask=0077,x-systemd.device-timeout=5min,nofail\t0 1\n'
_MARKER = 'LABEL=cloudimg-rootfs\t/\t ext4\t'


def _crc32c(data, seed=0xFFFFFFFF):
    crc = seed
    for byte in data:
        crc = _CRC32C[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc


def _inode_checksum(uuid, inum, inode):
    """ext4 metadata_csum over one inode. Matches e2fsprogs' raw CRC-32C."""
    seed = _crc32c(uuid)
    seed = _crc32c(struct.pack('<I', inum), seed)
    seed = _crc32c(inode[0x64:0x68], seed)
    body = bytearray(inode)
    body[0x7C:0x7E] = b'\x00\x00'
    body[0x82:0x84] = b'\x00\x00'
    return _crc32c(body, seed)


def _apply_checksum(uuid, inum, inode):
    value = _inode_checksum(uuid, inum, inode)
    inode[0x7C:0x7E] = struct.pack('<H', value & 0xFFFF)
    inode[0x82:0x84] = struct.pack('<H', (value >> 16) & 0xFFFF)


_TIMER_ARG = 'console=tty1 console=ttyS0'
# no_timer_check: TCG sometimes delivers the PIT interrupt after the kernel's
# IO-APIC timer probe gives up, and the kernel panics with "IO-APIC + timer
# doesn't work"; the option leaves the APIC enabled and only skips the probe.
# tsc=reliable: under software emulation the kernel's TSC warp test fails and
# it falls back to the emulated HPET. Every clock read then becomes a system
# call plus an emulated device access under QEMU's global lock; a Chromium
# based app reads the clock tens of thousands of times a second, which made
# typing lag by seconds. QEMU's TSC follows the host clock and is monotonic.
KERNEL_ARGS = ('no_timer_check', 'tsc=reliable')


def add_kernel_args(text, args=KERNEL_ARGS):
    """Add the missing args after each "console=tty1 console=ttyS0" kernel command line.

    Idempotent. Lines without that console pair (other entries, comments) are
    left unchanged. guest/offline-update.py applies the same rule in a guest.
    """
    binary = isinstance(text, (bytes, bytearray))
    data = text.decode('utf-8') if binary else text
    out = []
    for line in data.splitlines(keepends=True):
        found = line.find(_TIMER_ARG)
        if found >= 0:
            end = found + len(_TIMER_ARG)
            present = line[end:].split()
            missing = [arg for arg in args if arg not in present]
            if missing:
                line = line[:end] + ''.join(' ' + arg for arg in missing) + line[end:]
        out.append(line)
    updated = ''.join(out)
    if updated == data:
        return text
    return updated.encode() if binary else updated


def add_slow_timer_arg(text):
    """Backward-compatible name for add_kernel_args."""
    return add_kernel_args(text)


def rewrite_fstab(text):
    """Return fstab text that cannot emergency-boot on a late BOOT/UEFI label."""
    if _BOOT_NEW in text and _UEFI_NEW in text:
        return text
    if _BOOT not in text or _UEFI not in text:
        return text
    updated = text.replace(_BOOT, _BOOT_NEW, 1).replace(_UEFI, _UEFI_NEW, 1)
    if len(updated.encode()) > 4096:
        return text
    return updated


class _Qcow:
    def __init__(self, path):
        self.path = Path(path)
        self.file = self.path.open('r+b')
        try:
            header = self.file.read(104)
            magic, version = struct.unpack_from('>4sI', header, 0)
            if magic != b'QFI\xfb' or version not in (2, 3):
                raise ValueError('not a qcow2 image')
            self.cluster_bits = struct.unpack_from('>I', header, 20)[0]
            self.size = struct.unpack_from('>Q', header, 24)[0]
            self.crypt_method = struct.unpack_from('>I', header, 32)[0]
            self.l1_size = struct.unpack_from('>I', header, 36)[0]
            self.l1_offset = struct.unpack_from('>Q', header, 40)[0]
            self.refcount_offset = struct.unpack_from('>Q', header, 48)[0]
            self.refcount_clusters = struct.unpack_from('>I', header, 56)[0]
            if version >= 3:
                self.incompatible = struct.unpack_from('>Q', header, 72)[0]
                self.refcount_order = struct.unpack_from('>I', header, 96)[0]
            else:
                self.incompatible = 0
                self.refcount_order = 4
            if self.crypt_method or self.incompatible & ~1 or not 1 <= self.refcount_order <= 6:
                raise ValueError('unsupported qcow2 features')
            self.refcount_bits = 1 << self.refcount_order
            self.cluster = 1 << self.cluster_bits
            self.l2_entries = self.cluster // 8
            self._l2_bits = (self.l2_entries - 1).bit_length()
        except Exception:
            self.file.close()
            raise

    def close(self):
        self.file.close()

    def _host(self, virtual):
        if virtual < 0 or virtual >= self.size:
            raise ValueError('offset outside the image')
        l2_index = (virtual >> self.cluster_bits) & (self.l2_entries - 1)
        l1_index = virtual >> (self.cluster_bits + self._l2_bits)
        if l1_index >= self.l1_size:
            return None
        self.file.seek(self.l1_offset + l1_index * 8)
        l1 = struct.unpack('>Q', self.file.read(8))[0]
        if l1 & (1 << 62) or not (l1 & 0x00FFFFFFFFFFFE00):
            return None
        self.file.seek((l1 & 0x00FFFFFFFFFFFE00) + l2_index * 8)
        l2 = struct.unpack('>Q', self.file.read(8))[0]
        if l2 & (1 << 62) or l2 & 1 or not (l2 & 0x00FFFFFFFFFFFE00):
            return None
        return (l2 & 0x00FFFFFFFFFFFE00) + (virtual & (self.cluster - 1))

    def _refcount_block(self, host, create):
        per = self.cluster * 8 // self.refcount_bits
        index = host // self.cluster
        block_index, entry = divmod(index, per)
        table_entries = self.refcount_clusters * self.cluster // 8
        if block_index >= table_entries:
            raise ValueError('refcount table is too small')
        at = self.refcount_offset + block_index * 8
        self.file.seek(at)
        block = struct.unpack('>Q', self.file.read(8))[0]
        if block or not create:
            return block, entry
        block = self._append_cluster()
        self.file.seek(at)
        self.file.write(struct.pack('>Q', block))
        own_index, own_entry = divmod(block // self.cluster, per)
        if own_index == block_index:
            self._store_refcount(block, own_entry, 1)
        else:
            self.file.seek(self.refcount_offset + own_index * 8)
            previous = struct.unpack('>Q', self.file.read(8))[0]
            if not previous:
                raise ValueError('refcount block does not cover its own cluster')
            self._store_refcount(previous, own_entry, 1)
        return block, entry

    def _store_refcount(self, block, entry, value):
        width = self.refcount_bits // 8
        self.file.seek(block + entry * width)
        self.file.write(value.to_bytes(width, 'big'))

    def _append_cluster(self):
        end = self.file.seek(0, 2)
        if end % self.cluster:
            self.file.write(b'\x00' * (self.cluster - end % self.cluster))
            end = self.file.seek(0, 2)
        self.file.write(b'\x00' * self.cluster)
        return end

    def _allocate(self, virtual):
        """Allocate one data cluster for an unmapped virtual offset."""
        l1_index = virtual >> (self.cluster_bits + self._l2_bits)
        l2_index = (virtual >> self.cluster_bits) & (self.l2_entries - 1)
        if l1_index >= self.l1_size:
            raise ValueError('offset outside the L1 table')
        self.file.seek(self.l1_offset + l1_index * 8)
        l1 = struct.unpack('>Q', self.file.read(8))[0]
        l2_off = l1 & 0x00FFFFFFFFFFFE00
        if not l2_off or l1 & (1 << 62):
            l2_off = self._append_cluster()
            block, entry = self._refcount_block(l2_off, True)
            if not block:
                raise ValueError('could not refcount a new L2 table')
            self._store_refcount(block, entry, 1)
            self.file.seek(self.l1_offset + l1_index * 8)
            self.file.write(struct.pack('>Q', l2_off | (1 << 63)))
        data = self._append_cluster()
        block, entry = self._refcount_block(data, True)
        if not block:
            raise ValueError('could not refcount a new cluster')
        self._store_refcount(block, entry, 1)
        self.file.seek(l2_off + l2_index * 8)
        self.file.write(struct.pack('>Q', data | (1 << 63)))
        return data + (virtual & (self.cluster - 1))

    def read(self, offset, length):
        out = bytearray()
        while length:
            host = self._host(offset)
            take = min(length, self.cluster - (offset & (self.cluster - 1)))
            if host is None:
                out += b'\x00' * take
            else:
                self.file.seek(host)
                out += self.file.read(take)
            offset += take
            length -= take
        return bytes(out)

    def write(self, offset, data, *, allocate=False):
        view = memoryview(data)
        done = 0
        while done < len(view):
            host = self._host(offset)
            take = min(len(view) - done, self.cluster - (offset & (self.cluster - 1)))
            if host is None:
                if not allocate:
                    raise ValueError('refusing to allocate a new qcow2 cluster')
                host = self._allocate(offset)
            self.file.seek(host)
            self.file.write(view[done:done + take])
            offset += take
            done += take

    def flush(self):
        self.file.flush()
        os.fsync(self.file.fileno())


def _partitions(image):
    data = image.read(512, 92)
    if data[:8] != b'EFI PART':
        return
    part_lba, nparts, part_size = struct.unpack_from('<QII', data, 72)
    entries = image.read(part_lba * 512, nparts * part_size)
    for index in range(nparts):
        entry = entries[index * part_size:(index + 1) * part_size]
        if entry[:16] == b'\x00' * 16:
            continue
        first, last = struct.unpack_from('<QQ', entry, 32)
        yield first * 512, (last - first + 1) * 512


def _ext4_super(image, start):
    sb = image.read(start + 1024, 0x100)
    if struct.unpack_from('<H', sb, 0x38)[0] != 0xEF53:
        return None
    label = sb[0x78:0x88].split(b'\x00', 1)[0]
    return dict(start=start, uuid=sb[0x68:0x78], label=label,
                block=1024 << struct.unpack_from('<I', sb, 0x18)[0],
                blocks_per_group=struct.unpack_from('<I', sb, 0x20)[0],
                inodes_per_group=struct.unpack_from('<I', sb, 0x28)[0],
                inode_size=struct.unpack_from('<H', sb, 0x58)[0],
                desc_size=struct.unpack_from('<H', sb, 0xFE)[0] or 32)


def _iter_inodes(image, fs, limit_groups):
    for group in range(limit_groups):
        desc = image.read(fs['start'] + fs['block'] + group * fs['desc_size'], fs['desc_size'])
        table = struct.unpack_from('<I', desc, 8)[0]
        if not table:
            continue
        for index in range(fs['inodes_per_group']):
            raw = bytearray(image.read(fs['start'] + table * fs['block'] + index * fs['inode_size'], fs['inode_size']))
            yield group * fs['inodes_per_group'] + index + 1, raw, fs['start'] + table * fs['block'] + index * fs['inode_size']


def _extents(inode):
    if not struct.unpack_from('<I', inode, 32)[0] & 0x80000:
        return
    magic, entries, _, depth = struct.unpack_from('<HHHI', inode, 40)
    if magic != 0xF30A or depth != 0:
        return
    for entry in range(entries):
        block, length, start_hi, start_lo = struct.unpack_from('<IHHI', inode, 40 + 12 + entry * 12)
        yield block, length & 0xFFFF, (start_hi << 32) | start_lo


def _gpt_crc(header):
    size = struct.unpack_from('<I', header, 12)[0]
    struct.pack_into('<I', header, 16, 0)
    struct.pack_into('<I', header, 16, zlib.crc32(header[:size]) & 0xFFFFFFFF)
    return size


def repair_backup_gpt(path):
    """Move a resized image's backup GPT to the new end of the disk.

    Returns False when the file is not a GPT qcow2. A disk that is already
    consistent is left unchanged.
    """
    try:
        image = _Qcow(path)
    except (OSError, ValueError, struct.error):
        return False
    try:
        if image.size % 512:
            raise ValueError('guest disk size is not a whole number of sectors')
        header = bytearray(image.read(512, 92))
        if header[:8] != b'EFI PART':
            return False
        sectors = image.size // 512
        alternate = struct.unpack_from('<Q', header, 32)[0]
        if alternate == sectors - 1:
            return True
        part_lba, count, entry_size = struct.unpack_from('<QII', header, 72)
        entries = image.read(part_lba * 512, count * entry_size)
        if zlib.crc32(entries) & 0xFFFFFFFF != struct.unpack_from('<I', header, 88)[0]:
            raise ValueError('primary GPT entries failed their checksum')
        entry_sectors = (len(entries) + 511) // 512
        new_entries = sectors - 1 - entry_sectors
        if new_entries <= part_lba:
            raise ValueError('resized disk is too small for a backup GPT')
        struct.pack_into('<Q', header, 32, sectors - 1)
        struct.pack_into('<Q', header, 48, new_entries - 1)
        size = _gpt_crc(header)
        image.write(512, bytes(header[:size]))
        backup = bytearray(header[:size])
        struct.pack_into('<Q', backup, 24, sectors - 1)
        struct.pack_into('<Q', backup, 32, 1)
        struct.pack_into('<Q', backup, 72, new_entries)
        backup_size = _gpt_crc(backup)
        image.write(new_entries * 512, entries, allocate=True)
        image.write((sectors - 1) * 512, bytes(backup[:backup_size]).ljust(512, b'\x00'), allocate=True)
        mbr = bytearray(image.read(446, 16))
        if mbr[4] == 0xEE:
            struct.pack_into('<I', mbr, 12, min(sectors - 1, 0xFFFFFFFF))
            image.write(446, bytes(mbr))
        image.flush()
        return True
    finally:
        image.close()


def _load_inode(image, fs, number):
    group, index = divmod(number - 1, fs['inodes_per_group'])
    desc = image.read(fs['start'] + fs['block'] + group * fs['desc_size'], fs['desc_size'])
    table = struct.unpack_from('<I', desc, 8)[0]
    if not table:
        return None, None
    at = fs['start'] + table * fs['block'] + index * fs['inode_size']
    return bytearray(image.read(at, fs['inode_size'])), at


def _file_bytes(image, fs, inode):
    size = struct.unpack_from('<I', inode, 4)[0]
    if size <= 0 or size > 1024 * 1024:
        return b''
    extents = list(_extents(inode) or ())
    if not extents:
        return b''
    output = bytearray(size)
    for logical, length, physical in extents:
        data = image.read(fs['start'] + physical * fs['block'], length * fs['block'])
        offset = logical * fs['block']
        if offset >= size:
            continue
        output[offset:offset + min(len(data), size - offset)] = data[:size - offset]
    return bytes(output)


def _directory_entries(image, fs, inode):
    data = _file_bytes(image, fs, inode)
    offset = 0
    while offset + 8 <= len(data):
        number, record, name_len, _file_type = struct.unpack_from('<IHBB', data, offset)
        if record < 8 or offset + record > len(data):
            break
        name = data[offset + 8:offset + 8 + name_len]
        offset += record
        if number and name not in (b'.', b'..'):
            yield number, name


def _grub_files(image, fs):
    """Yield grub configs under /boot or /etc without walking the whole disk."""
    allowed = {b'boot', b'etc', b'grub', b'default', b'efi'}
    stack = [(2, b'', 0, True)]
    seen = set()
    produced = 0
    while stack and produced < 32:
        number, path, depth, descend = stack.pop()
        if number in seen or depth > 6:
            continue
        seen.add(number)
        inode, inode_at = _load_inode(image, fs, number)
        if inode is None:
            continue
        mode = struct.unpack_from('<H', inode, 0)[0] & 0xF000
        if mode == 0x8000 and (path.endswith(b'/grub.cfg') or path.endswith(b'/grub')):
            produced += 1
            yield path, number, inode, inode_at
            continue
        if mode != 0x4000 or not descend:
            continue
        under = b'grub' in path.split(b'/') or b'default' in path.split(b'/')
        for child, name in _directory_entries(image, fs, inode):
            if not 1 <= child <= 2_000_000:
                continue
            child_path = path + b'/' + name
            stack.append((child, child_path, depth + 1, name in allowed or under))


def allow_slow_ioapic(path):
    """Add KERNEL_ARGS to the guest bootloader config when it fits in place."""
    try:
        image = _Qcow(path)
    except (OSError, ValueError, struct.error):
        return False
    try:
        changed = False
        for start, _length in _partitions(image):
            filesystem = _ext4_super(image, start)
            if filesystem is None or filesystem['label'] not in (b'cloudimg-rootfs', b'BOOT'):
                continue
            for file_path, number, inode, inode_at in _grub_files(image, filesystem):
                extents = list(_extents(inode) or ())
                if len(extents) != 1 or extents[0][0] != 0:
                    continue
                size = struct.unpack_from('<I', inode, 4)[0]
                capacity = extents[0][1] * filesystem['block']
                body = image.read(filesystem['start'] + extents[0][2] * filesystem['block'], size)
                try:
                    updated = add_slow_timer_arg(body.decode('utf-8'))
                except UnicodeError:
                    continue
                if updated.encode() == body:
                    continue
                encoded = updated.encode()
                if len(encoded) > capacity:
                    print('Guest disk: bootloader config has no room for the kernel options', flush=True)
                    continue
                image.write(filesystem['start'] + extents[0][2] * filesystem['block'], encoded)
                struct.pack_into('<I', inode, 4, len(encoded))
                _apply_checksum(filesystem['uuid'], number, inode)
                image.write(inode_at, inode)
                changed = True
                print('Guest disk: added', ' '.join(KERNEL_ARGS), 'to', file_path.decode('utf-8', 'replace'), flush=True)
        if changed:
            image.flush()
        return changed
    except (OSError, ValueError, struct.error) as error:
        print('Guest disk: bootloader patch failed:', error, flush=True)
        return False
    finally:
        image.close()


def prepare_disk(path):
    """Repair the backup GPT, then keep a late or slow boot from stopping the desktop."""
    repaired = repair_backup_gpt(path)
    relaxed = relax_boot_mounts(path)
    timer = allow_slow_ioapic(path)
    if repaired:
        print('Guest disk: backup GPT is at the end of the resized disk', flush=True)
    if relaxed:
        print('Guest disk: BOOT and UEFI mounts wait longer and are not fatal', flush=True)
    if timer:
        print('Guest disk: kernel skips the emulated IO-APIC timer test and keeps the fast TSC clock', flush=True)
    return repaired or relaxed or timer


def relax_boot_mounts(path):
    """Make LABEL=BOOT and LABEL=UEFI non-fatal on a prepared cloud image.

    Images that are not this guest, including test stubs, are left unchanged.
    """
    try:
        image = _Qcow(path)
    except (OSError, ValueError, struct.error):
        return False
    try:
        filesystem = None
        for start, _length in _partitions(image):
            superblock = _ext4_super(image, start)
            if superblock and superblock['label'] == b'cloudimg-rootfs':
                filesystem = superblock
                break
        if filesystem is None:
            return False
        signature = (_MARKER + 'discard,commit=30,errors=remount-ro').encode()
        for inum, inode, inode_at in _iter_inodes(image, filesystem, 8):
            size = struct.unpack_from('<I', inode, 4)[0]
            if not 100 <= size <= 512:
                continue
            extents = list(_extents(inode))
            if len(extents) != 1 or extents[0][1] != 1 or extents[0][0] != 0:
                continue
            data_at = filesystem['start'] + extents[0][2] * filesystem['block']
            body = image.read(data_at, size)
            if not body.startswith(signature):
                continue
            text = body.decode('utf-8')
            updated = rewrite_fstab(text)
            if updated == text:
                return False
            encoded = updated.encode()
            if len(encoded) > filesystem['block']:
                return False
            image.write(data_at, encoded + b'\x00' * (len(body) - len(encoded)) if len(encoded) < len(body) else encoded)
            struct.pack_into('<I', inode, 4, len(encoded))
            _apply_checksum(filesystem['uuid'], inum, inode)
            image.write(inode_at, inode)
            return True
        return False
    finally:
        image.close()
