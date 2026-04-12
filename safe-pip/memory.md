# safe-pip

Supply-chain attack protection for Python/pip. Only allows installing package versions published >30 days ago, giving the community time to detect and remove malicious releases.

## Design

### Core Idea

Query PyPI for each package's release dates. Filter out versions published within the last 30 days (configurable via `SAFE_PIP_AGE_DAYS` env var). Pin to the newest "safe" version that satisfies the user's specifier.

### Architecture

```
safe-pip.py          — Single-file CLI, zero external deps (uses packaging from pip vendor)
usercustomize.py     — Hook to intercept `python -m pip install` (Python 3.10+)
pyenv_hook.bash      — Auto-configures usercustomize for new pyenv-installed Pythons
install.sh           — Installer: copies files, creates wrappers, sets up hooks
```

### Intercepted Commands

| Command | Behavior |
|---------|----------|
| `install <pkg...>` | Query PyPI, downgrade each to latest safe version, check transitive deps recursively |
| `install -r requirements.txt` | Parse requirements file, check each package |
| `install` (no args) | Pass through to pip (installs from lock/setup) |
| `--unsafe` flag | Bypass all safety checks |
| All other commands | Pass through to real pip unchanged |

### Transitive Dependency Resolution (BFS)

1. After pinning top-level packages, run `pip install --dry-run` to get the dependency tree
2. Fallback: fetch `requires_dist` from PyPI metadata API for each pinned package
3. BFS with parallel fetching at each level — resolves A->B->C->... at all depths
4. Cycle detection via `visited` dict (lowercase package names)
5. PEP 508 markers evaluated with `default_environment()` to filter platform-irrelevant deps
6. Each transitive dep is checked and downgraded if needed (not blocked)

### Output Icons

- `✅` — version is safe (published >30 days ago)
- `⬇️` — latest version too new, downgraded to safe version
- `🚫 skipped (too new)` — versions between safe and latest, with dates and age
- `🐍 skipped (py incompatible)` — newer versions that don't support current Python
- `📌 pinned` — the version that will actually be installed
- `❌` — no safe version exists at all (install blocked)

### Recursion Prevention

- `SAFE_PIP_ACTIVE=1` env var set when safe-pip internally calls `python -m pip`
- `usercustomize.py` checks this var and skips interception if set
- `usercustomize.py` uses `os._exit()` (not `sys.exit()`) to avoid crashing `site.py`

### `python -m pip` Interception

- `usercustomize.py` installed to each Python version's user site-packages
- Uses `sys.orig_argv` (Python 3.10+) to reliably detect `-m pip` invocation
- On Python 3.9-, only the pip/pip3 wrapper intercepts (no `sys.orig_argv`)

## Disable / Enable / Uninstall

### Temporary disable (persists across shell restarts)

Uses a sentinel file `~/.safe-pip/disabled`. When present, all commands pass through to real pip with zero overhead.

```bash
safe-pip disable      # create sentinel — bypass all checks
safe-pip enable       # remove sentinel — checks back on
safe-pip status       # show current state + age threshold
```

Works for `pip install`, `pip3 install`, and `python -m pip install` equally — usercustomize calls safe-pip.py which checks the sentinel before doing anything.

### Full uninstall

```bash
bash uninstall.sh
```

Removes: `~/.safe-pip/`, all `usercustomize.py` files from every Python version's user site-packages, pyenv hook, and `safe-pip`/`pip`/`pip3` wrappers in `~/.local/bin/`.

## Installation

```bash
cd safe-pip/

# Install safe-pip command only
bash install.sh

# Also replace pip/pip3 with safe-pip wrappers
bash install.sh --pip-wrapper
```

### What install.sh does

1. Copies `safe-pip.py` and `usercustomize.py` to `~/.safe-pip/`
2. Creates `~/.local/bin/safe-pip` wrapper script
3. Optionally creates `~/.local/bin/pip` and `pip3` wrappers
4. Installs `usercustomize.py` to current Python's user site-packages
5. Installs `usercustomize.py` to all existing pyenv Python versions
6. Registers pyenv hook at `$PYENV_ROOT/plugins/safe-pip/etc/pyenv.d/install/after.bash`

### Ensure PATH

`~/.local/bin` must be in PATH **before** system pip:

```bash
# bash/zsh
export PATH="$HOME/.local/bin:$PATH"

# fish
fish_add_path ~/.local/bin
```

## Special Considerations

- **pyenv**: The installer auto-configures all existing pyenv Pythons and registers a hook for future `pyenv install` runs. The hook uses `after_install '...'` (not bare code) because pyenv sources hook files *before* installation begins.
- **Dry-run fallback**: If `pip install --dry-run` output parsing fails (older pip versions), falls back to fetching `requires_dist` from PyPI metadata API directly.
- **packaging library**: Uses system `packaging` if available, falls back to `pip._vendor.packaging` (always bundled with pip).
- **Marker evaluation**: Uses `default_environment()` for full PEP 508 marker evaluation — correctly handles `sys_platform`, `python_version`, etc.
- **Already-installed packages**: If a package is already installed at a safe version, skips the PyPI check entirely for speed.
- **New packages with no safe version**: Blocks installation and suggests `--unsafe` to bypass.
