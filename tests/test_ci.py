import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import configure
import sarif_gate
import environment


class PackagingTests(unittest.TestCase):
    def test_graphical_readiness_survives_disabled_cloud_init(self):
        cloud = json.loads(environment.cloud_config().split('\n', 1)[1])
        files = {item['path']: item['content'] for item in cloud['write_files']}
        service = files['/etc/systemd/system/claude-desktop-ready.service']
        self.assertIn('WantedBy=graphical.target', service)
        self.assertIn('Restart=on-failure', service)
        self.assertIn('ExecStart=/usr/local/sbin/claude-desktop-ready', service)
        self.assertEqual(files['/usr/local/sbin/claude-desktop-ready'],
                         (ROOT / 'guest/desktop-ready.sh').read_text())
        bootstrap = files['/usr/local/sbin/bootstrap-claude']
        self.assertIn('enable --now claude-desktop-ready.service', bootstrap)
        self.assertNotIn("echo 'CLAUDE-ISOLATION: desktop-ready'", bootstrap)

    def test_sca_inventory_matches_guest_installed_packages(self):
        text = (ROOT / 'guest/bootstrap.sh').read_text()
        self.assertIn('CLAUDE_REPOSITORY_PROXY=http://10.0.2.100:7890 /usr/local/sbin/claude-repositories', text)
        installed = set()
        for line in text.splitlines():
            if line.startswith('apt-get ') and ' install -y --no-install-recommends ' in line:
                installed.update(line.split(' install -y --no-install-recommends ', 1)[1].split())
        inventory = set((ROOT / 'guest/packages.txt').read_text().split())
        self.assertEqual(installed, inventory)
        cloud = json.loads(environment.cloud_config().split('\n', 1)[1])
        files = {item['path']: item['content'] for item in cloud['write_files']}
        self.assertEqual(files['/usr/local/sbin/claude-repositories'],
                         (ROOT / 'guest/repositories.sh').read_text())

    def test_x86_config_has_no_mac_firmware_path(self):
        for system in ('Windows', 'Linux', 'Darwin'):
            cfg = configure.config(system, 'AMD64', Path('/data'))
            self.assertEqual(cfg['arch'], 'x86_64')
            self.assertNotIn('firmware', cfg)

    def test_linux_arm_requires_real_firmware(self):
        with patch.object(Path, 'is_file', return_value=False):
            with self.assertRaises(RuntimeError):
                configure.config('Linux', 'aarch64', Path('/data'))

    def test_sarif_gate_fails_for_missing_report(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                sarif_gate.findings(d)

    def test_sarif_gate_blocks_medium_security_even_when_level_warning(self):
        with tempfile.TemporaryDirectory() as d:
            report = {'runs': [{'tool': {'driver': {'rules': [
                {'id': 'unsafe', 'properties': {'security-severity': '5.0'}}]}},
                'results': [{'ruleId': 'unsafe', 'level': 'warning', 'message': {'text': 'unsafe'}}]}]}
            Path(d, 'scan.sarif').write_text(json.dumps(report))
            self.assertEqual(len(sarif_gate.findings(d)), 1)
            report['runs'][0]['results'] = []
            Path(d, 'scan.sarif').write_text(json.dumps(report))
            self.assertEqual(sarif_gate.findings(d), [])
