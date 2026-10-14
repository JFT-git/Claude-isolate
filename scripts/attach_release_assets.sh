#!/bin/sh
# Attach files to a GitHub release publish has already created.
# Never creates, edits, drafts, or deletes that release.
set -eu

if [ "$#" -lt 2 ]; then
  echo 'usage: attach_release_assets.sh <tag> <file>...' >&2
  exit 2
fi

tag=$1
shift
wait_seconds=${ATTACH_WAIT_SECONDS:-900}
interval=${ATTACH_INTERVAL_SECONDS:-30}
deadline=$(( $(date +%s) + wait_seconds ))

while :; do
  if state=$(gh release view "$tag" --json isDraft --jq .isDraft 2>/dev/null) && [ "$state" = "false" ]; then
    if gh release upload "$tag" "$@" --clobber; then
      exit 0
    fi
    echo "Upload to $tag failed; retrying" >&2
  else
    echo "Release $tag is not published yet; retrying" >&2
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Release $tag was not ready for assets" >&2
    exit 1
  fi
  sleep "$interval"
done
