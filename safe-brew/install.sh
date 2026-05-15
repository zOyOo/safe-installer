#!/usr/bin/env bash
# safe-brew installer
# Usage:
#   bash install.sh                # install safe-brew command only
#   bash install.sh --brew-wrapper # also replace the `brew` command
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[safe-brew]${NC} $*"; }
warn()  { echo -e "${YELLOW}[safe-brew]${NC} $*"; }
error() { echo -e "${RED}[safe-brew]${NC} $*" >&2; exit 1; }

WRAP_BREW=false
for arg in "$@"; do [[ "$arg" == "--brew-wrapper" ]] && WRAP_BREW=true; done

# ─── Prerequisites ────────────────────────────────────────────────────────────

command -v python3 &>/dev/null || error "python3 not found."
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" \
  || error "Python >= 3.9 required (found $(python3 --version))."

# Find the real brew binary (before we potentially shadow it)
REAL_BREW=""
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  if [[ -x "$candidate" ]]; then
    REAL_BREW="$candidate"
    break
  fi
done
# If brew is already a safe-brew wrapper, unwrap it
if [[ -z "$REAL_BREW" ]]; then
  REAL_BREW=$(command -v brew 2>/dev/null) || error "brew not found. Install Homebrew first."
fi
if grep -q 'SAFE_BREW_REAL' "$REAL_BREW" 2>/dev/null; then
  REAL_BREW=$(grep -o 'SAFE_BREW_REAL="[^"]*"' "$REAL_BREW" | cut -d'"' -f2)
fi
[[ -x "$REAL_BREW" ]] || error "Could not locate real brew binary."
info "Using brew at: $REAL_BREW"

info "Using Python: $(python3 --version)"

# ─── Install directory ────────────────────────────────────────────────────────

if [[ "$(id -u)" == "0" ]]; then
  INSTALL_DIR="/usr/local/lib/safe-brew"
  BIN_DIR="/usr/local/bin"
else
  INSTALL_DIR="$HOME/.safe-brew"
  BIN_DIR="$HOME/.local/bin"
fi
mkdir -p "$BIN_DIR"

info "Installing to: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# ─── Copy scripts ─────────────────────────────────────────────────────────────

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
cp "$SCRIPT_DIR/safe-brew.py" "$INSTALL_DIR/safe-brew.py"
chmod +x "$INSTALL_DIR/safe-brew.py"

# ─── Create safe-brew binary ──────────────────────────────────────────────────

cat > "$BIN_DIR/safe-brew" << WRAPPER
#!/bin/sh
exec python3 "$INSTALL_DIR/safe-brew.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/safe-brew"
info "Created: $BIN_DIR/safe-brew"

# ─── Optionally wrap brew ─────────────────────────────────────────────────────

if [[ "$WRAP_BREW" == "true" ]]; then
  # Guard: if REAL_BREW lives at the same path we'd write the wrapper to
  # (root + Intel Mac: both are /usr/local/bin/brew), we cannot safely wrap
  # brew in place — Homebrew's launcher is location-dependent and breaks when
  # copied out of its prefix tree.
  # Solution: place ~/.local/bin (or your chosen BIN_DIR) earlier in PATH
  # than /usr/local/bin so the safe-brew wrapper shadows the real brew without
  # touching /usr/local/bin/brew at all. Re-run without --brew-wrapper and
  # add BIN_DIR to PATH manually.
  if [[ "$REAL_BREW" == "$BIN_DIR/brew" ]]; then
    error "--brew-wrapper: cannot wrap brew in place at $REAL_BREW (Homebrew's launcher is location-dependent and would break if overwritten).
       Fix: ensure $BIN_DIR appears before $(dirname "$REAL_BREW") in PATH,
       then re-run: bash install.sh --brew-wrapper"
  fi

  cat > "$BIN_DIR/brew" << BREWWRAP
#!/bin/sh
exec env SAFE_BREW_REAL="$REAL_BREW" "$BIN_DIR/safe-brew" "\$@"
BREWWRAP
  chmod +x "$BIN_DIR/brew"
  info "Created: $BIN_DIR/brew → safe-brew (real brew: $REAL_BREW)"
fi

echo ""
info "Installation complete!"
echo ""

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

if [[ "$WRAP_BREW" == "true" ]]; then
  warn "For the brew wrapper to shadow Homebrew's brew, $BIN_DIR must appear"
  warn "BEFORE /opt/homebrew/bin (or /usr/local/bin) in your PATH."
  echo ""
  info "brew is now aliased to safe-brew. The real brew is: $REAL_BREW"
  echo ""
  echo "  Note: safe-brew calls the real brew directly via SAFE_BREW_REAL,"
  echo "  so there is no recursion even when brew → safe-brew."
else
  echo "  To also replace 'brew', re-run with: bash install.sh --brew-wrapper"
  echo ""
fi

# ─── GitHub token setup ───────────────────────────────────────────────────────

echo ""
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  info "GITHUB_TOKEN is already set in your environment — skipping token setup."
else
  echo "  safe-brew uses the GitHub API to check formula commit history."
  echo "  Without a token you get 60 requests/hour (often enough for casual use)."
  echo "  With a token you get 5000 requests/hour."
  echo ""
  echo "  Generate a token (no scopes needed for public repos):"
  echo "  https://github.com/settings/tokens/new"
  echo ""
  printf "  Enter your GitHub token (or press Enter to skip): "
  read -r INPUT_TOKEN

  if [[ -z "$INPUT_TOKEN" ]]; then
    warn "Skipped. You can set GITHUB_TOKEN later and re-run install.sh."
  else
    # Validate the token against the GitHub API
    printf "  Validating token... "
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
      -H "Authorization: token $INPUT_TOKEN" \
      -H "User-Agent: safe-brew-installer" \
      https://api.github.com/user)

    if [[ "$HTTP_CODE" == "200" ]]; then
      echo "OK"

      # Write to shell configs that exist
      TOKEN_LINE="export GITHUB_TOKEN=\"$INPUT_TOKEN\""
      WROTE_ANY=false

      for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc"; do
        if [[ -f "$rc" ]]; then
          if grep -q 'GITHUB_TOKEN' "$rc" 2>/dev/null; then
            warn "$rc already contains GITHUB_TOKEN — skipping (update it manually if needed)."
          else
            printf '\n# Added by safe-brew installer\n%s\n' "$TOKEN_LINE" >> "$rc"
            info "Written to $rc"
            WROTE_ANY=true
          fi
        fi
      done

      # fish: use universal variables (persists across sessions without editing files)
      if command -v fish &>/dev/null; then
        fish -c "set -Ux GITHUB_TOKEN '$INPUT_TOKEN'" 2>/dev/null \
          && { info "Set as fish universal variable (GITHUB_TOKEN)"; WROTE_ANY=true; } \
          || warn "Could not set fish universal variable — set it manually."
      fi

      if [[ "$WROTE_ANY" == "false" ]]; then
        warn "No shell config files found. Add this line manually:"
        echo ""
        echo "    $TOKEN_LINE"
      fi

      info "GitHub token configured. Reload your shell or run: source ~/.bashrc"
    elif [[ "$HTTP_CODE" == "401" ]]; then
      echo "FAILED"
      warn "Token is invalid or expired (HTTP 401). Not saved."
      warn "Generate a new one at: https://github.com/settings/tokens"
    else
      echo "FAILED (HTTP $HTTP_CODE)"
      warn "Could not validate token. Check your network connection."
      warn "You can set GITHUB_TOKEN manually after installation."
    fi
  fi
fi
echo ""
