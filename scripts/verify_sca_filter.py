#!/usr/bin/env python3
"""Reject any filtering outside the exact reviewed Firefox findings."""
from collections import Counter
from datetime import date
import json
from pathlib import Path
import sys
from urllib.parse import unquote

REVIEW_IDS = {'CVE-2022-25235', 'CVE-2022-25236'}
REVIEW_PURL = 'pkg:deb/ubuntu/firefox@157.0~build1'
EXPIRES = date(2026, 11, 1)


def findings(report):
    if not isinstance(report.get('Results'), list):
        raise ValueError('Missing Results: scan evidence is incomplete')
    result = Counter()
    for entry in report['Results']:
        for item in entry.get('Vulnerabilities', []):
            if item.get('Severity') in ('HIGH', 'CRITICAL'):
                result[(entry.get('Target'), entry.get('Type'), item.get('VulnerabilityID'),
                        item.get('PkgName'), item.get('InstalledVersion'),
                        item.get('PkgIdentifier', {}).get('PURL', ''))] += 1
    return result


def verify(raw, filtered, today=None):
    removed = findings(raw) - findings(filtered)
    for (target, kind, cve, package, version, purl), count in removed.items():
        # Trivy accepts a missing target PURL when matching an ignore rule.
        # Independently reject that case and any broader removal here.
        if ((today or date.today()) >= EXPIRES or kind != 'ubuntu'
                or cve not in REVIEW_IDS or package != 'firefox'
                or version != '157.0~build1'
                or unquote(purl.split('?', 1)[0]) != REVIEW_PURL):
            raise ValueError(f'Unreviewed finding removed: {cve} / {package} / {version}')
    return sum(removed.values())


if __name__ == '__main__':
    raw, filtered = (json.loads(Path(p).read_text()) for p in sys.argv[1:3])
    print(f'Verified {verify(raw, filtered)} exact, unexpired Firefox review matches; raw evidence retained.')
