"""Turn on the Windows Hypervisor Platform so QEMU can use WHPX.

Without it QEMU emulates the processor in software, several times slower. Most
PCs have the hardware: Windows ships the component switched off, and turning
it on needs administrator rights and a restart. The installer asks for both.
"""
import ctypes
import json
import os
from pathlib import PureWindowsPath
import re
import subprocess

FEATURE = 'HypervisorPlatform'
# Exit codes of `Claude Isolate Core.exe hypervisor-status` and
# `hypervisor-enable`; windows/setup.iss reads them.
READY = 0          # WHPX works now.
CAN_ENABLE = 10    # The hardware is ready; Windows lacks the component.
RESTART = 11       # Turned on; works after Windows restarts.
FIRMWARE = 12      # Virtualization is switched off in the BIOS/UEFI.
UNSUPPORTED = 13   # No SLAT, or an ARM64 PC: only software emulation.
FAILED = 14        # Turning it on failed; see the log.
CODES = {READY: 'ready', CAN_ENABLE: 'can-enable', RESTART: 'restart', FIRMWARE: 'firmware',
         UNSUPPORTED: 'unsupported', FAILED: 'failed'}
# DISM: success, and success that needs a restart.
DISM_OK = (0, 3010)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
PROBE = ("$c = Get-CimInstance Win32_ComputerSystem; "
         "$p = Get-CimInstance Win32_Processor | Select-Object -First 1; "
         "@{hypervisor = [bool]$c.HypervisorPresent; firmware = [bool]$p.VirtualizationFirmwareEnabled; "
         "slat = [bool]$p.SecondLevelAddressTranslationExtensions} | ConvertTo-Json -Compress")


def system_tool(*parts):
    # Core is a 64-bit process, so System32 is not redirected to SysWOW64.
    return str(PureWindowsPath(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', *parts))


def hardware():
    """What Windows reports about the processor and a running hypervisor."""
    result = subprocess.run([system_tool('WindowsPowerShell', 'v1.0', 'powershell.exe'),
                             '-NoProfile', '-NonInteractive', '-Command', PROBE],
                            capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Не удалось узнать параметры процессора: ' + (result.stderr or result.stdout).strip()[-500:])
    return json.loads(result.stdout)


def status(whpx_available, machine, read_hardware=hardware):
    if whpx_available:
        return READY
    if machine.lower() in ('arm64', 'aarch64'):
        return UNSUPPORTED
    info = read_hardware()
    # While a hypervisor runs, Windows itself is its guest and no longer sees
    # the processor's virtualization. Only the component is missing then.
    if info.get('hypervisor'):
        return CAN_ENABLE
    if not info.get('firmware'):
        return FIRMWARE
    if not info.get('slat'):
        return UNSUPPORTED
    return CAN_ENABLE


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def run(command, log):
    result = subprocess.run(command, capture_output=True, text=True, errors='replace',
                            timeout=1800, creationflags=NO_WINDOW)
    log('$ ' + ' '.join(command) + '\n' + result.stdout + result.stderr + f'exit {result.returncode}')
    return result


def enable(log=print, runner=run):
    """Turn the component on and let Windows start its hypervisor at boot."""
    if not is_admin():
        log('Нужны права администратора')
        return FAILED
    result = runner([system_tool('dism.exe'), '/online', '/enable-feature', '/featurename:' + FEATURE,
                     '/all', '/norestart', '/quiet'], log)
    if result.returncode not in DISM_OK:
        return FAILED
    # Some programs set hypervisorlaunchtype to Off; then the component is on
    # but no hypervisor starts. The setting name and its values are not localized.
    boot = runner([system_tool('bcdedit.exe'), '/enum', '{current}'], log)
    if boot.returncode == 0 and re.search(r'^hypervisorlaunchtype\s+off\s*$', boot.stdout, re.I | re.M):
        if runner([system_tool('bcdedit.exe'), '/set', '{current}', 'hypervisorlaunchtype', 'auto'],
                  log).returncode:
            return FAILED
    return RESTART
