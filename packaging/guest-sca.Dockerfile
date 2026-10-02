# Inventory image, not the VM disk and never distributed as the application.
# Resolve current signed packages on each run so weekly SCA detects new advisories.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
COPY guest/repositories.sh /tmp/repositories.sh
COPY guest/packages.txt /tmp/packages.txt
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && sh /tmp/repositories.sh && apt-get update \
    && xargs -r apt-get install -y --no-install-recommends < /tmp/packages.txt \
    && apt-get purge -y gnome-keyring gnome-keyring-pkcs11 libpam-gnome-keyring light-locker light-locker-settings \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /guest-packages.tsv \
    && rm -rf /var/lib/apt/lists/* /tmp/repositories.sh /tmp/packages.txt
