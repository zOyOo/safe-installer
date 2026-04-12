#!/usr/bin/env bash
# safe-pip installer
# Usage:
#   bash install.sh               # install safe-pip command only
#   bash install.sh --pip-wrapper # also replace pip / pip3 commands
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[safe-pip]${NC} $*"; }
warn()  { echo -e "${YELLOW}[safe-pip]${NC} $*"; }
error() { echo -e "${RED}[safe-pip]${NC} $*" >&2; exit 1; }

WRAP_PIP=false
for arg in "$@"; do [[ "$arg" == "--pip-wrapper" ]] && WRAP_PIP=true; done

# ─── Prerequisites ────────────────────────────────────────────────────────────

command -v python3 &>/dev/null || error "python3 not found."
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" \
  || error "Python >= 3.9 required (found $(python3 --version))."
python3 -c "from packaging.version import Version" 2>/dev/null \
  || python3 -c "from pip._vendor.packaging.version import Version" 2>/dev/null \
  || error "'packaging' not found even in pip vendor. Is pip installed?"

info "Using Python: $(python3 --version)"

# ─── Install directory ────────────────────────────────────────────────────────

if [[ "$(id -u)" == "0" ]]; then
  INSTALL_DIR="/usr/local/lib/safe-pip"
  BIN_DIR="/usr/local/bin"
else
  INSTALL_DIR="$HOME/.safe-pip"
  BIN_DIR="$HOME/.local/bin"
  mkdir -p "$BIN_DIR"
fi

info "Installing to: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# ─── Copy scripts ─────────────────────────────────────────────────────────────

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
cp "$SCRIPT_DIR/safe-pip.py"      "$INSTALL_DIR/safe-pip.py"
cp "$SCRIPT_DIR/usercustomize.py" "$INSTALL_DIR/usercustomize.py"
chmod +x "$INSTALL_DIR/safe-pip.py"

# ─── Create safe-pip binary ───────────────────────────────────────────────────

# Use plain "python3" — let PATH resolve it (works with pyenv, system Python, etc.)
cat > "$BIN_DIR/safe-pip" << WRAPPER
#!/bin/sh
exec python3 "$INSTALL_DIR/safe-pip.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/safe-pip"
info "Created: $BIN_DIR/safe-pip"

# ─── Optionally wrap pip / pip3 ───────────────────────────────────────────────

if [[ "$WRAP_PIP" == "true" ]]; then
  # Note: safe-pip.py internally calls `sys.executable -m pip`, NOT the `pip`
  # command, so there is no recursion even when pip → safe-pip.
  for cmd in pip pip3; do
    cat > "$BIN_DIR/$cmd" << PIPWRAP
#!/bin/sh
exec "$BIN_DIR/safe-pip" "\$@"
PIPWRAP
    chmod +x "$BIN_DIR/$cmd"
    info "Created: $BIN_DIR/$cmd → safe-pip"
  done
fi

echo ""
info "Installation complete!"
echo ""

# ─── usercustomize.py for current Python ─────────────────────────────────────

_install_usercustomize() {
  local python_bin="$1"
  local user_site
  user_site=$("$python_bin" -m site --user-site 2>/dev/null) || return
  [[ -n "$user_site" ]] || return
  mkdir -p "$user_site"
  cp "$INSTALL_DIR/usercustomize.py" "$user_site/usercustomize.py"
  info "Installed usercustomize.py → $user_site"
}

_install_usercustomize python3

# Also install for all existing pyenv versions that have pip
if command -v pyenv &>/dev/null; then
  PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
  for ver_dir in "$PYENV_ROOT/versions"/*/; do
    ver_python="$ver_dir/bin/python3"
    [[ -x "$ver_python" ]] || continue
    "$ver_python" -c "import pip" 2>/dev/null || continue
    _install_usercustomize "$ver_python"
  done
fi

# ─── pyenv after-install hook ─────────────────────────────────────────────────

if command -v pyenv &>/dev/null; then
  PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
  HOOK_DIR="$PYENV_ROOT/plugins/safe-pip/etc/pyenv.d/install"
  mkdir -p "$HOOK_DIR"
  cp "$SCRIPT_DIR/pyenv_hook.bash" "$HOOK_DIR/after.bash"
  chmod +x "$HOOK_DIR/after.bash"
  info "Registered pyenv hook → $HOOK_DIR/after.bash"
  info "Future 'pyenv install X.Y.Z' will auto-configure safe-pip."
fi

# ─── PATH check ───────────────────────────────────────────────────────────────

if echo "$PATH" | tr ':' '\n' | grep -qxF "$BIN_DIR"; then
  info "$BIN_DIR is already in PATH — you're all set."
else
  warn "$BIN_DIR is not in PATH. Add it to your shell config:"
  echo ""
  echo "  # bash / zsh:"
  echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc"
  echo ""
  echo "  # fish:"
  echo "  fish_add_path $BIN_DIR"
  echo ""
fi

if [[ "$WRAP_PIP" == "true" ]]; then
  info "pip and pip3 now point to safe-pip."
  echo ""
  echo "  Note: safe-pip internally uses 'python -m pip' to call the real pip,"
  echo "  so it works correctly with pyenv — no recursion, no version confusion."
else
  echo "  To also replace pip / pip3, re-run with: bash install.sh --pip-wrapper"
  echo ""
fi
