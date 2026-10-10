"""Transactional, offline updates of an existing qcow2 guest, with no guest NIC."""
import json
import io
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid

import environment
import ubuntu_image
import release_image
from session_lock import exclusive
from windows.serial_gateway import guest_files as gateway_files

# Guest payload revision is independent of controller-only releases.
REVISION = '0.4.2'
IDLE_SERVICE = '[Service]\nNice=19\nCPUSchedulingPolicy=idle\nIOSchedulingClass=idle\n'


def boot_tuning_files(root):
    """Same masks and priorities as guest/preinstall.sh, as plain files."""
    units = (root / 'guest/quiet-units.txt').read_text(encoding='utf-8').split()
    if not all(re.fullmatch(r'[A-Za-z0-9_@.-]+\.(service|socket|timer|path)', unit) for unit in units):
        raise ValueError('Invalid quiet unit name')
    return ([dict(path='/etc/systemd/system/' + unit, content='', permissions='0644') for unit in units]
            + [dict(path='/etc/udev/rules.d/90-console-setup.rules', content='', permissions='0644')]
            + [dict(path='/etc/systemd/system/' + service + '.service.d/50-idle.conf',
                    content=IDLE_SERVICE, permissions='0644') for service in ('apt-daily', 'apt-daily-upgrade')])


def guest_files(root):
    marker = '/var/lib/claude-isolate/updated-' + REVISION
    service = ('[Unit]\nDescription=Update isolated desktop components\n'
               'Wants=network-online.target claude-gateway.service\n'
               'After=network-online.target claude-gateway.service nftables.service\n'
               'Before=lightdm.service claude-desktop-ready.service\n'
               'ConditionPathExists=/var/lib/claude-isolation-ready\n'
               'ConditionPathExists=!' + marker + '\nStartLimitIntervalSec=0\n'
               '[Service]\nType=oneshot\nExecStart=/usr/local/sbin/claude-environment-update\n'
               'TimeoutStartSec=1800\nRestart=on-failure\nRestartSec=30\n'
               'StandardOutput=journal+console\nStandardError=journal+console\n'
               '[Install]\nWantedBy=multi-user.target\n')
    return [dict(path='/etc/claude-isolate/revision', content=REVISION + '\n', permissions='0644'),
            dict(path='/usr/local/sbin/claude-environment-update',
                 content=(root / 'guest/update.sh').read_text(), permissions='0700'),
            dict(path='/etc/systemd/system/claude-environment-update.service', content=service, permissions='0644'),
            *boot_tuning_files(root)]


def payload(cfg):
    files = json.loads(environment.cloud_config(cfg).split('\n', 1)[1])['write_files']
    # Keep LightDM customization and global environment variables; do not run
    # bootstrap on an installed guest (it resets first-login preferences).
    excluded = {'/etc/environment', '/etc/lightdm/lightdm.conf.d/50-isolated.conf'}
    files = [item for item in files if item['path'] not in excluded]
    for item in gateway_files(environment.ROOT) + guest_files(environment.ROOT):
        files = [old for old in files if old['path'] != item['path']]
        files.append(item)
    files.append(dict(path='/etc/nftables.conf',
                      content=(environment.ROOT / 'guest/firewall.nft').read_text(), permissions='0600'))
    return dict(files=files, enable=[(unit, 'multi-user.target') for unit in (
        'claude-gateway.service', 'claude-environment-update.service', 'claude-setup.service')]
        + [('claude-desktop-ready.service', 'graphical.target')])


def image_tool(cfg):
    return str(Path(cfg['qemu_executable']).with_name('qemu-img.exe' if os.name == 'nt' else 'qemu-img'))


def image_command(cfg, *args):
    result = subprocess.run([image_tool(cfg), *args], check=True, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=120,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return result.stdout


def updated_seed(cfg, directory):
    import pycdlib
    from windows.backend import seed_iso
    metadata = io.BytesIO()
    iso = pycdlib.PyCdlib()
    iso.open(cfg['seed'])
    try:
        # Preserve the instance ID so an initialized guest cannot accidentally
        # rerun first-login bootstrap if cloud-init gets reenabled later.
        iso.get_file_from_iso_fp(metadata, rr_path='/meta-data')
    finally:
        iso.close()
    folder = directory / 'updated-seed-files'
    folder.mkdir()
    (folder / 'meta-data').write_bytes(metadata.getvalue())
    (folder / 'user-data').write_text(environment.cloud_config(cfg), encoding='utf-8', newline='\n')
    result = directory / 'updated-seed.iso'
    seed_iso(folder, result)
    return result


def recover(data, cfg):
    from windows.backend import write_config, emit
    journal = data / 'guest-update-transaction.json'
    if not journal.exists():
        return
    transaction = json.loads(journal.read_text(encoding='utf-8'))
    if Path(transaction['disk']).resolve() != Path(cfg['disk']).resolve():
        raise RuntimeError('Незавершённое обновление относится к другому диску. Автоматический запуск отменён.')
    if cfg.get('guest_revision') == transaction['revision'] and cfg.get('guest_update_snapshot') == transaction['snapshot']:
        journal.unlink()
        return  # Configuration committed; never undo subsequent user changes.
    info = json.loads(image_command(cfg, 'info', '--output=json', cfg['disk']))
    if any(item['name'] == transaction['snapshot'] for item in info.get('snapshots', [])):
        emit('Восстанавливаю диск после незавершённого обновления…')
        image_command(cfg, 'snapshot', '-a', transaction['snapshot'], cfg['disk'])
    elif transaction['phase'] != 'snapshot-pending':
        raise RuntimeError('Не найден снимок незавершённого обновления. Запуск остановлен для сохранения данных.')
    original = transaction['configuration']
    if transaction.get('seed_backup'):
        backup = Path(transaction['seed_backup'])
        if not backup.is_file():
            raise RuntimeError('Не найдена резервная копия ISO незавершённого обновления.')
        replacement = Path(original['seed']).with_suffix('.restore.iso')
        replacement.write_bytes(backup.read_bytes())
        os.replace(replacement, original['seed'])
    # Keep the current runtime paths after reinstalling the controller.
    original.update({key: cfg[key] for key in ('qemu_executable', 'qemu_data_dir') if key in cfg})
    write_config(data / 'environment.json', original)
    cfg.clear()
    cfg.update(original)
    journal.unlink()


def run_helper(command, cfg, log, ready):
    """Attach the old disk only AFTER the helper has mounted its own root.

    Ubuntu copies share root UUIDs/labels. Attaching both disks at firmware
    startup can boot the user's OS instead of the maintenance OS.
    """
    job, process = None, None
    replies = queue.Queue()
    def receive():
        try:
            for line in process.stdout:
                replies.put(json.loads(line))
        except (OSError, ValueError) as error:
            replies.put(error)
        finally:
            replies.put(None)
    def reply(identifier=None):
        deadline = time.monotonic() + 30
        while True:
            try:
                item = replies.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty as error:
                raise RuntimeError('Maintenance QMP did not respond') from error
            if item is None or isinstance(item, Exception):
                raise RuntimeError('Maintenance QMP disconnected')
            if (identifier is None and 'QMP' in item) or (identifier is not None and item.get('id') == identifier):
                if 'error' in item:
                    raise RuntimeError('Maintenance QMP: ' + str(item['error']))
                return item
            if time.monotonic() >= deadline:
                raise RuntimeError('Maintenance QMP response deadline exceeded')
    def request(name, arguments=None):
        message = dict(execute=name, id=name)
        if arguments is not None:
            message['arguments'] = arguments
        process.stdin.write((json.dumps(message) + '\n').encode('utf-8'))
        process.stdin.flush()
        return reply(name)
    reader = None
    try:
        if os.name == 'nt':
            from windows.job import Job
            job = Job()
        with (log.parent / 'qemu.log').open('wb') as output:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=output,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if job:
                job.assign(process)
            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            reply()
            request('qmp_capabilities')
            deadline = time.monotonic() + 1200
            attached = False
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, 1200)
                if not attached and log.exists() and ready in log.read_text(encoding='utf-8', errors='replace'):
                    request('blockdev-add', dict(driver='qcow2', **{'node-name': 'update-target'},
                        file=dict(driver='file', filename=str(Path(cfg['disk']).resolve()))))
                    request('device_add', dict(driver='virtio-blk-pci', drive='update-target',
                                              id='update-disk', bus='update-port'))
                    attached = True
                time.sleep(.2)
            if not attached:
                raise RuntimeError('Maintenance did not request the existing disk')
            return process.returncode
    finally:
        if job:
            job.close()
        if process:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
            if process.stdin:
                process.stdin.close()
            if reader:
                reader.join(timeout=5)
            process.stdout.close()


def maintenance(cfg, base, directory, content):
    from windows.backend import seed_iso, maintenance_memory
    nonce = uuid.uuid4().hex
    marker = 'CLAUDE-ISOLATION: offline-update-complete ' + nonce
    ready = 'CLAUDE-ISOLATION: maintenance-awaiting-disk ' + nonce
    seed_files = directory / 'seed-files'
    seed_files.mkdir()
    # An offline helper has no NIC, host mounts, gateway, clipboard, or account
    # credentials. It mounts ONLY the old guest root identified on /dev/vdb.
    script = ('#!/bin/sh\nset -eu\n'
              'finish() { sync; umount /target 2>/dev/null || true; systemctl --no-block poweroff; }; trap finish EXIT\n'
              'count=0; target=; while [ -z "$target" ]; do\n'
              '  for part in /dev/vdb[0-9]*; do\n'
              '    [ -b "$part" ] || continue\n'
              '    if [ "$(blkid -s LABEL -o value "$part")" = cloudimg-rootfs ]; then\n'
              '      test -z "$target"; target="$part"\n'
              '    fi\n'
              '  done\n'
              '  count=$((count+1)); test "$count" -lt 120; [ -n "$target" ] || sleep 1\n'
              'done\n'
              'test "$(blkid -s TYPE -o value "$target")" = ext4\n'
              'mkdir /target; mount -t ext4 -o nodev,nosuid,noexec "$target" /target\n'
              'python3 /offline-update.py /target /payload.json\n'
              'sync; umount /target\n'
              'echo "' + marker + '" > /dev/console\n')
    data = dict(hostname='isolated-maintenance', users=[], package_update=False,
                growpart=dict(mode='off'), resize_rootfs=False,
                cloud_init_modules=['write_files', 'bootcmd'], cloud_config_modules=['runcmd'],
                cloud_final_modules=['scripts-user'],
                bootcmd=[['python3', '/verify-helper-root.py', ready]],
                write_files=[dict(path='/verify-helper-root.py', content=(environment.ROOT / 'guest/helper-root.py').read_text(), permissions='0600'),
                             dict(path='/payload.json', content=json.dumps(content), permissions='0600'),
                             dict(path='/offline-update.py', content=(environment.ROOT / 'guest/offline-update.py').read_text(), permissions='0600'),
                             dict(path='/apply-update.sh', content=script, permissions='0700')],
                runcmd=[['sh', '/apply-update.sh']])
    (seed_files / 'user-data').write_text('#cloud-config\n' + json.dumps(data), encoding='utf-8', newline='\n')
    (seed_files / 'meta-data').write_text('instance-id: maintenance-' + nonce + '\n', encoding='utf-8')
    (seed_files / 'network-config').write_text('version: 2\nethernets: {}\n', encoding='utf-8')
    seed, helper, log = directory / 'seed.iso', directory / 'helper.qcow2', directory / 'boot.log'
    seed_iso(seed_files, seed)
    image_command(cfg, 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(base.resolve()), str(helper))
    command = [cfg['qemu_executable'], '-nodefaults', '-machine', 'q35',
               '-accel', 'tcg,thread=multi', '-cpu', 'max', '-m', str(maintenance_memory()), '-smp', '2',
               '-display', 'none', '-monitor', 'none', '-serial', 'file:' + environment.qemu_path(log),
               '-nic', 'none', '-no-reboot',
               '-qmp', 'stdio', '-device', 'pcie-root-port,id=update-port,chassis=1,slot=1',
               '-drive', 'file=' + environment.qemu_path(helper) + ',if=virtio,format=qcow2',
               '-device', 'virtio-scsi-pci,id=seed-controller',
               '-drive', 'file=' + environment.qemu_path(seed) + ',if=none,id=seed,format=raw,readonly=on',
               '-device', 'scsi-cd,drive=seed,bus=seed-controller.0']
    if cfg.get('qemu_data_dir'):
        command += ['-L', cfg['qemu_data_dir']]
    try:
        result = run_helper(command, cfg, log, ready)
        if result or marker not in log.read_text(encoding='utf-8', errors='replace'):
            raise RuntimeError('Обновление Linux не завершилось. Проверьте guest-update-boot.log; исходный диск будет восстановлен.')
    finally:
        for source, target in ((log, 'guest-update-boot.log'), (directory / 'qemu.log', 'guest-update-qemu.log')):
            if source.is_file():
                (directory.parent / target).write_bytes(source.read_bytes())


def upgrade(data, cfg):
    from windows.backend import emit, find_tool, write_config, GUEST_GATEWAY_VERSION
    disk = Path(cfg['disk']).resolve()
    with exclusive(disk.with_suffix('.launch.lock')):
        recover(data, cfg)
        if cfg.get('guest_revision') == REVISION and cfg.get('guest_gateway_version') == GUEST_GATEWAY_VERSION:
            return
        emit('Обновляю существующий Linux-диск. Файлы и настройки сохраняются…')
        # Saved memory would not match the updated disk.
        from windows import state
        state.discard(cfg)
        info = json.loads(image_command(cfg, 'info', '--output=json', str(disk)))
        if info.get('format') != 'qcow2' or info.get('backing-filename'):
            raise RuntimeError('Автоматическое обновление требует отдельного диска qcow2 без backing-файла.')
        base, digest = release_image.download(data / 'downloads', 'x86_64')
        if not ubuntu_image.matches(base, digest):
            raise RuntimeError('Release maintenance image checksum mismatch')
        snapshot = 'before-update-' + REVISION + '-' + uuid.uuid4().hex
        seed_backup = data / (snapshot + '.iso')
        with seed_backup.open('xb') as output:
            output.write(Path(cfg['seed']).read_bytes())
            output.flush()
            os.fsync(output.fileno())
        journal = data / 'guest-update-transaction.json'
        transaction = dict(disk=str(disk), snapshot=snapshot, revision=REVISION,
                           phase='snapshot-pending', configuration=dict(cfg), seed_backup=str(seed_backup))
        write_config(journal, transaction)
        try:
            image_command(cfg, 'snapshot', '-c', snapshot, str(disk))
            transaction['phase'] = 'updating'
            write_config(journal, transaction)
            with tempfile.TemporaryDirectory(prefix='.guest-update-', dir=data) as temporary:
                directory = Path(temporary)
                seed = updated_seed(cfg, directory)
                maintenance(cfg, base, directory, payload(cfg))
                os.replace(seed, cfg['seed'])
            updated = dict(cfg, guest_revision=REVISION, guest_gateway_version=GUEST_GATEWAY_VERSION,
                           guest_update_snapshot=snapshot, guest_update_seed_backup=str(seed_backup))
            write_config(data / 'environment.json', updated)
            cfg.update(updated)
            journal.unlink()
        except BaseException:
            recover(data, cfg)
            raise
        emit('Образ обновлён. Claude и Firefox обновятся автоматически при следующем запуске Linux через защищённую сеть.')
