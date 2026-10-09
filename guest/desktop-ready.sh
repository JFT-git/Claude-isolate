#!/bin/sh
set -eu
# Confirm an actual X session on every boot, without reading account data.
attempt=0
while [ "$attempt" -lt 150 ]; do
    if runuser -u claude -- env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority xrandr --current 2>/dev/null | grep -q ' connected'; then
        echo 'CLAUDE-ISOLATION: desktop-ready' > /dev/console
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
done
echo 'CLAUDE-ISOLATION: desktop-waiting' > /dev/console
exit 1
