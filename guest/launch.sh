#!/bin/sh
set -eu
test -f /var/lib/claude-isolation-ready || exit 1
export HTTP_PROXY=http://10.0.2.100:7890
export HTTPS_PROXY="$HTTP_PROXY"
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTP_PROXY"
export NO_PROXY=localhost,127.0.0.1
export no_proxy="$NO_PROXY"
# After an offline prepare the gateway is not up yet. One quick probe, then a
# short wait; when the host already permits access this returns immediately.
if ! curl --fail --silent --proxy "$HTTP_PROXY" --connect-timeout 2 --max-time 5 \
    https://downloads.claude.ai/claude-desktop/key.asc -o /dev/null; then
  waited=0
  until curl --fail --silent --proxy "$HTTP_PROXY" --connect-timeout 5 --max-time 20 \
      https://downloads.claude.ai/claude-desktop/key.asc -o /dev/null; do
    waited=$((waited + 3))
    if [ "$waited" -ge 120 ]; then break; fi
    sleep 3
  done
fi
# No flag disables Chromium's sandbox. Child processes that ignore the
# proxy cannot reach the Internet because of the hypervisor and firewall.
exec claude-desktop --force-device-scale-factor=1.5 --password-store=basic --proxy-server="$HTTP_PROXY" "$@"
