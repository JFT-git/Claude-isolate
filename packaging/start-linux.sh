#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f environment.json ]; then python3 scripts/configure.py; fi
if [ ! -f state/desktop.qcow2 ]; then python3 scripts/prepare.py; fi
exec python3 environment.py start
