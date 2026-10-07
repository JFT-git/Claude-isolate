#!/bin/sh
# CI only: run in a fresh vendor image, never on a user's disk.
set -eu
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
apt-get update
apt-get install -y --no-install-recommends curl gnupg ca-certificates
sh /tmp/claude-repositories.sh
apt-get update
xargs -r apt-get install -y --no-install-recommends < /tmp/claude-packages.txt
apt-get purge -y gnome-keyring gnome-keyring-pkcs11 libpam-gnome-keyring light-locker light-locker-settings
while IFS= read -r pkg; do
    [ -z "$pkg" ] || test "$(dpkg-query -W -f='${db:Status-Status}' "$pkg")" = installed
done < /tmp/claude-packages.txt
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /etc/claude-preinstalled-packages.tsv
# Leave cloud-init and user creation for the first local boot. No profiles or credentials.
touch /etc/claude-preinstalled
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/claude-repositories.sh /tmp/claude-packages.txt
systemctl mask ssh.service ssh.socket apt-daily.timer apt-daily-upgrade.timer
