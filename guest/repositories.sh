#!/bin/sh
# Shared by guest installation and the dependency scanning image.
set -eu
if [ -n "${CLAUDE_REPOSITORY_PROXY:-}" ]; then
  export https_proxy="$CLAUDE_REPOSITORY_PROXY" http_proxy="$CLAUDE_REPOSITORY_PROXY"
fi
curl --retry 5 --retry-all-errors --fail --silent --show-error \
  https://downloads.claude.ai/claude-desktop/key.asc \
  -o /usr/share/keyrings/claude-desktop-archive-keyring.asc
fingerprint=$(gpg --show-keys --with-colons /usr/share/keyrings/claude-desktop-archive-keyring.asc | awk -F: '$1=="fpr" {print $10; exit}')
test "$fingerprint" = '31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE'
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/claude-desktop-archive-keyring.asc] https://downloads.claude.ai/claude-desktop/apt/stable stable main' > /etc/apt/sources.list.d/claude-desktop.list
install -d -m 0755 /etc/apt/keyrings
curl --retry 5 --retry-all-errors --fail --silent --show-error \
  https://packages.mozilla.org/apt/repo-signing-key.gpg -o /etc/apt/keyrings/packages.mozilla.org.asc
fingerprint=$(gpg --show-keys --with-colons /etc/apt/keyrings/packages.mozilla.org.asc | awk -F: '$1=="fpr" {print $10; exit}')
test "$fingerprint" = '35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3'
printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main' > /etc/apt/sources.list.d/mozilla.list
# Enable Ubuntu universe for xclip (clipboard bridge dependency).
# Ubuntu 24.04 uses deb822 format (.sources file) or legacy format (.list).
if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
  if ! grep -q 'Components:.*universe' /etc/apt/sources.list.d/ubuntu.sources; then
    sed -i '/^Components:/ s/main$/main universe/' /etc/apt/sources.list.d/ubuntu.sources
  fi
elif [ -f /etc/apt/sources.list ]; then
  if ! grep -q ' universe' /etc/apt/sources.list; then
    sed -i '0,/^deb /s//deb /; s/ main$/ main universe/' /etc/apt/sources.list
  fi
fi
apt-get update -qq 2>/dev/null || true
cat > /etc/apt/preferences.d/mozilla <<'EOF'
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000

Package: firefox
Pin: release o=Ubuntu
Pin-Priority: -1
EOF
