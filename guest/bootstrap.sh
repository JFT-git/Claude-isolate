#!/bin/sh
set -eu
trap 'result=$?; if [ "$result" -ne 0 ]; then rm -f /var/lib/claude-isolation-ready; echo "CLAUDE-ISOLATION: setup-retrying exit=$result" > /dev/console; fi' EXIT
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
# QEMU denies direct networking even before the guest firewall is installed.
# A systemd service retries this idempotent setup after transient network errors.
systemctl mask lightdm.service
apt-get -o DPkg::Lock::Timeout=180 update
apt-get -o DPkg::Lock::Timeout=180 install -y --no-install-recommends xfce4-session xfce4-panel xfce4-settings xfwm4 xfdesktop4 thunar xfce4-terminal xfce4-xkb-plugin mousepad x11-xkb-utils fonts-dejavu-core xserver-xorg-core xserver-xorg-input-libinput xinit dbus-user-session lightdm dbus-x11 nftables curl gnupg ca-certificates xdg-utils openssh-client
nft -f /etc/claude-isolation.nft
systemctl enable nftables
install -m 600 /etc/claude-isolation.nft /etc/nftables.conf
CLAUDE_REPOSITORY_PROXY=http://10.0.2.100:7890 /usr/local/sbin/claude-repositories
apt-get -o DPkg::Lock::Timeout=180 update
apt-get -o DPkg::Lock::Timeout=180 install -y --no-install-recommends claude-desktop firefox
# No password prompt or Secret Service daemon inside this passwordless VM.
apt-get -o DPkg::Lock::Timeout=180 purge -y gnome-keyring gnome-keyring-pkcs11 libpam-gnome-keyring light-locker light-locker-settings
for pkg in claude-desktop firefox; do
  dpkg-query -W -f='CLAUDE-ISOLATION: installed ${Package} ${Version}\n' "$pkg" > /dev/console
done
# Firefox uses the same isolated gateway; disable direct fallback and DoH.
install -d /usr/lib/firefox/distribution
cat > /usr/lib/firefox/distribution/policies.json <<'EOF'
{"policies":{"Proxy":{"Mode":"manual","Locked":true,"HTTPProxy":"10.0.2.100:7890","SSLProxy":"10.0.2.100:7890","UseHTTPProxyForAllProtocols":true,"Passthrough":"localhost, 127.0.0.1"},"DNSOverHTTPS":{"Enabled":false,"Locked":true},"DontCheckDefaultBrowser":true,"OverrideFirstRunPage":"","OverridePostUpdatePage":"","Preferences":{"network.proxy.failover_direct":{"Value":false,"Status":"locked"},"browser.aboutwelcome.enabled":{"Value":false,"Status":"locked"}}}}
EOF
# Real guest smoke checks, without signing in to any account.
if curl --noproxy '*' --connect-timeout 2 --max-time 4 -Is https://downloads.claude.ai/claude-desktop/key.asc >/dev/null 2>&1; then
  echo 'CLAUDE-ISOLATION: FAILURE direct-network-open' > /dev/console
  exit 1
fi
curl --retry 3 --retry-all-errors --fail --silent --show-error --proxy http://10.0.2.100:7890 --connect-timeout 5 --max-time 20 https://downloads.claude.ai/claude-desktop/key.asc -o /dev/null
printf '%s\n' 'CLAUDE-ISOLATION: direct-denied proxy-working' > /dev/console
install -d -o claude -g claude /home/claude/.config /home/claude/.config/autostart /home/claude/Desktop
cat > /home/claude/.config/autostart/claude.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Claude Desktop
Exec=/usr/local/bin/claude-isolated
EOF
/usr/local/sbin/claude-display-install
# Desktop/menu launch paths all use the same proxy and password-store settings.
install -d /usr/local/share/applications
cat > /usr/local/share/applications/claude-desktop.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Claude Desktop
Exec=/usr/local/bin/claude-isolated %U
Icon=claude-desktop
Categories=Network;
MimeType=x-scheme-handler/claude;
EOF
cp /usr/local/share/applications/claude-desktop.desktop /home/claude/Desktop/Claude.desktop
cp /usr/share/applications/firefox.desktop /home/claude/Desktop/Firefox.desktop
chmod +x /home/claude/Desktop/*.desktop
chown -R claude:claude /home/claude/.config /home/claude/Desktop
# No first-login panel choice, lock screen or default-browser wizard.
install -d -o claude -g claude /home/claude/.config/xfce4/xfconf/xfce-perchannel-xml
cat > /home/claude/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-session" version="1.0"><property name="general" type="empty"><property name="LockCommand" type="string" value=""/><property name="SaveOnExit" type="bool" value="false"/></property><property name="shutdown" type="empty"><property name="LockScreen" type="bool" value="false"/></property></channel>
EOF
cat > /home/claude/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-power-manager" version="1.0"><property name="xfce4-power-manager" type="empty"><property name="lock-screen-suspend-hibernate" type="bool" value="false"/></property></channel>
EOF
# Windows-style keyboard switching and a visible layout indicator.
cat > /etc/default/keyboard <<'EOF'
XKBMODEL="pc105"
XKBLAYOUT="us,ru"
XKBVARIANT=","
XKBOPTIONS="grp:alt_shift_toggle"
BACKSPACE="guess"
EOF
install -d /etc/X11/xorg.conf.d
cat > /etc/X11/xorg.conf.d/00-keyboard.conf <<'EOF'
Section "InputClass"
    Identifier "isolated-keyboard"
    MatchIsKeyboard "on"
    Option "XkbModel" "pc105"
    Option "XkbLayout" "us,ru"
    Option "XkbOptions" "grp:alt_shift_toggle"
EndSection
EOF
cat > /home/claude/.config/xfce4/xfconf/xfce-perchannel-xml/keyboard-layout.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="keyboard-layout" version="1.0">
  <property name="Default" type="empty">
    <property name="XkbDisable" type="bool" value="false"/>
    <property name="XkbModel" type="string" value="pc105"/>
    <property name="XkbLayout" type="string" value="us,ru"/>
    <property name="XkbVariant" type="string" value=","/>
    <property name="XkbOptions" type="empty"><property name="Group" type="string" value="grp:alt_shift_toggle"/></property>
  </property>
</channel>
EOF
cat > /home/claude/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-panel" version="1.0">
  <property name="configver" type="int" value="2"/>
  <property name="panels" type="array"><value type="int" value="1"/>
    <property name="panel-1" type="empty">
      <property name="position" type="string" value="p=10;x=0;y=0"/>
      <property name="position-locked" type="bool" value="true"/>
      <property name="size" type="uint" value="36"/>
      <property name="length" type="uint" value="100"/>
      <property name="plugin-ids" type="array">
        <value type="int" value="1"/><value type="int" value="2"/>
        <value type="int" value="3"/><value type="int" value="4"/>
        <value type="int" value="5"/><value type="int" value="6"/>
      </property>
    </property>
  </property>
  <property name="plugins" type="empty">
    <property name="plugin-1" type="string" value="applicationsmenu"/>
    <property name="plugin-2" type="string" value="tasklist"><property name="expand" type="bool" value="true"/></property>
    <property name="plugin-3" type="string" value="separator"><property name="expand" type="bool" value="true"/><property name="style" type="uint" value="0"/></property>
    <property name="plugin-4" type="string" value="xkb"><property name="display-type" type="uint" value="1"/></property>
    <property name="plugin-5" type="string" value="clock"/>
    <property name="plugin-6" type="string" value="actions"/>
  </property>
</channel>
EOF
cat > /home/claude/Desktop/Read-me.txt <<'EOF'
Keyboard: English / Russian — Alt+Shift (Option+Shift on a Mac keyboard).
You can also click the language indicator on the bottom panel.
Full screen: View > Enter Full Screen in the virtual-machine window.
Claude and Firefox are installed automatically. Sign in to Claude with your account.
Firefox uses the isolated gateway. Public websites are available in system VPN mode.
EOF
chown claude:claude /home/claude/Desktop/Read-me.txt
chown -R claude:claude /home/claude/.config
runuser -u claude -- xdg-settings set default-web-browser firefox.desktop
# Log versions and absence of keyring, never account state or secrets.
if dpkg-query -W -f='${db:Status-Status}' gnome-keyring 2>/dev/null | grep -qx installed; then exit 1; fi
echo 'CLAUDE-ISOLATION: no-keyring' > /dev/console
# Reclaim installation archives and tell qcow2 which guest blocks are free.
apt-get clean
install -d /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=64M\nRuntimeMaxUse=32M\n' > /etc/systemd/journald.conf.d/50-isolated.conf
systemctl restart systemd-journald
fstrim -av || true
if [ -x /usr/local/sbin/claude-environment-update ]; then
  /usr/local/sbin/claude-environment-update --installed
fi
systemctl unmask lightdm.service
systemctl enable lightdm
systemctl restart lightdm
touch /etc/cloud/cloud-init.disabled
# Publish completion only after all persistent boot settings are installed
# and the display manager has started. A retry can outlive cloud-init's first
# systemctl job; consumers must never see an intermediate ready marker.
touch /var/lib/claude-isolation-ready
systemctl enable --now claude-desktop-ready.service
