#!/bin/sh
# Applied at installation, or while the desktop session is stopped.
set -eu
cat > /usr/local/bin/claude-display-setup <<'EOF'
#!/bin/sh
sleep 2
output=$(xrandr --query | awk '/ connected/ {print $1; exit}')
if [ -n "$output" ]; then
    xrandr --output "$output" --mode 1920x1200 ||
        xrandr --output "$output" --mode 1920x1080 ||
        xrandr --output "$output" --mode 1280x800 || true
fi
EOF
chmod 755 /usr/local/bin/claude-display-setup
install -d -o claude -g claude /home/claude/.config/autostart
cat > /home/claude/.config/autostart/claude-display.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Desktop display
Exec=/usr/local/bin/claude-display-setup
EOF
# Keep unrelated theme/preferences intact when updating an existing desktop.
python3 - <<'PY'
from pathlib import Path
import xml.etree.ElementTree as ET
base = Path('/home/claude/.config/xfce4/xfconf/xfce-perchannel-xml')
base.mkdir(parents=True, exist_ok=True)
def update(channel, properties):
    path = base / (channel + '.xml')
    root = ET.parse(path).getroot() if path.exists() else ET.Element('channel', name=channel, version='1.0')
    for parts, kind, value in properties:
        parent = root
        for part in parts.split('/'):
            child = next((p for p in parent if p.get('name') == part), None)
            if child is None:
                child = ET.SubElement(parent, 'property', name=part, type='empty')
            parent = child
        parent.set('type', kind)
        parent.set('value', value)
    ET.ElementTree(root).write(path, encoding='UTF-8', xml_declaration=True)
update('xsettings', [('Xft/DPI', 'int', '144'), ('Xft/Antialias', 'int', '1'),
                      ('Xft/Hinting', 'int', '1'), ('Xft/HintStyle', 'string', 'hintslight')])
PY
chown -R claude:claude /home/claude/.config
