#!/usr/bin/env bash
# safe-pip uninstaller
# Usage: bash uninstall.sh
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[safe-pip]${NC} $*"; }
warn()  { echo -e "${YELLOW}[safe-pip]${NC} $*"; }

if [[ "$(id -u)" == "0" ]]; then
  INSTALL_DIR="/usr/local/lib/safe-pip"
  BIN_DIR="/usr/local/bin"
else
  INSTALL_DIR="$HOME/.safe-pip"
  BIN_DIR="$HOME/.local/bin"
fi

# ─── Remove usercustomize.py from all Python versions ─────────────────────────

_remove_usercustomize() {
  local python_bin="$1"
  local user_site
  user_site=$("$python_bin" -m site --user-site 2>/dev/null) || return
  local target="$user_site/usercustomize.py"
  if [[ -f "$target" ]] && grep -q 'safe-pip' "$target" 2>/dev/null; then
    rm -f "$target"
    info "Removed usercustomize.py ← $user_site"
  fi
}

command -v python3 &>/dev/null && _remove_usercustomize python3

if command -v pyenv &>/dev/null; then
  PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
  for ver_dir in "$PYENV_ROOT/versions"/*/; do
    ver_python="$ver_dir/bin/python3"
    [[ -x "$ver_python" ]] && _remove_usercustomize "$ver_python"
  done

  # Remove pyenv hook
  HOOK_FILE="$PYENV_ROOT/plugins/safe-pip/etc/pyenv.d/install/after.bash"
  if [[ -f "$HOOK_FILE" ]]; then
    rm -f "$HOOK_FILE"
    # Clean up empty dirs
    rmdir "$PYENV_ROOT/plugins/safe-pip/etc/pyenv.d/install" 2>/dev/null || true
    rmdir "$PYENV_ROOT/plugins/safe-pip/etc/pyenv.d"         2>/dev/null || true
    rmdir "$PYENV_ROOT/plugins/safe-pip/etc"                 2>/dev/null || true
    rmdir "$PYENV_ROOT/plugins/safe-pip"                     2>/dev/null || true
    info "Removed pyenv hook"
  fi
fi

# ─── Remove wrapper binaries ──────────────────────────────────────────────────

for cmd in safe-pip pip pip3; do
  f="$BIN_DIR/$cmd"
  if [[ -f "$f" ]] && grep -q 'safe-pip' "$f" 2>/dev/null; then
    rm -f "$f"
    info "Removed $f"
  fi
done

# ─── Remove install directory ─────────────────────────────────────────────────

if [[ -d "$INSTALL_DIR" ]]; then
  rm -rf "$INSTALL_DIR"
  info "Removed $INSTALL_DIR"
fi

echo ""
info "Uninstall complete. safe-pip has been fully removed."
