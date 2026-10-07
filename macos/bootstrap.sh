#!/bin/bash
# Native entry point: works even before Python/QEMU have been installed.
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ENV_HINTS=1
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
action=${1:-status}
case $(uname -m) in
  arm64) brew_prefix=/opt/homebrew; guest_arch=aarch64 ;;
  x86_64) brew_prefix=/usr/local; guest_arch=x86_64 ;;
  *) echo '{"message":"Архитектура Mac не поддерживается","error":true}'; exit 1 ;;
esac
python="$brew_prefix/opt/python@3.12/bin/python3.12"
if [[ "$action" == start || "$action" == restart || "$action" == prepare ]]; then
  if [[ ! -x "$brew_prefix/bin/brew" ]]; then
    echo '{"message":"Устанавливаю компоненты. macOS может запросить пароль администратора."}'
    staging=$(mktemp -d "${TMPDIR:-/private/tmp}/claude-setup.XXXXXX")
    trap 'rm -rf -- "$staging"' EXIT
    /usr/bin/curl --fail --location --proto '=https' --proto-redir '=https' --retry 2 \
      https://github.com/Homebrew/brew/releases/latest/download/Homebrew.pkg -o "$staging/Homebrew.pkg"
    /usr/sbin/pkgutil --check-signature "$staging/Homebrew.pkg"
    /usr/sbin/spctl --assess --type install "$staging/Homebrew.pkg"
    /usr/bin/osascript "$script_dir/install-package.applescript" "$staging/Homebrew.pkg"
    rm -rf -- "$staging"
    trap - EXIT
  fi
  missing=()
  [[ -x "$python" ]] || missing+=(python@3.12)
  [[ -x "$brew_prefix/bin/qemu-system-$guest_arch" && -x "$brew_prefix/bin/qemu-img" ]] || missing+=(qemu)
  if (( ${#missing[@]} )); then
    echo '{"message":"Устанавливаю необходимые компоненты — это может занять несколько минут"}'
    "$brew_prefix/bin/brew" install "${missing[@]}"
  fi
fi
if [[ ! -x "$python" ]]; then
  echo '{"message":"Компоненты установятся автоматически при запуске","ready":false,"running":false,"needs_setup":true}'
  exit 0
fi
exec "$python" "$script_dir/backend.py" "$@"
