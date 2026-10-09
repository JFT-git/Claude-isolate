#!/bin/sh
set -eu
# Confirm an actual X session on every boot, without reading account data.
attempt=0
while [ "$attempt" -lt 150 ]; do
    if runuser -u claude -- env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority xrandr --current 2>/dev/null | grep -q ' connected'; then
        echo 'CLAUDE-ISOLATION: desktop-ready' > /dev/console
        # Report when the new session has stopped loading: the hidden first-run
        # boot saves its state then. Bounded, and idle itself on every boot.
        quiet=0
        rounds=0
        while [ "$rounds" -lt 30 ] && [ "$quiet" -lt 2 ]; do
            idle=$(vmstat 3 2 2>/dev/null | awk 'END { print $15 }')
            if [ "${idle:-0}" -ge 90 ] 2>/dev/null; then quiet=$((quiet + 1)); else quiet=0; fi
            rounds=$((rounds + 1))
        done
        if [ "$quiet" -ge 2 ]; then echo 'CLAUDE-ISOLATION: session-idle' > /dev/console; fi
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
done
echo 'CLAUDE-ISOLATION: desktop-waiting' > /dev/console
exit 1
