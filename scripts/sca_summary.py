#!/usr/bin/env python3
"""Make Trivy's dependency findings visible in Actions summaries and annotations."""
import json
import os
from pathlib import Path
import sys


def escape(value):
    return str(value).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def summarize(report):
    findings = []
    for result in report.get('Results', []):
        for item in result.get('Vulnerabilities', []):
            if item.get('Severity') in ('HIGH', 'CRITICAL'):
                findings.append(item)
    lines = ['## Guest dependency vulnerabilities', '',
             f'Found {len(findings)} HIGH/CRITICAL findings. Automatic release remains blocked if any are present.', '',
             '| Package | Vulnerability | Severity | Installed | Fix available |',
             '| --- | --- | --- | --- | --- |']
    for i, item in enumerate(findings):
        values = [item.get(key, '') for key in ('PkgName', 'VulnerabilityID', 'Severity', 'InstalledVersion', 'FixedVersion')]
        values[-1] = values[-1] or 'No version reported'
        lines.append('| ' + ' | '.join(str(v).replace('|', '\\|').replace('\n', ' ') for v in values) + ' |')
        if i < 30:
            print('::warning::' + escape(' / '.join(str(v) for v in values)))
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    path = Path(sys.argv[1])
    if not path.is_file():
        raise SystemExit('No vulnerability report generated; inspect the scanner/build failure.')
    summary = summarize(json.loads(path.read_text()))
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as output:
            output.write(summary)
