#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f environment.json ]; then python3 scripts/configure.py; fi
python3 scripts/prepare.py --if-needed
exec python3 environment.py start
