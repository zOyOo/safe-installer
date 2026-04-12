# safe-npm

Supply-chain attack protection for Node.js/npm/npx. Only allows installing package versions published >30 days ago, giving the community time to detect and remove malicious releases.

## Design

### Core Idea

Query the npm registry for each package's `time` field (version publish dates). Filter out versions published within the last 30 days (configurable via `SAFE_NPM_AGE_DAYS` env var). Pin to the newest "safe" version that satisfies the user's semver range.

### Architecture

```
index.js             — Single-file CLI, handles both safe-npm and safe-npx modes
package.json         — Declares semver dependency
install.sh           — Installer: copies files, creates wrappers
uninstall.sh         — Full uninstaller
```

Dispatches between npm and npx modes via `SAFE_NPX_MODE=1` env var.

### Two-Phase Install (lockfile audit)

BFS over the registry is an approximation — npm's actual resolver (deduplication, peer deps, hoisting) may produce a different dependency tree. To guarantee no unsafe package reaches `node_modules`, all install/update commands use a two-phase approach:

```
Phase 1: BFS pre-check — informational display (downgrades, skipped versions, icons)
Phase 2: npm install <pins> --package-lock-only — npm's full resolution, writes lockfile only
Phase 3: Read lockfile, check ALL packages (including ones BFS didn't cover) against registry
         Fail → restore package.json + package-lock.json backups, exit
Phase 4: npm install (no args) — installs from the already-verified lockfile, no re-resolution
```

This ensures that the **authoritative check** is against npm's actual resolution output, not our BFS approximation.

### Intercepted Commands

| Command | Behavior |
|---------|----------|
| `install <pkg...>` / `i` / `add` | Two-phase: BFS display → `--package-lock-only` → lockfile audit → install |
| `install` (no args, lockfile exists) | Audit ALL packages in lockfile (direct + transitive), then install |
| `install` (no args, no lockfile) | `--package-lock-only` to generate lockfile → audit → install |
| `update [pkg...]` / `up` / `upgrade` | Two-phase: pin safe versions → `--package-lock-only` → lockfile audit → install |
| `ci` | Audit ALL packages in lockfile, then `npm ci` |
| `npx <pkg>` (safe-npx) | Check package age, pin to safe version before executing |
| `--unsafe` flag | Bypass all safety checks (stripped before passing to npm) |
| All other commands | Pass through to real npm unchanged |

### BFS Pre-Check (Phase 1)

Used for user-facing display — shows which packages are downgraded, skipped, blocked:

1. Seeds BFS frontier with top-level pinned packages
2. Each BFS level: parallel-fetch per-version deps from `registry.npmjs.org/<pkg>/<version>`
3. Collects `dependencies` + `optionalDependencies` + `peerDependencies` (npm v7+ installs all three)
4. Resolves safe version for each new dep in parallel
5. Handles `npm:` aliases (e.g., `npm:@scope/pkg@1.0.0-darwin-arm64`):
   - Parses real package name and range
   - Checks date against real package
   - Marks `isNpmAlias: true` — excluded from explicit install args
6. Pre-release exact pins (platform packages) checked via `time` field directly
7. Cycle detection via `visited` Map (lowercase package names)

### Output Icons

- `✅` — version is safe (published >30 days ago)
- `⬇️` — latest version too new, downgraded to safe version
- `🚫 skipped (too new)` — versions between safe and latest, with dates and age
- `📌 pinned` — the version that will actually be installed
- `❌` — no safe version exists at all (install blocked)
- `⚠️` — registry unreachable, could not verify (warns but doesn't block)

### Recursion Prevention

Two layers:

1. **`SAFE_NPM_REAL` / `SAFE_NPX_REAL`** — wrapper scripts set these to point to the real npm/npx binaries. `runCmd()` uses these instead of calling the wrapper recursively.
2. **`SAFE_NPM_ACTIVE=1`** — set in the env of ALL spawned npm/npx processes. `main()` and `mainNpx()` check this at entry and pass through directly. Prevents double-auditing when `REAL_NPM` falls back to `'npm'` (our wrapper) because `SAFE_NPM_REAL` isn't set.

### Failure Rollback

If the lockfile audit (Phase 3) finds unsafe packages:
- `package.json` backup is restored (dependency additions from Phase 2 are reverted)
- `package-lock.json` backup is restored (resolution is reverted)
- `node_modules` was never touched (Phase 2 used `--package-lock-only`)

Project state returns to exactly what it was before the command ran.

### safe-npx

- Parses npx args, skipping value-consuming flags (`-p`, `-c`, `--package`, etc.)
- Finds the first non-flag arg as the package specifier
- Skips local paths (`.`, `/`) and URLs (`://`)
- Checks package age, pins to safe version, rewrites the arg, then calls real npx

## Disable / Enable / Uninstall

### Temporary disable (persists across shell restarts)

Uses a sentinel file `~/.safe-npm/disabled`. When present, all commands pass through to real npm/npx with zero overhead.

```bash
safe-npm disable      # create sentinel — bypass all checks
safe-npm enable       # remove sentinel — checks back on
safe-npm status       # show current state, age threshold, real npm path
```

Works for `npm`, `npx`, `safe-npm`, and `safe-npx` equally — all dispatch through the same `index.js` which checks the sentinel early.

### Full uninstall

```bash
bash uninstall.sh
```

Removes: `~/.safe-npm/` and `safe-npm`/`safe-npx`/`npm`/`npx` wrappers in `~/.local/bin/` (only if they are safe-npm wrappers).

## Installation

```bash
cd safe-npm/

# Install safe-npm and safe-npx commands only
bash install.sh

# Also replace npm and npx with safe wrappers
bash install.sh --npm-wrapper
```

### What install.sh does

1. Finds the real npm/npx binaries (handles already-wrapped detection)
2. Copies `index.js` and `package.json` to `~/.safe-npm/`
3. Installs `semver` dependency via `npm install --prefix`
4. Creates `~/.local/bin/safe-npm` and `~/.local/bin/safe-npx`
5. Optionally creates `~/.local/bin/npm` (with `SAFE_NPM_REAL`) and `~/.local/bin/npx` (with `SAFE_NPX_REAL`) wrappers

### Wrapper chain

```
~/.local/bin/npm  →  sets SAFE_NPM_REAL="/opt/homebrew/bin/npm"  →  safe-npm  →  node ~/.safe-npm/index.js
~/.local/bin/npx  →  sets SAFE_NPX_REAL="/opt/homebrew/bin/npx"  →  safe-npx  →  SAFE_NPX_MODE=1 node ~/.safe-npm/index.js
```

### Ensure PATH

`~/.local/bin` must be in PATH **before** system npm:

```bash
# bash/zsh
export PATH="$HOME/.local/bin:$PATH"

# fish
fish_add_path ~/.local/bin
```

## Special Considerations

- **semver dependency**: The only external dependency. Installed to `~/.safe-npm/node_modules/` by the installer. Required for version range matching.
- **Scoped packages**: Registry URLs encode `@scope/pkg` as `@scope%2Fpkg`.
- **npm: aliases**: Platform-specific packages often use `npm:@scope/pkg@version` notation. BFS resolves the real package for age checking but doesn't add aliases to the install command (npm handles alias resolution internally).
- **Pre-release versions**: Filtered out by default (`semver.prerelease(v)`), but platform-specific exact pins (e.g., `1.0.0-darwin-arm64`) are accepted as a fallback and checked via the `time` field.
- **--unsafe flag**: Stripped from args before passing to npm (npm doesn't recognize `--unsafe`).
- **Registry errors during BFS**: Warning is shown (`⚠️ registry unreachable`). The lockfile audit in Phase 3 is the authoritative check — if a package slipped through BFS, it will still be caught there.
- **optionalDependencies/peerDependencies**: Both are fetched and checked during BFS, and verified again in the lockfile audit.
- **Performance**: The lockfile audit checks all packages against the registry in batches of 25. For large projects (hundreds of deps) this adds ~10-20s. The tradeoff is correctness — every package is verified.
