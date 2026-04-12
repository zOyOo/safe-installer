#!/usr/bin/env bash
# safe-pip pyenv after-install hook
# Placed at: $(pyenv root)/plugins/safe-pip/etc/pyenv.d/install/after.bash
#
# pyenv sources this file BEFORE installation begins.
# Use after_install 'code' to register code that runs AFTER the Python binary exists.

after_install '
  SAFE_PIP_SCRIPT="$HOME/.safe-pip/safe-pip.py"
  USERCUSTOMIZE_SRC="$HOME/.safe-pip/usercustomize.py"

  if [[ ! -f "$SAFE_PIP_SCRIPT" ]] || [[ ! -f "$USERCUSTOMIZE_SRC" ]]; then
    return 0
  fi

  # DEFINITION is the version string, e.g. "3.13.2"
  PYTHON_BIN="$(pyenv root)/versions/${DEFINITION}/bin/python3"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    return 0
  fi

  "$PYTHON_BIN" -c "import pip" 2>/dev/null || return 0

  echo "[safe-pip] Configuring for Python ${DEFINITION}..."

  USER_SITE=$("$PYTHON_BIN" -m site --user-site 2>/dev/null)
  if [[ -n "$USER_SITE" ]]; then
    mkdir -p "$USER_SITE"
    cp "$USERCUSTOMIZE_SRC" "$USER_SITE/usercustomize.py"
    echo "[safe-pip] ✓ python -m pip intercepted for Python ${DEFINITION}"
  fi
'
