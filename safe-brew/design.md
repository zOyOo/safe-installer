# safe-brew

Supply-chain attack protection for Homebrew. Only allows installing formulas/casks whose current version was committed to `homebrew-core` / `homebrew-cask` more than 30 days ago. Too-new packages are automatically pinned to the most recent safe version via raw GitHub URL installs.

## Design

### Core Idea

Homebrew has no central registry with publish dates (unlike PyPI or npm). Dates are derived from **git commit history**: when a version string first appears in a formula file's commit log, that commit's date is the "publish date". All checks and fallbacks are based on this date.

### Architecture

```
safe-brew.py    — Single-file CLI, stdlib-only (Python 3.9+)
install.sh      — Installer: copies script, creates wrappers
uninstall.sh    — Removes all installed files and wrappers
```

### Intercepted Commands

| Command | Behavior |
|---------|----------|
| `brew install <formula...>` | Check version dates, pin too-new to safe fallback, check deps recursively |
| `brew install --cask <cask...>` | Same, but queries `formulae.brew.sh/api/cask/` and `homebrew-cask` repo |
| `brew <anything else>` | Pass through to real brew unchanged |
| `--unsafe` flag | Bypass all safety checks |

### Version Date Derivation

Homebrew doesn't publish release timestamps. Instead, `scan_version_history()` queries the GitHub Commits API for the formula file:

```
GET /repos/Homebrew/homebrew-core/commits?path=Formula/w/wget.rb&per_page=100
```

Walking commits newest → oldest, it tracks version transitions and records:
- **`intro_date`** — date of the *oldest* commit that mentions this version (= when the version was first introduced; subsequent commits are patches/fixups)
- **`newest_sha`** — SHA of the *most recent* commit that still had this version (= most polished state of the formula, used for raw-URL installs)
- **`newest_file_commit_date`** — date of the most recent commit touching the file, regardless of whether the message matches the version pattern. Used to catch non-version-bump edits (URL/checksum/dep changes). `check_formula` uses `max(intro_date, newest_file_commit_date)` as the effective age.

`scan_version_history()` returns `(history, newest_file_commit_date, repo, scanned_path)`. The `scanned_path` is the actual file path that yielded results (sharded `Formula/<l>/<name>.rb` or legacy flat `Formula/<name>.rb`). Raw URLs are built from this path to avoid 404s on old SHAs where the sharded layout didn't yet exist.

**Fixup-commit SHA capture:** When a version bump commit is preceded by a non-version-bump commit (e.g. a checksum or dependency fix), the non-version-bump SHA is captured as `pending_sha` and used as `newest_sha` for that version block. This ensures raw-URL installs point to the most up-to-date state of the formula for that version.

This is done in a single API call per formula.

### Cask Version Format

Cask versions use `upstream,build` format (e.g. `6.8.0,60800`), but Homebrew commit messages only include the upstream part (`proxyman 6.8.0`). `_version_matches()` strips the `,build` suffix when comparing, so the date lookup works correctly.

### Version Pinning via Raw GitHub URLs

pip supports `pip install pkg==1.2.3`. Homebrew has no equivalent. Instead, specific formula versions are installed by pointing brew at a raw file from git history:

```
brew install https://raw.githubusercontent.com/Homebrew/homebrew-core/<sha>/Formula/o/openssl@3.rb
```

`newest_sha` (the last commit that had the safe version) is used here — it represents the final, most-patched state of that version, not the initial commit.

When a formula is safe, its plain name is passed. When it needs pinning, its raw URL is substituted. The final install command mixes names and URLs freely:

```
brew install wget \
  https://.../openssl@3.rb \
  https://.../ca-certificates.rb
```

### Transitive Dependency Auditing (BFS)

After checking top-level formulae, `collect_transitive_deps()` does a BFS over the dependency graph using the Homebrew JSON API:

```
GET https://formulae.brew.sh/api/formula/<name>.json
  → dependencies[]            (runtime deps, always installed)
  → recommended_dependencies[] (installed by default)
```

Excluded from the BFS:
- `build_dependencies` — bottles are pre-compiled; build deps are not installed at install time
- `optional_dependencies` — not installed by default
- `uses_from_macos` — system libraries, not installed by brew

Already-installed packages (from `brew list`) skip the age check — they were presumably safe when first installed.

**Fail-closed on metadata errors:** `fetch_formula_deps` raises on any API failure (no silent catch). `collect_transitive_deps` propagates the error as a `RuntimeError` with the offending package name and a clear message. `handle_install` exits with code 1 rather than proceeding with an incomplete graph. Use `--unsafe` to bypass.

**Cask auto-detection:** `check_formula` will retry as a cask if a formula lookup returns 404. The corrected `is_cask` value is stored in the result dict and used when seeding the BFS — so packages like `proxyman` that are casks (not formulas) get their deps fetched from the cask API endpoint.

Too-new deps get raw URL fallbacks, passed to `brew install` alongside the parent:

```
brew install wget https://.../libidn2.rb https://.../openssl@3.rb
```

#### Parent + dep coherence

When a dep D is too new, two things must be true for the install to work:

1. **The parent P must predate D's update** — otherwise P might require features only present in the new D.
2. **Both P and D must be passed as explicit raw URLs** — if P is passed as a plain name, brew resolves its `depends_on` declarations against the *current* homebrew-core at install time, ignoring any dep raw URLs we also pass.

**Why the safe parent always predates the too-new dep (by math):**

```
safe parent:  intro_date < CUTOFF  (now − SAFE_AGE_DAYS)
too-new dep:  intro_date > CUTOFF
∴  parent intro_date < CUTOFF < dep intro_date   — always true
```

A parent that passes the age check was necessarily released *before* the dep that fails it. No additional downgrade is needed; the current safe version of the parent is already the right version.

**What safe-brew does:**

1. Check the top-level formula P → safe, record its raw URL alongside its name.
2. Check deps via BFS → dep D is too new, find D's safe fallback raw URL.
3. Since deps need pinning, *promote* P's install target from name → raw URL.
4. Final command: `brew install <P_raw_url> <D_fallback_raw_url> ...`

This gives brew a completely explicit set of formula files with no homebrew-core resolution for any pinned package.

### Output Icons

| Icon | Meaning |
|------|---------|
| `✅` | Version is safe (published >N days ago) |
| `⬇️` | Current version too new; automatically pinned to safe fallback |
| `📌 pinned` | The specific version that will be installed |
| `⚠️` | Publish date unknown or third-party tap — allowed with warning |
| `❌` | No safe version found, or rate limit hit — install blocked |

### Recursion Prevention

`SAFE_BREW_ACTIVE=1` is set in the environment when safe-brew calls the real brew internally (dep listing, the final install). If `brew` is shadowed by the wrapper, this env var causes immediate passthrough to the real brew.

### install.sh — Wrapper Overwrite Guard

When `--brew-wrapper` is passed on a root + Intel Mac, `BIN_DIR` and `REAL_BREW` are both `/usr/local/bin/brew`. The Homebrew launcher is location-dependent (it derives `HOMEBREW_PREFIX` from `BASH_SOURCE`), so copying it to another directory breaks it. The installer detects this conflict and exits with an error, instructing the user to place `BIN_DIR` earlier in `PATH` so the wrapper shadows the real brew without touching it.

### GitHub API Rate Limiting

- **Unauthenticated**: 60 req/hr. Each `brew install` consumes one request per formula checked (top-level + new deps).
- **Authenticated**: 5000 req/hr — set `GITHUB_TOKEN` in the environment.
- **Rate limit → fail closed**: Unlike "date unknown" (warn and allow), hitting the rate limit blocks the install. The user must either set `GITHUB_TOKEN` or use `--unsafe`.

### Third-Party Taps

Formulas containing `/` (e.g. `user/tap/formula`) are taps outside `homebrew-core`/`homebrew-cask`. The GitHub history approach doesn't generalize to arbitrary repos, so these are warned about but allowed through.

## Known Limitations

- **Build-time supply chain attacks**: `build_dependencies` are not checked. An attacker updating a build tool within the 30-day window would not be caught. Mitigated in practice because most Homebrew installs use pre-built bottles.

- **Cask + formula dep URL mixing**: When a cask's formula dependency needs a raw-URL pin, the install command mixes `--cask` with formula raw URLs. Behavior depends on the brew version; most casks have no formula deps that need pinning, so this rarely triggers.

- **100-commit history window**: `scan_version_history` fetches at most 100 commits. A formula that hasn't changed version in 100+ commits will show "date unknown" (treated as old/stable → allowed). Configurable by extending `per_page` at the cost of more data transfer.

- **Dep version compatibility**: When a dep is pinned to an older version, it's not verified to be compatible with the parent formula. Homebrew itself enforces formula-level `depends_on` constraints, but cross-version compatibility at the application level is not checked.

## Installation

```bash
cd safe-brew/

# Install safe-brew command only
bash install.sh

# Also replace 'brew' with the safe-brew wrapper
bash install.sh --brew-wrapper
```

For the brew wrapper to take effect, `~/.local/bin` must appear **before** `/opt/homebrew/bin` (Apple Silicon) or `/usr/local/bin` (Intel) in `PATH`.

```bash
# fish
fish_add_path ~/.local/bin

# bash / zsh
export PATH="$HOME/.local/bin:$PATH"
```

## Configuration

| Env var | Default | Effect |
|---------|---------|--------|
| `SAFE_BREW_AGE_DAYS` | `30` | Minimum age in days for a version to be considered safe |
| `GITHUB_TOKEN` | (unset) | GitHub personal access token; raises rate limit from 60 to 5000 req/hr |
| `SAFE_BREW_REAL` | auto-detected | Path to the real brew binary; set by the wrapper, override if needed |

## Management Subcommands

```bash
safe-brew disable   # create ~/.safe-brew/disabled sentinel — bypass all checks
safe-brew enable    # remove sentinel — checks active again
safe-brew status    # show enabled/disabled state, age threshold, GitHub auth status
```
