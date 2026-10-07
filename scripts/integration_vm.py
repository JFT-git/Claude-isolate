#!/usr/bin/env python3
"""Opt-in destructive ONLY inside a fresh test directory: install and test a real VM.

No account login, user VM, host route, or VPN configuration is changed. Requires
QEMU, GnuPG, an ISO builder, and an approved exit using the normal network guard.
"""
import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import configure
import environment
import ubuntu_image

GUEST_TEST = r'''#!/bin/sh
set -eu
exec > /dev/console 2>&1
trap 'code=$?; echo "AUDIT: finished status=$code"; systemctl poweroff --no-block' EXIT
# xwininfo is a test-only dependency; it is not shipped in the desktop.
apt-get -o DPkg::Lock::Timeout=180 update
apt-get -o DPkg::Lock::Timeout=180 install -y --no-install-recommends x11-utils
sleep 20
test -f /var/lib/claude-isolation-ready
systemctl is-active lightdm
test "$(systemctl is-enabled ssh.socket)" = masked
test "$(systemctl is-enabled ssh.service)" = masked
for pkg in claude-desktop firefox xfce4-session; do
 dpkg-query -W -f='${Package} ${Version} ${db:Status-Status}\n' "$pkg"
done
! id -nG claude | grep -qw sudo
# Check that retries replace our firewall table without duplicating rules.
nft -f /etc/claude-isolation.nft
before=$(nft list table inet claude_isolation)
nft -f /etc/claude-isolation.nft
test "$before" = "$(nft list table inet claude_isolation)"
for url in https://1.1.1.1 http://10.0.2.2 http://169.254.169.254; do
 if curl --noproxy '*' -k --connect-timeout 2 --max-time 4 "$url" -o /dev/null; then
   echo 'AUDIT: direct network unexpectedly open'; exit 1
 fi
done
curl --fail --max-time 30 --proxy http://10.0.2.100:7890 https://example.com -o /dev/null
for url in http://127.0.0.1 http://169.254.169.254 https://10.0.2.2; do
 if curl --fail --noproxy '' --max-time 5 --proxy http://10.0.2.100:7890 "$url" -o /dev/null; then
   echo 'AUDIT: proxy reached private destination'; exit 1
 fi
done
# Both actual GUI applications must create a window in the installed session.
runuser -u claude -- env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority firefox --new-window about:blank > /tmp/firefox-smoke.log 2>&1 &
sleep 25
ps -u claude -o comm= | grep -i firefox
ps -u claude -o comm= | grep -i claude
runuser -u claude -- env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority xwininfo -root -tree > /tmp/windows
cat /tmp/windows
grep -i firefox /tmp/windows
grep -i claude /tmp/windows
runuser -u claude -- env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority setxkbmap -query | grep 'us,ru'
echo 'AUDIT: resource measurements'
free -m
df -h /
du -sh /var/cache/apt/archives
ps -u claude -o comm,rss --sort=-rss | head -15
echo 'AUDIT: PASS'
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True, help='New empty test directory')
    parser.add_argument('--base', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    data = args.data.resolve()
    data.mkdir(mode=0o700, parents=True, exist_ok=False)
    cfg = configure.config(platform.system(), platform.machine(), data)
    cfg['boot_log'] = str(data / 'boot.log')
    config_path = data / 'environment.json'
    config_path.write_text(json.dumps(cfg))
    original_cloud = environment.cloud_config
    def cloud(configuration=None):
        content = json.loads(original_cloud(configuration).split('\n', 1)[1])
        content['write_files'] += [
            dict(path='/usr/local/sbin/isolation-audit', owner='root:root', permissions='0700', content=GUEST_TEST),
            dict(path='/etc/systemd/system/isolation-audit.service', owner='root:root', permissions='0644', content='''[Unit]
Requires=claude-setup.service
After=claude-setup.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/isolation-audit
[Install]
WantedBy=multi-user.target
''')]
        content['runcmd'].append(['systemctl', 'enable', '--now', 'isolation-audit.service'])
        return '#cloud-config\n' + json.dumps(content) + '\n'
    environment.cloud_config = cloud
    if args.base:
        if not args.sha256:
            parser.error('--base requires a previously signature-verified --sha256')
        base, digest = args.base, args.sha256
    else:
        base, digest = ubuntu_image.download(data / 'downloads', cfg['arch'], environment.tool('gpg'))
    environment.prepare(cfg, base, digest)
    # Keep the real launcher/guard/relay; override only the display for automation.
    code = '''import sys;sys.path.insert(0,sys.argv[1]);import environment
original=environment.command
def headless(cfg,check=True):
 cmd=original(cfg,check)
 if '-display' in cmd: cmd[cmd.index('-display')+1]='none'
 else: cmd += ['-display','none']
 return cmd
environment.command=headless
sys.argv=['environment.py','start','--config',sys.argv[2]]
environment.main()
'''
    started = time.monotonic()
    with (data / 'launcher.log').open('w') as log:
        proc = subprocess.Popen([sys.executable, '-u', '-c', code, str(ROOT), str(config_path)], stdout=log, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=args.timeout)
        except BaseException:
            proc.terminate()
            proc.wait(timeout=20)
            raise
    boot = (data / 'boot.log').read_text(errors='replace') if (data / 'boot.log').exists() else ''
    result = dict(passed=proc.returncode == 0 and 'AUDIT: PASS' in boot and 'AUDIT: finished status=0' in boot,
                  seconds=round(time.monotonic() - started), memory_mb=cfg['memory_mb'], cpus=cfg['cpus'])
    (data / 'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    sys.exit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
