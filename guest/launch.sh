#!/bin/sh
set -eu
test -f /var/lib/claude-isolation-ready || exit 1
export HTTP_PROXY=http://10.0.2.100:7890
export HTTPS_PROXY="$HTTP_PROXY"
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTP_PROXY"
export NO_PROXY=localhost,127.0.0.1
export no_proxy="$NO_PROXY"
# No flag disables Chromium's sandbox. Child processes that ignore the
# proxy cannot reach the Internet because of the hypervisor and firewall.
exec claude-desktop --force-device-scale-factor=1.5 --password-store=basic --proxy-server="$HTTP_PROXY" "$@"
