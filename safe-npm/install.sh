#!/usr/bin/env bash
# safe-npm installer
# Usage:
#   bash install.sh
#   bash install.sh --npm-wrapper   # also replace `npm` with safe-npm
set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[safe-npm]${NC} $*"; }
warn()  { echo -e "${YELLOW}[safe-npm]${NC} $*"; }
error() { echo -e "${RED}[safe-npm]${NC} $*" >&2; exit 1; }

WRAP_NPM=false
for arg in "$@"; do [[ "$arg" == "--npm-wrapper" ]] && WRAP_NPM=true; done

# ─── Prerequisites ────────────────────────────────────────────────────────────

command -v node &>/dev/null || error "Node.js not found. Install Node.js >= 16 first."
node -e "if(parseInt(process.version.slice(1))<16)process.exit(1)" \
  || error "Node.js >= 16 required (found $(node --version))."

# Find the real npm binary (before we potentially replace it)
REAL_NPM=$(command -v npm 2>/dev/null) || error "npm not found."
# If npm is already a safe-npm wrapper, grab SAFE_NPM_REAL from it
if grep -q 'SAFE_NPM_REAL' "$REAL_NPM" 2>/dev/null; then
  REAL_NPM=$(grep -o 'SAFE_NPM_REAL="[^"]*"' "$REAL_NPM" | cut -d'"' -f2)
fi
[[ -x "$REAL_NPM" ]] || error "Could not locate real npm binary."
info "Using npm at: $REAL_NPM"

# Find the real npx binary
REAL_NPX=$(command -v npx 2>/dev/null) || true
if [[ -n "$REAL_NPX" ]] && grep -q 'SAFE_NPX_REAL' "$REAL_NPX" 2>/dev/null; then
  REAL_NPX=$(grep -o 'SAFE_NPX_REAL="[^"]*"' "$REAL_NPX" | cut -d'"' -f2)
fi
if [[ -n "$REAL_NPX" ]]; then
  info "Using npx at: $REAL_NPX"
fi

# ─── Install directory ────────────────────────────────────────────────────────

if [[ "$(id -u)" == "0" ]]; then
  INSTALL_DIR="/usr/local/lib/safe-npm"
  BIN_DIR="/usr/local/bin"
else
  INSTALL_DIR="$HOME/.safe-npm"
  BIN_DIR="$HOME/.local/bin"
  mkdir -p "$BIN_DIR"
fi

info "Installing to: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# ─── Copy source files ───────────────────────────────────────────────────────

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
cp "$SCRIPT_DIR/index.js" "$INSTALL_DIR/index.js"
cp "$SCRIPT_DIR/package.json" "$INSTALL_DIR/package.json"
chmod +x "$INSTALL_DIR/index.js"

# ─── Install semver dependency ────────────────────────────────────────────────

info "Installing semver dependency..."
"$REAL_NPM" install --prefix "$INSTALL_DIR" --save=false 2>/dev/null \
  || "$REAL_NPM" install --prefix "$INSTALL_DIR" 2>/dev/null

# ─── Create safe-npm binary ───────────────────────────────────────────────────

cat > "$BIN_DIR/safe-npm" << SAFENPM
#!/bin/sh
exec node "$INSTALL_DIR/index.js" "\$@"
SAFENPM
chmod +x "$BIN_DIR/safe-npm"
info "Created: $BIN_DIR/safe-npm"

# ─── Create safe-npx binary ───────────────────────────────────────────────────

cat > "$BIN_DIR/safe-npx" << SAFENPX
#!/bin/sh
exec env SAFE_NPX_MODE=1 node "$INSTALL_DIR/index.js" "\$@"
SAFENPX
chmod +x "$BIN_DIR/safe-npx"
info "Created: $BIN_DIR/safe-npx"

# ─── Optionally create npm / npx wrappers ─────────────────────────────────────

if [[ "$WRAP_NPM" == "true" ]]; then
  cat > "$BIN_DIR/npm" << NPMWRAP
#!/bin/sh
exec env SAFE_NPM_REAL="$REAL_NPM" "$BIN_DIR/safe-npm" "\$@"
NPMWRAP
  chmod +x "$BIN_DIR/npm"
  info "Created: $BIN_DIR/npm (wraps safe-npm)"

  if [[ -n "$REAL_NPX" && -x "$REAL_NPX" ]]; then
    cat > "$BIN_DIR/npx" << NPXWRAP
#!/bin/sh
exec env SAFE_NPX_REAL="$REAL_NPX" "$BIN_DIR/safe-npx" "\$@"
NPXWRAP
    chmod +x "$BIN_DIR/npx"
    info "Created: $BIN_DIR/npx (wraps safe-npx)"
  fi
fi

# ─── PATH reminder ────────────────────────────────────────────────────────────

echo ""
info "Installation complete!"
echo ""

# Check if BIN_DIR is already in PATH
if echo "$PATH" | tr ':' '\n' | grep -qxF "$BIN_DIR"; then
  info "$BIN_DIR is already in PATH — you're all set."
else
  warn "$BIN_DIR is not in PATH. Add it to your shell config:"
  echo ""
  echo "  # bash:"
  echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc"
  echo ""
  echo "  # zsh:"
  echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc"
  echo ""
  echo "  # fish:"
  echo "  fish_add_path $BIN_DIR"
  echo ""
fi

if [[ "$WRAP_NPM" == "true" ]]; then
  info "npm is now aliased to safe-npm."
  info "The real npm is: $REAL_NPM"
else
  echo "  To also replace 'npm', re-run with: bash install.sh --npm-wrapper"
  echo ""
fi
