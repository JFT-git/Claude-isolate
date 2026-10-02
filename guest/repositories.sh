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
cat > /etc/apt/preferences.d/mozilla <<'EOF'
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000

Package: firefox
Pin: release o=Ubuntu
Pin-Priority: -1
EOF
