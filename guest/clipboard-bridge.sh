#!/bin/sh
# Guest-side clipboard bridge: syncs text clipboard with the host via a
# virtio-serial port. Images are transferred as base64-encoded PNG.
# Exits cleanly if the port is not available (e.g. CI without -virtfs).
PORT=/dev/virtio-ports/claude-clipboard
[ -c "$PORT" ] || exit 0
export DISPLAY=:0
export XAUTHORITY=/home/claude/.Xauthority
LAST_HASH=$(mktemp)
trap 'rm -f "$LAST_HASH"' EXIT

while true; do
  # Read host clipboard -> set guest clipboard
  if read -r -t 1 line < "$PORT" 2>/dev/null; then
    case "$line" in
      TEXT:*) printf '%s' "${line#TEXT:}" | xclip -selection clipboard 2>/dev/null || true ;;
      IMAGE_PNG:*)
        echo "${line#IMAGE_PNG:}" | base64 -d > /tmp/claude-clip.png 2>/dev/null
        xclip -selection clipboard -t image/png < /tmp/claude-clip.png 2>/dev/null || true
        rm -f /tmp/claude-clip.png
        ;;
    esac
  fi
  # Read guest clipboard -> send to host
  CURRENT=$(xclip -selection clipboard -o 2>/dev/null | sha256sum | cut -d' ' -f1 || true)
  if [ -n "$CURRENT" ] && [ "$CURRENT" != "$(cat "$LAST_HASH" 2>/dev/null)" ]; then
    echo "$CURRENT" > "$LAST_HASH"
    CONTENT=$(xclip -selection clipboard -o 2>/dev/null || true)
    printf 'TEXT:%s\n' "$CONTENT" > "$PORT" 2>/dev/null || true
  fi
  sleep 0.5
done
