#!/usr/bin/env bash
# safe-npm uninstaller
# Usage: bash uninstall.sh
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[safe-npm]${NC} $*"; }
warn()  { echo -e "${YELLOW}[safe-npm]${NC} $*"; }

if [[ "$(id -u)" == "0" ]]; then
  INSTALL_DIR="/usr/local/lib/safe-npm"
  BIN_DIR="/usr/local/bin"
else
  INSTALL_DIR="$HOME/.safe-npm"
  BIN_DIR="$HOME/.local/bin"
fi

# ─── Remove wrapper binaries ──────────────────────────────────────────────────

for cmd in safe-npm safe-npx npm npx; do
  f="$BIN_DIR/$cmd"
  if [[ -f "$f" ]] && grep -qE 'safe-npm|safe-npx|SAFE_NPM_REAL|SAFE_NPX_REAL' "$f" 2>/dev/null; then
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
info "Uninstall complete. safe-npm has been fully removed."
warn "If you had npm/npx replaced, reinstall Node.js or ensure the real npm is in PATH."
