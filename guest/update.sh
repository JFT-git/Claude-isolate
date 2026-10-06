#!/bin/sh
# Update installed components without rerunning first-login customization.
set -eu
revision=$(cat /etc/claude-isolate/revision)
marker=/var/lib/claude-isolate/updated-$revision
install -d -m 700 /var/lib/claude-isolate
if [ "${1:-}" != '--installed' ]; then
    echo "CLAUDE-ISOLATION: update-started $revision" > /dev/console
    trap 'code=$?; if [ "$code" -ne 0 ]; then echo "CLAUDE-ISOLATION: update-retrying exit=$code" > /dev/console; fi' EXIT
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
    nft -f /etc/claude-isolation.nft
    CLAUDE_REPOSITORY_PROXY=http://10.0.2.100:7890 /usr/local/sbin/claude-repositories
    apt-get -o DPkg::Lock::Timeout=180 update
    apt-get -o DPkg::Lock::Timeout=180 -o Dpkg::Options::=--force-confdef \
        -o Dpkg::Options::=--force-confold install -y --no-install-recommends \
        claude-desktop firefox openssh-client nftables curl ca-certificates
    apt-get clean
fi
# Packages can replace their own policy files. Reapply the product's policy,
# leaving profile data and unrelated policies intact.
python3 - <<'PY'
import json
from pathlib import Path
path = Path('/usr/lib/firefox/distribution/policies.json')
data = json.loads(path.read_text()) if path.exists() else {}
policies = data.setdefault('policies', {})
policies['Proxy'] = dict(Mode='manual', Locked=True, HTTPProxy='10.0.2.100:7890',
                        SSLProxy='10.0.2.100:7890', UseHTTPProxyForAllProtocols=True,
                        Passthrough='localhost, 127.0.0.1')
policies['DNSOverHTTPS'] = dict(Enabled=False, Locked=True)
policies.setdefault('Preferences', {})['network.proxy.failover_direct'] = dict(Value=False, Status='locked')
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data) + '\n')
PY
touch "$marker"
echo "CLAUDE-ISOLATION: environment-updated $revision" > /dev/console
