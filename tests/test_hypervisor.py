import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windows import backend, hypervisor


def completed(code=0, stdout=''):
    return subprocess.CompletedProcess([], code, stdout, '')


class StatusTests(unittest.TestCase):
    def test_states(self):
        cases = [
            (True, 'AMD64', None, hypervisor.READY),
            (False, 'ARM64', None, hypervisor.UNSUPPORTED),
            (False, 'AMD64', dict(hypervisor=False, firmware=True, slat=True), hypervisor.CAN_ENABLE),
            # The test laptop: Windows 11 Pro with VT-x on and the component off.
            (False, 'AMD64', dict(hypervisor=False, firmware=False, slat=True), hypervisor.FIRMWARE),
            (False, 'AMD64', dict(hypervisor=False, firmware=True, slat=False), hypervisor.UNSUPPORTED),
            # Hyper-V or memory integrity runs a hypervisor that hides VT-x
            # from Windows; only the platform component is missing.
            (False, 'AMD64', dict(hypervisor=True, firmware=False, slat=False), hypervisor.CAN_ENABLE),
        ]
        for whpx, machine, info, expected in cases:
            with self.subTest(whpx=whpx, machine=machine, info=info):
                def read():
                    if info is None:
                        raise AssertionError('Hardware is not queried when the answer is known')
                    return info
                self.assertEqual(hypervisor.status(whpx, machine, read), expected)


class EnableTests(unittest.TestCase):
    def run_enable(self, results, admin=True):
        commands = []

        def runner(command, log):
            commands.append(command)
            return results.pop(0)
        with patch.object(hypervisor, 'is_admin', return_value=admin):
            code = hypervisor.enable(log=lambda text: None, runner=runner)
        return code, commands

    def test_turns_on_the_component_without_restarting(self):
        code, commands = self.run_enable([completed(3010), completed(0, 'hypervisorlaunchtype    Auto\n')])
        self.assertEqual(code, hypervisor.RESTART)
        dism = commands[0]
        self.assertTrue(dism[0].lower().endswith(r'system32\dism.exe'))
        for argument in ('/online', '/enable-feature', '/featurename:HypervisorPlatform', '/norestart'):
            self.assertIn(argument, dism)
        self.assertEqual(len(commands), 2)

    def test_restores_a_hypervisor_launch_type_set_to_off(self):
        code, commands = self.run_enable([completed(0), completed(0, 'description   Windows\r\nhypervisorlaunchtype    Off\r\n'),
                                          completed(0)])
        self.assertEqual(code, hypervisor.RESTART)
        self.assertEqual(commands[2][1:], ['/set', '{current}', 'hypervisorlaunchtype', 'auto'])

    def test_failures(self):
        self.assertEqual(self.run_enable([], admin=False), (hypervisor.FAILED, []))
        code, commands = self.run_enable([completed(87)])
        self.assertEqual((code, len(commands)), (hypervisor.FAILED, 1))
        code, _ = self.run_enable([completed(0), completed(0, 'hypervisorlaunchtype Off\n'), completed(1)])
        self.assertEqual(code, hypervisor.FAILED)


class CoreTests(unittest.TestCase):
    def test_core_actions_exit_with_the_codes_the_installer_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            for action, target, code in (('hypervisor-status', 'status', hypervisor.CAN_ENABLE),
                                         ('hypervisor-enable', 'enable', hypervisor.RESTART)):
                with self.subTest(action=action), \
                     patch.object(sys, 'argv', ['core', action, '--data', temporary]), \
                     patch.object(hypervisor, target, return_value=code), \
                     patch.object(backend, 'acceleration', return_value='tcg'), \
                     patch.object(backend, 'emit') as emit:
                    with self.assertRaises(SystemExit) as caught:
                        backend.main()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(emit.call_args.kwargs['hypervisor'], hypervisor.CODES[code])
            # Neither action creates or touches an environment.
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_installer_uses_the_same_exit_codes(self):
        script = (ROOT / 'windows/setup.iss').read_text(encoding='utf-8-sig')
        for name, value in (('HypervisorCanEnable', hypervisor.CAN_ENABLE),
                            ('HypervisorRestart', hypervisor.RESTART),
                            ('HypervisorFirmware', hypervisor.FIRMWARE)):
            self.assertRegex(script, r'\b' + name + r' = ' + str(value) + ';')
        # Only the enable step is elevated; the installer stays per-user.
        self.assertIn('PrivilegesRequired=lowest', script)
        self.assertEqual(len(re.findall(r"ShellExec\('runas'", script)), 1)
        self.assertIn("ShellExec('runas', CoreExecutable, 'hypervisor-enable", script)


if __name__ == '__main__':
    unittest.main()
