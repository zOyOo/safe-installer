# Workspace & --prefix Support for safe-npm

This document captures the design decisions and edge cases discovered during the initial (abandoned) implementation of workspace support. Use as a reference when implementing this feature.

## What needs to be supported

npm flags that change *where* packages are installed:

| Flag | Form | Meaning |
|------|------|---------|
| `--prefix <dir>` | value flag | Install into a different directory |
| `-w <name>`, `--workspace <name>` | value flag (repeatable) | Target a specific workspace |
| `--workspace=<name>` | equals form | Same as above |
| `--workspaces`, `--ws` | boolean flag | Target ALL workspaces |

These flags are already kept in `flags` (not `pkgArgs`) by `splitFlagsAndPkgs` via `NPM_INSTALL_VALUE_FLAGS`, so package-name parsing is already correct. The gaps are in the audit and rollback logic.

---

## --prefix

### Problem
`handleInstallWithPackages` and `handleInstallNoPackages` both hardcode `process.cwd()` for `lockPath` and `pkgPath`. When `--prefix ./app` is passed, npm writes `package-lock.json` and `package.json` to `./app/`, so safe-npm reads/restores the wrong files.

### Fix
Extract prefix dir from flags and use it as the base dir:

```js
function getPrefixDir(flags) {
  for (let i = 0; i < flags.length; i++) {
    if (flags[i] === '--prefix' && i + 1 < flags.length) return path.resolve(flags[i + 1]);
    if (flags[i].startsWith('--prefix=')) return path.resolve(flags[i].slice('--prefix='.length));
  }
  return null;
}

// In handleInstallWithPackages / handleInstallNoPackages / handleUpdate:
const baseDir = getPrefixDir(flags) ?? process.cwd();
const lockPath = path.join(baseDir, 'package-lock.json');
const pkgPath  = path.join(baseDir, 'package.json');
```

Apply to: `handleInstallWithPackages`, `handleInstallNoPackages`, `handleUpdate` (both manifest read and lockfile paths).

---

## --workspace / -w

### Problem 1: workspace manifest not backed up on rollback
When `npm install pkg --workspace app --package-lock-only` runs, it modifies both:
- Root `package-lock.json` ✓ (already backed up)
- Root `package.json` ✓ (already backed up)
- The workspace's `package.json` ✗ (NOT backed up)

If Phase 3 audit fails, the rollback leaves the workspace manifest with the blocked dependency still added.

### Fix
Back up all workspace manifests before Phase 2, restore them on failure:

```js
function getWorkspaceManifestPaths(rootDir) {
  const root = readJson(path.join(rootDir, 'package.json'));
  if (!root?.workspaces) return [];
  const patterns = Array.isArray(root.workspaces)
    ? root.workspaces
    : (root.workspaces.packages || []);
  const results = [];
  for (const pattern of patterns) {
    if (pattern.endsWith('/*')) {
      const parent = path.join(rootDir, pattern.slice(0, -2));
      if (!fs.existsSync(parent)) continue;
      for (const entry of fs.readdirSync(parent, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        const p = path.join(parent, entry.name, 'package.json');
        if (fs.existsSync(p)) results.push(p);
      }
    } else {
      const p = path.join(rootDir, pattern, 'package.json');
      if (fs.existsSync(p)) results.push(p);
    }
  }
  return results;
}
// Note: only handles "dir/*" and literal "dir/name" glob patterns.
// Complex patterns (e.g. "packages/app-*") need a real glob library.
```

Then in `handleInstallWithPackages`:
```js
const wsPkgPaths = getWorkspaceManifestPaths(baseDir);
const wsBackups  = wsPkgPaths.map(p => ({ p, buf: fs.existsSync(p) ? fs.readFileSync(p) : null }));

const restoreAll = () => {
  if (lockBackup) fs.writeFileSync(lockPath, lockBackup);
  else if (fs.existsSync(lockPath)) fs.unlinkSync(lockPath);
  if (pkgBackup) fs.writeFileSync(pkgPath, pkgBackup);
  for (const { p, buf } of wsBackups) { if (buf) fs.writeFileSync(p, buf); }
};
```

### Problem 2: handleUpdate reads wrong manifest for workspace-scoped updates
`safe-npm update -w packages/app` should find update candidates from the workspace's `package.json`, not the root one. Without this, workspace-only deps are invisible to the update planner, and npm may receive wrong or missing ranges.

### Fix
Resolve workspace dirs from `-w`/`--workspace` values, read deps from there:

```js
function getWorkspaceValues(flags) {
  const values = [];
  let allWorkspaces = false;
  for (let i = 0; i < flags.length; i++) {
    if (flags[i] === '--workspaces' || flags[i] === '--ws') { allWorkspaces = true; continue; }
    if ((flags[i] === '-w' || flags[i] === '--workspace') && i + 1 < flags.length) {
      values.push(flags[++i]);
    } else if (flags[i].startsWith('--workspace=')) {
      values.push(flags[i].slice('--workspace='.length));
    }
  }
  return { values, allWorkspaces };
}

function resolveWorkspaceDirs(wsValues, baseDir) {
  if (!wsValues.length) return [];
  const allManifests = getWorkspaceManifestPaths(baseDir);
  const dirs = [];
  for (const val of wsValues) {
    const directPath = path.resolve(baseDir, val);
    if (fs.existsSync(path.join(directPath, 'package.json'))) {
      dirs.push(directPath);
      continue;
    }
    // Fallback: match by package name
    for (const p of allManifests) {
      const pkg = readJson(p);
      if (pkg?.name === val) { dirs.push(path.dirname(p)); break; }
    }
  }
  return dirs;
}
```

---

## Multiple workspaces: the range collision problem

This is the hardest case. When `safe-npm update -w a -w b` (or `--workspaces`) is used:

**Problem**: If both workspaces declare `react` but with different ranges (`^17` vs `^18`), merging their dep maps means one range wins (last-write). Then `npm install react@X -w a -w b` pins react to the same X in both, potentially violating one workspace's declared range.

**Problem**: When no explicit packages are given and workspaces have *disjoint* deps, a naive union causes `npm install A_deps B_deps -w a -w b` to add A's deps into B and B's deps into A.

### Correct fix: per-workspace sequential updates

The only truly correct approach is to run `handleUpdate` once per workspace with a single `-w` flag. Each call independently:
1. Reads that workspace's own `package.json` for ranges
2. Finds safe versions within those ranges
3. Resolves the lockfile, audits, and installs

```js
function stripWorkspaceFlags(flags) {
  const result = [];
  for (let i = 0; i < flags.length; i++) {
    const f = flags[i];
    if (f === '--workspaces' || f === '--ws') continue;
    if ((f === '-w' || f === '--workspace') && i + 1 < flags.length) { i++; continue; }
    if (f.startsWith('--workspace=')) continue;
    result.push(f);
  }
  return result;
}

// In handleUpdate, before doing anything:
if (wsDirs.length > 1) {
  const baseFlags = stripWorkspaceFlags(flags);
  for (const wsDir of wsDirs) {
    await handleUpdate(pkgArgs, [...baseFlags, '--workspace', path.relative(baseDir, wsDir)]);
  }
  return;
}
```

**Note**: Sequential calls share the root lockfile. Each call backs up and (on failure) restores the lockfile as of the start of *that* call. On success, the lockfile accumulates changes from each workspace — this is correct behavior.

---

## npx --package flag forms

Both forms need to be handled in `mainNpx`:

| Form | Example | Notes |
|------|---------|-------|
| `-p pkg` | `npx -p cowsay` | Space-separated, already handled |
| `--package pkg` | `npx --package cowsay` | Space-separated, already handled |
| `--package=pkg` | `npx --package=cowsay` | **Equals form** — requires special handling |

### Fix for equals form
Store `equalsForm: true` on the entry and restore the prefix when rewriting:

```js
if (a.startsWith('--package=')) {
  pkgFlagEntries.push({ argIdx: i, spec: a.slice('--package='.length), equalsForm: true });
  continue;
}

// In the rewrite loop:
const pinned = await auditNpxSpec(spec);
newArgv[argIdx] = equalsForm ? `--package=${pinned}` : pinned;
```

Without this, `safe-npx --package=typescript tsc --version` becomes `npx typescript@x tsc --version` which npx interprets as "run tsc from a package named tsc", not from typescript.

---

## pip: missing value flags

`-C` / `--config-settings` is a value-taking pip install flag (used for build backend settings, e.g. `-C key=value`). Without it in `_PIP_VALUE_FLAGS`, `key=value` is classified as a package requirement and safe-pip tries to look it up on PyPI.

```python
_PIP_VALUE_FLAGS = {
    ...,
    "-C", "--config-settings",
}
```

---

## Flags to block (until workspace support is implemented)

These flags change the install target directory and must be blocked until workspace support is complete:

```js
const WS_FLAGS = new Set(['--workspace', '-w', '--workspaces', '--ws', '--prefix']);
const hasWsFlag = flags.some(
  f => WS_FLAGS.has(f) || f.startsWith('--workspace=') || f.startsWith('--prefix=')
);
if (hasWsFlag) {
  console.error('[safe-npm] ❌  Workspace and --prefix installs are not yet supported.\n' +
                '           Use --unsafe to bypass, or run npm directly.\n');
  process.exit(1);
}
```
