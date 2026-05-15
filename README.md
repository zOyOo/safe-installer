# safe-install tools

Supply-chain attack protection for npm, pip, and Homebrew.

Enforces a **30-day quarantine** on newly published packages before they can be installed — giving the community time to catch malicious releases before they land on your machine.

---

## How supply-chain attacks work

An attacker publishes a malicious version of a popular package. Most attacks are detected and pulled from registries within days — but users who installed during that window are already compromised.

These tools close that window by refusing to install any package version published less than 30 days ago. If a package was just pushed, it waits. Once it clears the quarantine window, it installs normally.

All three tools also perform **recursive transitive dependency checking**: if A depends on B depends on C, all three versions are verified before anything is installed.

---

## Tools

| Tool | Protects | Registry checked |
|------|----------|-----------------|
| [safe-npm](#safe-npm) | `npm install` / `npx` | npmjs.org |
| [safe-pip](#safe-pip) | `pip install` | pypi.org |
| [safe-brew](#safe-brew) | `brew install` / `brew upgrade` | formulae.brew.sh + GitHub |

---

## safe-npm

### Install

```bash
cd safe-npm

# Install safe-npm and safe-npx commands only
bash install.sh

# Also replace npm and npx with wrappers (recommended)
bash install.sh --npm-wrapper
```

Requires Node.js ≥ 16 and npm.

Installs to `~/.safe-npm/` (or `/usr/local/lib/safe-npm/` if run as root).
Binaries are placed in `~/.local/bin/`.

### Usage

```bash
# Use directly
safe-npm install express
safe-npx create-react-app my-app

# Or transparently via wrappers (if --npm-wrapper was used)
npm install express
npx create-react-app my-app
```

When a package or any of its dependencies is too new, the install is blocked:

```
[safe-npm] ❌  express@5.0.0 was published 3 days ago (2024-11-01).
              Minimum age: 30 days. Blocked to protect against supply-chain attacks.
```

### Options

| Flag | Description |
|------|-------------|
| `--min-age=N` | Override the age threshold for this invocation (minimum: 2 days) |
| `--unsafe` | Bypass age checks entirely and call the real npm directly |

```bash
# You know this specific release is safe
npm install some-package --min-age=7

# Emergency bypass
npm install some-package --unsafe
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SAFE_NPM_AGE_DAYS` | `30` | Quarantine window in days |

### Disable temporarily

```bash
# Create the disabled flag file
touch ~/.safe-npm/disabled

# Remove to re-enable
rm ~/.safe-npm/disabled
```

### Uninstall

```bash
bash safe-npm/uninstall.sh
```

---

## safe-pip

### Install

```bash
cd safe-pip

# Install safe-pip command only
bash install.sh

# Also replace pip and pip3 (recommended)
bash install.sh --pip-wrapper

# Also protect pip inside virtual environments
bash install.sh --pip-wrapper --venv-hook
```

Requires Python ≥ 3.9 and pip.

Installs to `~/.safe-pip/` (or `/usr/local/lib/safe-pip/` if run as root).
Binaries are placed in `~/.local/bin/`.

Automatically registers a **pyenv hook** if pyenv is installed — new Python versions installed via `pyenv install` are automatically configured with safe-pip.

### Usage

```bash
# Use directly
safe-pip install requests

# Or transparently via wrappers (if --pip-wrapper was used)
pip install requests
pip3 install flask
```

When a package is too new:

```
[safe-pip] ❌  requests==2.32.0 was published 5 days ago (2024-11-07).
              Minimum age: 30 days. Blocked to protect against supply-chain attacks.
```

When safe-pip finds a safe older version, it offers to pin it:

```
[safe-pip] ⚠️  requests==2.32.0 is too new (5 days old).
              Latest safe version: 2.31.0 (published 2024-09-01, 67 days ago).
              Installing requests==2.31.0 instead.
```

### Virtual environment support

With `--venv-hook`, pip inside any activated venv is automatically routed through safe-pip:

```bash
# Create a protected venv (installs safe-pip into it automatically)
safe-venv myenv

# Or inject into an existing venv
safe-pip inject-venv ./myenv
```

### Options

| Flag | Description |
|------|-------------|
| `--min-age=N` | Override the age threshold for this invocation (minimum: 2 days) |
| `--unsafe` | Bypass age checks and call the real pip directly |

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SAFE_PIP_AGE_DAYS` | `30` | Quarantine window in days |

### Uninstall

```bash
bash safe-pip/uninstall.sh
```

---

## safe-brew

### Install

```bash
cd safe-brew

# Install safe-brew command only
bash install.sh

# Also replace the brew command (recommended)
bash install.sh --brew-wrapper
```

Requires Python ≥ 3.9 and Homebrew.

Installs to `~/.safe-brew/`. Binaries are placed in `~/.local/bin/`.

> **Note for `--brew-wrapper`:** `~/.local/bin` must appear before `/opt/homebrew/bin` (Apple Silicon) or `/usr/local/bin` (Intel) in your `PATH` for the wrapper to shadow the real `brew`.

### Usage

```bash
# Use directly
safe-brew install ripgrep
safe-brew upgrade

# Or transparently via wrapper (if --brew-wrapper was used)
brew install ripgrep
brew upgrade
```

safe-brew checks the last commit date of each formula's `.rb` file in the Homebrew GitHub repository. When a formula was updated too recently, it either blocks or installs the last safe version:

```
[safe-brew] ⚠️  ripgrep was updated 12 days ago.
              Installing last safe version: 13.0.0 (updated 2024-09-15, 45 days ago).
```

### GitHub API rate limits

safe-brew uses the GitHub API to check commit history. Unauthenticated requests are limited to 60/hour; setting a token raises this to 5000/hour.

```bash
export GITHUB_TOKEN=your_token_here
```

### Options

| Flag | Description |
|------|-------------|
| `--min-age=N` | Override the age threshold for this invocation (minimum: 2 days) |
| `--unsafe` | Bypass age checks and call the real brew directly |

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SAFE_BREW_AGE_DAYS` | `30` | Quarantine window in days |
| `GITHUB_TOKEN` | _(none)_ | GitHub personal access token for higher API rate limits |

### Uninstall

```bash
bash safe-brew/uninstall.sh
```

---

## Adding to PATH

All tools install binaries to `~/.local/bin`. If that directory isn't already in your PATH:

```bash
# bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

# zsh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc

# fish
fish_add_path ~/.local/bin
```

---

## Known limitations

See [KNOWN_ISSUES.md](KNOWN_ISSUES.md) for tracked edge cases that are real but deferred — mostly affecting unusual dependency graphs or niche package configurations.
