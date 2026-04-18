#!/usr/bin/env bash
# safe-brew uninstaller
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[safe-brew]${NC} $*"; }
warn()  { echo -e "${YELLOW}[safe-brew]${NC} $*"; }

if [[ "$(id -u)" == "0" ]]; then
  INSTALL_DIR="/usr/local/lib/safe-brew"
  BIN_DIR="/usr/local/bin"
else
  INSTALL_DIR="$HOME/.safe-brew"
  BIN_DIR="$HOME/.local/bin"
fi

# ─── Remove wrapper binaries ──────────────────────────────────────────────────

for cmd in safe-brew brew; do
  f="$BIN_DIR/$cmd"
  if [[ -f "$f" ]] && grep -q 'safe-brew\|SAFE_BREW' "$f" 2>/dev/null; then
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
info "Uninstall complete. safe-brew has been fully removed."
