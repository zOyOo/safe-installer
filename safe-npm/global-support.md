# Global Install Support for safe-npm

This document captures the design decisions and edge cases discovered during the implementation
of `npm install -g` support, plus known gaps for future work.

## What is supported

`safe-npm install -g <pkg...>` is fully audited via a temp-directory lockfile flow:

1. **Phase 1** — BFS age check on top-level packages + transitive deps (same as local install).
2. **Phase 2** — Lockfile resolution in a temp dir. npm ignores `--package-lock-only` for global
   installs, so a throwaway `package.json` is written to `$TMPDIR/safe-npm-global-XXXX/` and
   `npm install --package-lock-only` is run there (without `-g`). The same resolver flags passed
   by the user (e.g. `--omit=optional`, `--legacy-peer-deps`) are forwarded so the audited tree
   matches what npm will actually install.
3. **Phase 3** — Full lockfile audit (same as local install).
4. **Phase 4** — `npm install -g <allPinned>` where `allPinned` includes both top-level and
   transitive safe pins. Transitive pins are passed explicitly because there is no lockfile to
   enforce them for global installs — without this, npm could re-resolve transitives to newer
   unsafe versions.

### Flags handled

| Flag | Handling |
|------|----------|
| `-g` | Detected and stripped before handler; re-added in Phase 4 |
| `--global` | Same |
| `--location=global` | Same |
| `--location global` | Same (space-separated value form) |

`--location` is also added to `NPM_INSTALL_VALUE_FLAGS` so the `global` value token is not
mistaken for a package name by `splitFlagsAndPkgs`.

### `npm install -g` with no packages

`safe-npm install -g` (no package args) re-adds `-g` to the flags before calling
`handleInstallNoPackages`, so the audit-then-install flow uses the global flag correctly.

---

## What is NOT supported (blocked)

### `npm update -g`

Global updates are blocked with an early error. The challenge:

- `handleUpdate` reads `package.json` from cwd to find declared dependency ranges. For global
  packages there is no cwd `package.json` — the update target is the global prefix.
- `npm update -g` without package args would update *all* global packages; the list must be
  discovered from `npm ls -g --depth=0 --json`, not from a local manifest.
- With package args (`npm update -g eslint`), the current range must be fetched from the global
  install, not cwd.

**Implementation plan when needed:**
1. Run `npm ls -g --depth=0 --json` to get the current global packages and their installed versions.
2. For each target, use the installed version as the current range floor (or fetch the declared
   range from `npm ls -g`).
3. Resolve and audit via the same temp-dir lockfile flow used for `npm install -g`.
4. Run `npm install -g <pinned-updates>` for Phase 4.

---

## Edge cases to watch for

### Transitive pins and npm's global deduplication

npm deduplicates global packages differently from local ones — there is no `node_modules`
hoisting tree, just a flat global prefix. Passing all transitive pins explicitly in Phase 4
(`npm install -g pkg@x dep@y dep2@z`) is correct but may install more packages than a plain
`npm install -g pkg` would (npm normally only installs what the top-level package declares).
This is intentional: it locks the audited versions in place.

### `--prefix` with `-g`

`npm install -g --prefix /some/dir` installs into a custom global prefix. This combination is
currently blocked by the `--prefix` workspace guard before the global flag is even checked.
If `--prefix` + `-g` support is needed later:
- Strip both flags for temp-dir resolution.
- Pass both back in Phase 4.
- The lockfile resolution temp dir should not be affected by `--prefix`.

### `npm link` with global semantics

`npm link` (no args) creates a global symlink for the current package. This is not an
install/update command and passes through to npm unchanged — no auditing is applied.
If auditing `npm link` is desired, it would need its own handler.
