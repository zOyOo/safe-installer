# Known Issues

Tracked issues that are real but deferred — too niche or complex to fix now.

---

## safe-brew

### [KB-1] `lib*` formula URL retry doesn't cover pre-`Formula/lib/` layouts

**File:** `safe-brew/safe-brew.py` → `_fetch_raw_formula`

`_fetch_raw_formula` retries between the flat (`Formula/<name>.rb`) and sharded
(`Formula/<n>/<name>.rb`) layouts on 404. But for `lib*` formulas scanned under
`Formula/lib/<name>.rb`, neither layout appears in the URL, so no retry is attempted.
If the selected pinned SHA predates the `Formula/lib/` directory move, the download
404s instead of falling back to the flat or first-letter path.

**Impact:** Only affects pinned installs of `lib*` formulas (e.g. `libpng`) when the
safe fallback version is old enough to predate the `Formula/lib/` migration (~2022).
Uncommon in practice.

**Fix sketch:** Extend `_fetch_raw_formula` to also try `Formula/lib/<name>.rb` as a
third candidate when the URL contains neither `flat` nor `sharded`.

---

### [KB-2] Pinned fallback uses `bump_sha`, not the latest safe non-version-bump edit

**File:** `safe-brew/safe-brew.py` → `_raw_url_for`, `_find_fallback`

When pinning a formula to a fallback version, the raw GitHub URL points to the
version-bump commit (`bump_sha`). If a subsequent non-version-bump edit (checksum
fix, URL change, dependency patch) was made after the bump and is now also older than
the quarantine window, the pinned install uses the older, potentially broken formula
state rather than the most up-to-date safe one.

We use `bump_sha` deliberately — it's guaranteed to have the formula file at the
expected path, unlike `newest_sha` which can be a rename/migration commit where the
file has moved. The risk is small because `newest_commit` already causes the current
version to stay quarantined if any recent non-version-bump edit exists.

**Impact:** Pinned fallback installs may miss post-bump formula fixes, potentially
resulting in a stale checksum or broken source URL for old versions.

**Fix sketch:** Use `newest_sha` for the raw URL when it predates CUTOFF and the file
path is known to still be valid at that SHA (requires a HEAD check or a second API call).

---

## safe-pip

### [KB-3] Local dep check fails open on network errors (should fail closed)

**File:** `safe-pip/safe-pip.py` → `_check_local_deps` → `_check_ver`

When auditing dependencies of a local/editable package, any `fetch_pypi()` failure
(network error, timeout, 5xx) is caught as a generic exception and treated as
"not on PyPI — passing through". Only a 404 should be interpreted as a private/local
package; transient network errors should block the install (fail closed).

**Impact:** If PyPI is temporarily unreachable during a local package dep audit, deps
that exist on PyPI may be installed without an age check.

**Fix sketch:** Distinguish `urllib.error.HTTPError` with `code == 404` from other
exceptions in `_check_ver`; re-raise (or return a blocking result) for non-404 errors.

---

### [KB-4] Local dep downgrade ignores the local package's own version specifier

**File:** `safe-pip/safe-pip.py` → `_check_local_deps` → `_check_one`

When a resolved dependency is too new, `check_package(name, "")` searches for a safe
version across all releases without considering the specifier that the local package
actually declared (e.g. `foo>=2`). If no 2.x release is old enough, safe-pip will pin
`foo==1.x`, causing a pip resolver conflict instead of a clear "no safe version satisfies
the constraint" error.

**Impact:** Install fails with a confusing pip resolver error rather than a safe-pip
message. Not a silent bypass — the install is still blocked.

**Fix sketch:** Extract the local package's `requires_dist` from PyPI metadata (or the
local package itself), find the matching specifier for the dep being checked, and pass it
to `check_package`.

---

### [KB-5] Post-downgrade re-resolution is not iterative

**File:** `safe-pip/safe-pip.py` → `_check_local_deps`

After pinning too-new local deps, the code re-resolves once to catch newly introduced
transitive deps. But it only does one extra pass: if downgraded dep A introduces too-new
B, and downgraded B introduces too-new C, C only appears after B's constraint is applied
and never gets age-checked.

**Impact:** In a 3+-level downgrade cascade, transitive deps introduced by the second
(or later) downgrade are installed without an age check. Requires an unusual dependency
graph; not seen in practice.

**Fix sketch:** Loop `resolve_install_plan` + age-check until the pinned set stabilises
(i.e. no new pins are added in a round).
