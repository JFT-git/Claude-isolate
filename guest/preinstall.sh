#!/bin/sh
# CI only: run in a fresh vendor image, never on a user's disk.
set -eu
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
# All dependencies were fetched through signed APT repositories in the CI
# container. The appliance has no NIC; missing packages must fail the build.
# APT's --no-download also disables acquisition of explicitly supplied local
# files. Remove remote indexes instead: only these local archives can satisfy
# dependencies, and virt-customize additionally runs with --no-network.
rm -rf /var/lib/apt/lists/*
apt-get install -y --no-install-recommends /tmp/claude-debs/*.deb
# Keep the verified repository keys and Firefox pin for subsequent updates.
cp -a /tmp/claude-debs/repository-config/. /
set --
for pkg in gnome-keyring gnome-keyring-pkcs11 libpam-gnome-keyring light-locker light-locker-settings; do
    if [ "$(dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null || true)" = installed ]; then
        set -- "$@" "$pkg"
    fi
done
# An empty index cannot resolve names of packages absent from the image.
if [ "$#" -gt 0 ]; then apt-get purge -y "$@"; fi
while IFS= read -r pkg; do
    [ -z "$pkg" ] || test "$(dpkg-query -W -f='${db:Status-Status}' "$pkg")" = installed
done < /tmp/claude-packages.txt
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /etc/claude-preinstalled-packages.tsv
# Leave cloud-init and user creation for the first local boot. No profiles or credentials.
touch /etc/claude-preinstalled
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/claude-debs /tmp/claude-repositories.sh /tmp/claude-packages.txt
systemctl mask lightdm.service ssh.service ssh.socket apt-daily.timer apt-daily-upgrade.timer
