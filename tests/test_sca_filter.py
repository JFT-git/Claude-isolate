from copy import deepcopy
from datetime import date
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from verify_sca_filter import verify


def report(**overrides):
    item = dict(VulnerabilityID='CVE-2022-25235', PkgName='firefox',
                InstalledVersion='157.0~build1', Severity='HIGH',
                PkgIdentifier={'PURL': 'pkg:deb/ubuntu/firefox@157.0~build1?arch=arm64&distro=ubuntu-24.04'})
    item.update(overrides)
    return {'Results': [{'Target': 'guest (ubuntu 24.04)', 'Type': 'ubuntu', 'Vulnerabilities': [item]}]}


class ReviewedFilterTests(unittest.TestCase):
    def test_exact_review_is_allowed_with_arch_qualifiers(self):
        for cve in ('CVE-2022-25235', 'CVE-2022-25236'):
            self.assertEqual(verify(report(VulnerabilityID=cve), {'Results': []}, date(2026, 10, 2)), 1)

    def test_missing_purl_cannot_expand_native_trivy_exception(self):
        for identifier in ({}, {'PURL': ''}):
            with self.assertRaises(ValueError):
                verify(report(PkgIdentifier=identifier), {'Results': []}, date(2026, 10, 2))

    def test_other_packages_versions_and_cves_stay_blocking(self):
        for changes in ({'PkgName': 'libexpat1'}, {'InstalledVersion': '158.0'},
                        {'InstalledVersion': '91.0'}, {'VulnerabilityID': 'CVE-2026-12345'},
                        {'PkgIdentifier': {'PURL': 'pkg:deb/ubuntu/libexpat1@157.0~build1'}}):
            with self.assertRaises(ValueError):
                verify(report(**changes), {'Results': []}, date(2026, 10, 2))

    def test_expired_review_cannot_remove_findings(self):
        with self.assertRaises(ValueError):
            verify(report(), {'Results': []}, date(2026, 11, 1))

    def test_unchanged_findings_are_not_exempted(self):
        original = report(PkgName='libexpat1')
        self.assertEqual(verify(original, deepcopy(original), date(2026, 10, 2)), 0)

    def test_absent_scan_results_fail_closed(self):
        with self.assertRaises(ValueError):
            verify({}, {'Results': []}, date(2026, 10, 2))
