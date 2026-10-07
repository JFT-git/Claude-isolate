#!/usr/bin/env python3
"""Signal hotplug only after the maintenance root is a partition of vda."""
import os
from pathlib import Path
import sys


def verify(root_device, sysfs=Path('/sys')):
    device = sysfs / 'dev/block' / f'{os.major(root_device)}:{os.minor(root_device)}'
    expected = sysfs / 'class/block/vda'
    if (not (device / 'partition').is_file()
            or device.resolve().parent != expected.resolve()):
        raise RuntimeError(f'Maintenance root is not a vda partition: {device}')


if __name__ == '__main__':
    verify(os.stat('/').st_dev)
    with open('/dev/console', 'w') as console:
        console.write(sys.argv[1] + '\n')
