#!/usr/bin/env python3
"""Fail the release gate on CodeQL security findings >= medium, without suppressions."""
import json
from pathlib import Path
import sys


def findings(root):
    reports = list(Path(root).rglob('*.sarif'))
    if not reports:
        raise RuntimeError('No SARIF produced; refusing to treat an absent scan as success')
    blocked = []
    for path in reports:
        for run in json.loads(path.read_text())['runs']:
            rules = {rule['id']: rule for rule in run['tool']['driver'].get('rules', [])}
            for result in run.get('results', []):
                rule = rules.get(result.get('ruleId'), {})
                score = float(rule.get('properties', {}).get('security-severity', 0))
                if score >= 4 or result.get('level') == 'error':
                    blocked.append(f'{path.name}: {result.get("ruleId")}: {result["message"].get("text", "")[:300]}')
    return blocked


if __name__ == '__main__':
    blocked = findings(sys.argv[1])
    for line in blocked:
        print(line)
    sys.exit(bool(blocked))
