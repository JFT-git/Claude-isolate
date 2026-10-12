#!/usr/bin/env bash
# Sync this tree to laptop B and run scripts/local-ci-windows.ps1.
# The Windows script runs as scheduled task ClaudeIsolateLocalCI and writes
# C:\ClaudeIsolateTest\agent\local-ci.log. This script only polls that log,
# so a dropped SSH session does not kill the guest boot.
# Prints LOCAL CI PASS or LOCAL CI FAIL. Does not push to GitHub.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAPTOP_HOST="${LAPTOP_HOST:-192.168.60.107}"
REMOTE="Vladislav Gubin@${LAPTOP_HOST}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=25 -o ConnectionAttempts=3
  -o ServerAliveInterval=15 -o ServerAliveCountMax=6 -o TCPKeepAlive=yes
  -o NumberOfPasswordPrompts=0 -o StrictHostKeyChecking=accept-new)
SCP=(scp -o BatchMode=yes -o ConnectTimeout=25 -o ConnectionAttempts=3
  -o ServerAliveInterval=15 -o ServerAliveCountMax=40
  -o NumberOfPasswordPrompts=0 -o StrictHostKeyChecking=accept-new)
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
ARCHIVE="$(mktemp /tmp/claude-isolate-sync.XXXXXX).tar"
trap 'rm -f "$ARCHIVE"' EXIT

retry() {
  local attempt=1
  until "$@"; do
    if [[ $attempt -ge 5 ]]; then
      echo "giving up after ${attempt} attempts: $*" >&2
      return 1
    fi
    echo "retry ${attempt}: $*" >&2
    attempt=$((attempt + 1))
    sleep 8
  done
}

echo "Syncing tree to ${REMOTE}..."
retry "${SSH[@]}" "$REMOTE" cmd /c "if not exist C:\\ClaudeIsolateTest\\build\\Claude-isolate mkdir C:\\ClaudeIsolateTest\\build\\Claude-isolate"
EXCLUDES=(--exclude .git --exclude __pycache__ --exclude .venv --exclude build
  --exclude dist/windows --exclude reports --exclude '*.pyc')
LOCAL_IMG="$ROOT/dist/guest/Claude-isolate-guest-x86_64.qcow2"
if [[ -f "$LOCAL_IMG" ]]; then
  LOCAL_SHA="$(shasum -a 256 "$LOCAL_IMG" | awk '{print $1}')"
  HASH_PS="\$ProgressPreference='SilentlyContinue'; if (Test-Path -LiteralPath 'C:/ClaudeIsolateTest/build/Claude-isolate/dist/guest/Claude-isolate-guest-x86_64.qcow2') { (Get-FileHash -Algorithm SHA256 -LiteralPath 'C:/ClaudeIsolateTest/build/Claude-isolate/dist/guest/Claude-isolate-guest-x86_64.qcow2').Hash.ToLower() } else { 'missing' }"
  HASH_B64="$(printf '%s' "$HASH_PS" | python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read().decode().encode("utf-16le")).decode())')"
  REMOTE_SHA="$(retry "${SSH[@]}" "$REMOTE" powershell -NoProfile -EncodedCommand "$HASH_B64" || true)"
  REMOTE_SHA="$(printf '%s' "$REMOTE_SHA" | tr -d '[:space:]' | tr 'A-F' 'a-f')"
  if [[ "$REMOTE_SHA" == "$LOCAL_SHA" ]]; then
    echo "Guest image already matches; not uploading it again."
    EXCLUDES+=(--exclude 'dist/guest/*.qcow2')
  else
    echo "Guest image will be uploaded (remote hash ${REMOTE_SHA:-unknown})."
  fi
fi
COPYFILE_DISABLE=1 tar -C "$ROOT" -cf "$ARCHIVE" "${EXCLUDES[@]}" .
retry "${SCP[@]}" "$ARCHIVE" "$REMOTE:C:/ClaudeIsolateTest/build/sync.tar"
retry "${SSH[@]}" "$REMOTE" "tar -xf C:/ClaudeIsolateTest/build/sync.tar -C C:/ClaudeIsolateTest/build/Claude-isolate"
retry "${SSH[@]}" "$REMOTE" "del C:\\ClaudeIsolateTest\\build\\sync.tar"

echo "Starting local Windows CI as a scheduled task..."
retry "${SSH[@]}" "$REMOTE" powershell -NoProfile -ExecutionPolicy Bypass -File C:/ClaudeIsolateTest/build/Claude-isolate/scripts/start-local-ci-task.ps1 -GitCommit "$COMMIT"

echo "Waiting for the laptop transcript..."
set +e
rc=2
for _ in $(seq 1 720); do
  sleep 30
  result="$("${SSH[@]}" "$REMOTE" powershell -NoProfile -ExecutionPolicy Bypass -File C:/ClaudeIsolateTest/build/Claude-isolate/scripts/local-ci-status.ps1 2>/dev/null || true)"
  result="$(printf '%s' "$result" | tr -d '\r')"
  echo "laptop CI: ${result:-unreachable}"
  if [[ "$result" == PASS* ]]; then
    rc=0
    break
  fi
  if [[ "$result" == FAIL* ]]; then
    rc=1
    break
  fi
done
set -e
"${SSH[@]}" "$REMOTE" powershell -NoProfile -Command "Get-Content -Tail 50 C:\\ClaudeIsolateTest\\agent\\local-ci.log" || true
if [[ $rc -eq 0 ]]; then
  echo 'LOCAL CI PASS'
else
  echo 'LOCAL CI FAIL'
fi
exit "$rc"
