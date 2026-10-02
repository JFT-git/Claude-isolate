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
        runs = json.loads(path.read_text())['runs']
        if not runs:
            raise RuntimeError('Empty SARIF scan')
        for run in runs:
            if any(i.get('executionSuccessful') is False for i in run.get('invocations', [])):
                raise RuntimeError('SARIF reports an unsuccessful scanner execution')
            components = [run['tool']['driver']] + run['tool'].get('extensions', [])
            rules = {rule['id']: rule for component in components for rule in component.get('rules', [])}
            for result in run.get('results', []):
                rule = rules.get(result.get('ruleId'), {})
                score = float(rule.get('properties', {}).get('security-severity', 0))
                level = result.get('level', rule.get('defaultConfiguration', {}).get('level', 'warning'))
                if score >= 4 or level == 'error' or not rule:
                    blocked.append(f'{path.name}: {result.get("ruleId")}: {result["message"].get("text", "")[:300]}')
    return blocked


if __name__ == '__main__':
    blocked = findings(sys.argv[1])
    for line in blocked:
        print(line)
    sys.exit(bool(blocked))
