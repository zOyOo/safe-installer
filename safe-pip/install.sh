#!/usr/bin/env bash
# safe-pip installer
# Usage:
#   bash install.sh                  # install safe-pip and wrap pip/pip3 (default)
#   bash install.sh --no-pip-wrapper # install safe-pip only, leave pip/pip3 untouched
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[safe-pip]${NC} $*"; }
warn()  { echo -e "${YELLOW}[safe-pip]${NC} $*"; }
error() { echo -e "${RED}[safe-pip]${NC} $*" >&2; exit 1; }

WRAP_PIP=true
VENV_HOOK=false
for arg in "$@"; do
  [[ "$arg" == "--no-pip-wrapper" ]] && WRAP_PIP=false
  [[ "$arg" == "--venv-hook"      ]] && VENV_HOOK=true
done

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

  local target="$user_site/usercustomize.py"
  if [[ -f "$target" ]] && ! grep -q "safe-pip usercustomize hook" "$target" 2>/dev/null; then
    # An existing, non-safe-pip usercustomize.py is present.
    # Back it up and warn — do NOT silently overwrite it.
    local backup="$user_site/usercustomize_pre_safepip.py"
    cp "$target" "$backup"
    cp "$INSTALL_DIR/usercustomize.py" "$target"
    warn "Existing usercustomize.py backed up → $backup"
    warn "If you had custom startup code there, merge it back manually."
    info "Installed usercustomize.py → $user_site"
  else
    cp "$INSTALL_DIR/usercustomize.py" "$target"
    info "Installed usercustomize.py → $user_site"
  fi
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

# ─── Venv hook (--venv-hook) ──────────────────────────────────────────────────

_install_venv_hook() {
  # ── Fish shell ──────────────────────────────────────────────────────────────
  local fish_fn_dir="$HOME/.config/fish/functions"
  if command -v fish &>/dev/null || [[ -d "$fish_fn_dir" ]]; then
    mkdir -p "$fish_fn_dir"

    # pip / pip3: when inside a venv, route through safe-pip.
    # Outside a venv the --pip-wrapper binary (or system pip) is used as usual.
    for cmd in pip pip3; do
      cat > "$fish_fn_dir/$cmd.fish" << FISHFN
function $cmd --wraps=$cmd --description 'safe-pip venv wrapper'
    if set -q VIRTUAL_ENV; and test -f ~/.safe-pip/safe-pip.py
        python ~/.safe-pip/safe-pip.py \$argv
    else
        command $cmd \$argv
    end
end
FISHFN
      info "Installed fish venv hook: $fish_fn_dir/$cmd.fish"
    done

    # safe-venv: create a venv then immediately inject safe-pip into it.
    cat > "$fish_fn_dir/safe-venv.fish" << 'SAFEVENV'
function safe-venv --wraps='python -m venv' --description 'Create venv and inject safe-pip'
    python -m venv $argv
    or return $status
    # Last positional arg (non-flag) is the venv directory
    set -l venv_dir
    for arg in $argv
        if not string match -q -- '-*' $arg
            set venv_dir $arg
        end
    end
    if test -n "$venv_dir"; and test -d "$venv_dir"
        safe-pip inject-venv "$venv_dir"
    end
end
SAFEVENV
    info "Installed fish function: $fish_fn_dir/safe-venv.fish"
  fi

  # ── Bash / Zsh ──────────────────────────────────────────────────────────────
  local _snippet
  _snippet='
# safe-pip venv hook — added by safe-pip install.sh
_safe_pip_venv_wrap() {
  local _cmd="$1"; shift
  if [ -n "$VIRTUAL_ENV" ] && [ -f "$HOME/.safe-pip/safe-pip.py" ]; then
    python "$HOME/.safe-pip/safe-pip.py" "$@"
  else
    command "$_cmd" "$@"
  fi
}
pip()  { _safe_pip_venv_wrap pip  "$@"; }
pip3() { _safe_pip_venv_wrap pip3 "$@"; }
safe-venv() {
  python -m venv "$@" || return $?
  local d
  for a in "$@"; do [[ "$a" != -* ]] && d="$a"; done
  [[ -n "$d" && -d "$d" ]] && safe-pip inject-venv "$d"
}'

  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [[ -f "$rc" ]] && ! grep -q "safe-pip venv hook" "$rc" 2>/dev/null; then
      printf '%s\n' "$_snippet" >> "$rc"
      info "Installed venv hook → $rc"
    fi
  done
}

if [[ "$VENV_HOOK" == "true" ]]; then
  _install_venv_hook
fi

# ─── PATH check ───────────────────────────────────────────────────────────────

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

if [[ "$WRAP_PIP" == "true" ]]; then
  info "pip and pip3 now point to safe-pip."
  echo ""
  echo "  Note: safe-pip internally uses 'python -m pip' to call the real pip,"
  echo "  so it works correctly with pyenv — no recursion, no version confusion."
else
  echo "  To install without wrapping pip / pip3, re-run with: bash install.sh --no-pip-wrapper"
  echo ""
fi

if [[ "$VENV_HOOK" == "true" ]]; then
  info "Venv hooks installed."
  echo ""
  echo "  • pip / pip3 inside any activated venv → routed through safe-pip"
  echo "  • safe-venv <dir>  — creates a venv and auto-injects safe-pip into it"
  echo "  • safe-pip inject-venv <dir>  — inject into an existing venv manually"
  echo ""
else
  echo "  To also protect pip inside virtual envs, re-run with: bash install.sh --venv-hook"
  echo ""
fi
