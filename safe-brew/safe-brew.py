#!/usr/bin/env python3
"""safe-brew: only install Homebrew formulas/casks updated >30 days ago (supply-chain protection)."""
from __future__ import annotations
import sys
import os
import re
import shutil
import tempfile

sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)

import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

SAFE_AGE_DAYS = int(os.environ.get('SAFE_BREW_AGE_DAYS', '30'))
CUTOFF = datetime.now(timezone.utc) - timedelta(days=SAFE_AGE_DAYS)
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')

INSTALL_DIR = os.path.expanduser("~/.safe-brew")
DISABLED_FLAG = os.path.join(INSTALL_DIR, "disabled")


# ─── GitHub API ───────────────────────────────────────────────────────────────

class RateLimitError(RuntimeError):
    pass


class GitHubAPIError(RuntimeError):
    pass


def _github_headers() -> dict:
    h = {"User-Agent": "safe-brew/1.0", "Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h


def _github_get(url: str) -> list | dict | None:
    try:
        req = urllib.request.Request(url, headers=_github_headers())
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RateLimitError(
                "GitHub API rate limit hit. Set GITHUB_TOKEN env var for higher limits "
                "(unauthenticated: 60 req/hr, authenticated: 5000 req/hr)."
            )
        raise GitHubAPIError(
            f"GitHub API returned HTTP {e.code} for {url}. "
            "Check GITHUB_TOKEN validity or retry later. Use --unsafe to bypass."
        )
    except urllib.error.URLError as e:
        raise GitHubAPIError(
            f"GitHub API unreachable: {e.reason}. "
            "Check network connectivity or use --unsafe to bypass."
        )
    except Exception as e:
        raise GitHubAPIError(f"GitHub API request failed: {e}. Use --unsafe to bypass.")


# ─── Homebrew JSON API ────────────────────────────────────────────────────────

def fetch_formula_info(name: str, is_cask: bool = False) -> dict:
    kind = "cask" if is_cask else "formula"
    url = f"https://formulae.brew.sh/api/{kind}/{name}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "safe-brew/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f'{"Cask" if is_cask else "Formula"} "{name}" not found on Homebrew')
        raise RuntimeError(f'Homebrew API returned HTTP {e.code} for "{name}"')
    except Exception as e:
        raise RuntimeError(f'Failed to fetch "{name}" from Homebrew: {e}')


def fetch_formula_deps(name: str, is_cask: bool = False) -> tuple[list[str], list[str]]:
    """Return (formula_deps, cask_deps) for a formula/cask.
    Raises RuntimeError if the Homebrew API is unreachable — callers must fail
    closed rather than silently skipping dependency checks."""
    info = fetch_formula_info(name, is_cask)  # raises on any failure

    if is_cask:
        dep_on = info.get("depends_on", {})
        return dep_on.get("formula", []), dep_on.get("cask", [])
    else:
        deps = info.get("dependencies", []) + info.get("recommended_dependencies", [])
        return deps, []


def collect_transitive_deps(
    targets: list[tuple[str, bool]]
) -> list[tuple[str, bool]]:
    """BFS over all transitive deps. Returns [(name, is_cask), ...] in discovery order.

    Raises RuntimeError (with the offending package name) if any dep metadata
    fetch fails — callers must block the install rather than proceed with an
    incomplete dependency graph.
    """
    visited: set[str] = {f"{n}:{'cask' if c else 'formula'}" for n, c in targets}
    queue: list[tuple[str, bool]] = list(targets)
    result: list[tuple[str, bool]] = []

    while queue:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(fetch_formula_deps, n, c): (n, c) for n, c in queue}
            next_queue: list[tuple[str, bool]] = []
            for fut in as_completed(futures):
                n, c = futures[fut]
                try:
                    formula_deps, cask_deps = fut.result()
                except Exception as e:
                    raise RuntimeError(
                        f'Failed to fetch dependency metadata for "{n}": {e}\n'
                        f'Cannot verify the full dependency graph — install blocked.\n'
                        f'Use --unsafe to bypass, or retry when the Homebrew API is reachable.'
                    )
                for d in formula_deps:
                    key = f"{d}:formula"
                    if key not in visited:
                        visited.add(key)
                        result.append((d, False))
                        next_queue.append((d, False))
                for d in cask_deps:
                    key = f"{d}:cask"
                    if key not in visited:
                        visited.add(key)
                        result.append((d, True))
                        next_queue.append((d, True))
        queue = next_queue

    return result


def get_installed_brew_packages() -> set[str]:
    """Return lowercase names of all currently-installed formulae and casks."""
    installed: set[str] = set()
    try:
        env = {**os.environ, "SAFE_BREW_ACTIVE": "1"}
        for flag in ("--formula", "--cask"):
            r = subprocess.run(
                [_find_real_brew(), "list", flag, "--full-name"],
                capture_output=True, text=True, env=env, timeout=15,
            )
            if r.returncode == 0:
                installed |= {line.strip().lower() for line in r.stdout.splitlines() if line.strip()}
    except Exception:
        pass
    return installed


# ─── Version history via GitHub commits ──────────────────────────────────────

def _extract_version_from_msg(name: str, first_line: str) -> str | None:
    """Extract version from 'formulaname X.Y.Z' style Homebrew commit message."""
    m = re.match(rf'^{re.escape(name)}\s+(\S+)', first_line, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _version_matches(api_version: str, commit_version: str) -> bool:
    """Match Homebrew API version to commit-message version.
    Cask versions use 'upstream,build' (e.g. '6.8.0,60800'); commit messages
    use only the upstream part ('6.8.0')."""
    if api_version == commit_version:
        return True
    if "," in api_version and api_version.split(",")[0] == commit_version:
        return True
    return False


def scan_version_history(
    name: str, is_cask: bool = False
) -> tuple[list[tuple[str, str, datetime]], datetime | None, str, str]:
    """Walk GitHub commits for a formula/cask file.

    Returns (history, newest_file_commit_date, repo, scanned_path):
      history            — list of (version, newest_sha, intro_date) newest-version-first
      newest_file_commit_date — date of the most recent commit touching the file
      repo               — e.g. "Homebrew/homebrew-core"
      scanned_path       — the exact file path that returned results (may be
                           the sharded or legacy flat layout); raw URLs must use
                           this path to avoid 404s on old SHAs.

    Raises RateLimitError if the GitHub API limit is hit.
    """
    if is_cask:
        repo = "Homebrew/homebrew-cask"
        paths = [f"Casks/{name[0]}/{name}.rb", f"Casks/{name}.rb"]
    else:
        repo = "Homebrew/homebrew-core"
        paths = [f"Formula/{name[0]}/{name}.rb", f"Formula/{name}.rb"]

    for path in paths:
        history, newest_commit = _scan_path(repo, path, name)
        if history:
            return history, newest_commit, repo, path
    return [], None, repo, paths[0]


def _scan_path(
    repo: str, path: str, name: str
) -> tuple[list[tuple[str, str, datetime]], datetime | None]:
    """Return (history, newest_file_commit_date).

    newest_file_commit_date is the date of the most recent commit that touched
    this file, regardless of whether its message matches the version pattern.
    It is used to catch non-version-bump edits (URL/checksum/dependency changes)
    that are recent enough to be suspicious.
    """
    url = f"https://api.github.com/repos/{repo}/commits?path={path}&per_page=100"
    commits = _github_get(url)  # may raise RateLimitError
    if not commits or not isinstance(commits, list):
        return [], None

    results: list[tuple[str, str, datetime]] = []
    current_ver: str | None = None
    current_newest_sha: str = ""
    current_intro_date: datetime | None = None
    newest_file_commit_date: datetime | None = None
    pending_sha: str | None = None  # most-recent non-version-bump SHA, applied to next version block

    for commit in commits:
        sha = commit.get("sha", "")
        msg = commit.get("commit", {}).get("message", "")
        date_str = commit.get("commit", {}).get("committer", {}).get("date", "")
        if not date_str:
            continue
        try:
            d = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        if newest_file_commit_date is None:
            newest_file_commit_date = d  # first commit in newest→oldest walk = most recent

        ver = _extract_version_from_msg(name, msg.split("\n")[0].strip())
        if ver is None:
            # Non-version-bump edit (checksum/URL/dep fix). Record the SHA so the
            # next version block picks it up as its newest_sha — it represents the
            # most up-to-date state of that version. Works across all version
            # boundaries (not just before the first version commit).
            if pending_sha is None:
                pending_sha = sha
            continue

        if current_ver is None:
            current_ver = ver
            current_newest_sha = pending_sha or sha  # use fixup SHA if present
            current_intro_date = d
            pending_sha = None
        elif ver == current_ver:
            current_intro_date = d  # older commit → earlier intro date
        else:
            results.append((current_ver, current_newest_sha, current_intro_date))  # type: ignore[arg-type]
            current_ver = ver
            current_newest_sha = pending_sha or sha
            current_intro_date = d
            pending_sha = None

    if current_ver and current_intro_date:
        results.append((current_ver, current_newest_sha, current_intro_date))

    return results, newest_file_commit_date


# ─── Formula safety check ─────────────────────────────────────────────────────

def check_formula(name: str, is_cask: bool = False) -> dict:
    """Return a result dict for one formula/cask.

    Possible keys (beyond name/is_cask/version):
      safe=True                         → OK to install at current version
      safe=False, fallback=(v,url,date) → too new; older version available
      safe=False, fallback=None         → too new; no safe fallback found
      date_unknown=True, reason=...     → can't determine publish date (warn, allow)
      rate_limited=str                  → GitHub rate limit; block install
      error=str                         → fatal error
    """
    if "/" in name:
        return {"name": name, "is_cask": is_cask, "version": "?", "date_unknown": True,
                "reason": "third-party tap — cannot audit"}

    try:
        info = fetch_formula_info(name, is_cask)
    except ValueError as e:
        if not is_cask:
            try:
                info = fetch_formula_info(name, is_cask=True)
                is_cask = True
            except Exception:
                return {"name": name, "is_cask": False, "error": str(e)}
        else:
            return {"name": name, "is_cask": is_cask, "error": str(e)}
    except Exception as e:
        return {"name": name, "is_cask": is_cask, "error": str(e)}

    version = info.get("version") if is_cask else info.get("versions", {}).get("stable")
    if not version:
        return {"name": name, "is_cask": is_cask, "error": "could not determine version"}

    # Use the canonical name from the API for history scanning.
    # Aliases like "python" resolve to "python@3.14" in the API response and in
    # commit messages — scanning under the alias would miss all history.
    canonical_name = info.get("token") if is_cask else info.get("name", name)
    if not canonical_name:
        canonical_name = name

    try:
        history, newest_commit, repo, scanned_path = scan_version_history(canonical_name, is_cask)
    except RateLimitError as e:
        return {"name": name, "is_cask": is_cask, "version": version, "rate_limited": str(e)}
    except GitHubAPIError as e:
        return {"name": name, "is_cask": is_cask, "version": version, "rate_limited": str(e)}

    if not history:
        return {"name": name, "is_cask": is_cask, "version": version, "date_unknown": True,
                "reason": "not found in recent GitHub history (likely old/stable)"}

    # Find current version's intro date
    intro_date: datetime | None = None
    for hist_ver, _, hist_date in history:
        if _version_matches(version, hist_ver):
            intro_date = hist_date
            break

    if intro_date is None:
        # Current version not in history — brand-new, treat as just published.
        intro_date = datetime.now(timezone.utc)
    elif newest_commit and newest_commit > intro_date:
        # A non-version-bump commit (URL/checksum/dep change) is more recent than
        # the version introduction. Use the newer date so silent formula edits are
        # subject to the same quarantine as version bumps.
        intro_date = newest_commit

    age_days = (datetime.now(timezone.utc) - intro_date).days

    if intro_date < CUTOFF:
        # Also compute the raw URL for this safe version so that if any dep needs
        # pinning, the parent can be passed as a raw URL too (prevents brew from
        # re-resolving the dep to the current homebrew-core version).
        raw_url = _raw_url_for(repo, scanned_path, history, version)
        return {
            "name": name, "is_cask": is_cask, "version": version,
            "pub_date": intro_date.strftime("%Y-%m-%d"), "age_days": age_days,
            "safe": True, "raw_url": raw_url,
        }

    # Current version is too new — find the most recent safe fallback
    fallback = _find_fallback(repo, scanned_path, history, newest_commit)
    return {
        "name": name, "is_cask": is_cask, "version": version,
        "pub_date": intro_date.strftime("%Y-%m-%d"), "age_days": age_days,
        "safe": False,
        "fallback": fallback,  # (version, raw_url, intro_date) or None
    }


def _raw_url_for(
    repo: str, path: str, history: list[tuple[str, str, datetime]], version: str
) -> str | None:
    """Return the raw GitHub URL for `version` using the scanned path and newest_sha."""
    sha = next((s for v, s, _ in history if _version_matches(version, v)), None)
    return f"https://raw.githubusercontent.com/{repo}/{sha}/{path}" if sha else None


def _find_fallback(
    repo: str, path: str, history: list[tuple[str, str, datetime]],
    newest_file_commit_date: datetime | None = None,
) -> tuple[str, str, datetime] | None:
    """Return (version, raw_url, intro_date) for the most recent safe version, or None.

    newest_file_commit_date is the date of the most recent file edit (may be a
    non-version-bump commit). For the first history entry (the current version),
    we use max(intro_date, newest_file_commit_date) as the effective age so that
    a version whose newest_sha points at a recently-quarantined edit is not
    mistakenly returned as a safe fallback.
    """
    for i, (hist_ver, newest_sha, intro_date) in enumerate(history):
        effective_date = intro_date
        if i == 0 and newest_file_commit_date and newest_file_commit_date > intro_date:
            effective_date = newest_file_commit_date
        if effective_date < CUTOFF:
            raw_url = f"https://raw.githubusercontent.com/{repo}/{newest_sha}/{path}"
            return (hist_ver, raw_url, intro_date)
    return None


# ─── Pinned cask installer (via temporary local tap) ─────────────────────────

def _install_casks_via_tap(
    name_target_pairs: list[tuple[str, str]],
    extra_targets: list[str],
    flags: list[str],
) -> None:
    """Install pinned casks via a temporary local tap.

    brew refuses to install casks from raw URLs or arbitrary local files —
    they must live in a tap (a git repo with the correct directory layout).
    We create a minimal git repo, commit the downloaded cask file(s) into it,
    register it as a local tap, install, then untap and delete.
    """
    tap_dir = tempfile.mkdtemp(prefix="safe-brew-tap-")
    tap_name = "safe-brew/pinned"
    real_brew = _find_real_brew()
    env = {**os.environ, "SAFE_BREW_ACTIVE": "1"}

    try:
        casks_dir = os.path.join(tap_dir, "Casks")
        os.makedirs(casks_dir)

        tap_targets: list[str] = []
        for cask_name, target in name_target_pairs:
            if target.startswith("https://"):
                local = os.path.join(casks_dir, f"{cask_name}.rb")
                req = urllib.request.Request(target, headers={"User-Agent": "safe-brew/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    with open(local, "wb") as f:
                        f.write(r.read())
                tap_targets.append(f"{tap_name}/{cask_name}")
            else:
                tap_targets.append(target)

        # brew requires taps to be git repos
        git_env = {**os.environ,
                   "GIT_AUTHOR_NAME": "safe-brew", "GIT_AUTHOR_EMAIL": "safe-brew@localhost",
                   "GIT_COMMITTER_NAME": "safe-brew", "GIT_COMMITTER_EMAIL": "safe-brew@localhost"}
        subprocess.run(["git", "init", "-q", tap_dir], capture_output=True, env=git_env)
        subprocess.run(["git", "-C", tap_dir, "add", "."], capture_output=True, env=git_env)
        subprocess.run(["git", "-C", tap_dir, "commit", "-q", "-m", "safe-brew pinned cask"],
                       capture_output=True, env=git_env)

        tap_result = subprocess.run([real_brew, "tap", tap_name, tap_dir],
                                    env=env, capture_output=True, text=True)
        if tap_result.returncode != 0:
            raise RuntimeError(f"brew tap failed: {tap_result.stderr.strip()}")

        try:
            all_targets = tap_targets + extra_targets
            print(f"\n[safe-brew] Running: brew install --cask {' '.join(all_targets + flags)}\n")
            sys.stdout.flush()
            result = subprocess.run([real_brew, "install", "--cask"] + all_targets + flags, env=env)
        finally:
            subprocess.run([real_brew, "untap", tap_name], env=env, capture_output=True)

        sys.exit(result.returncode)

    except Exception as e:
        print(f"\n[safe-brew] ❌  Failed to create temporary tap for cask pinning: {e}\n",
              file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tap_dir, ignore_errors=True)


# ─── Real brew ────────────────────────────────────────────────────────────────

def _find_real_brew() -> str:
    real = os.environ.get("SAFE_BREW_REAL", "")
    if real and os.path.isfile(real):
        return real
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.path.isfile(candidate):
            return candidate
    import shutil
    return shutil.which("brew") or "brew"


def run_real_brew(args: list[str]) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    result = subprocess.run([_find_real_brew()] + args,
                            env={**os.environ, "SAFE_BREW_ACTIVE": "1"})
    sys.exit(result.returncode)


# ─── Result printer ───────────────────────────────────────────────────────────

def _print_result(r: dict, label: str = "") -> bool:
    """Print one check result. Returns True if this result blocks the install."""
    tag = f" ({label})" if label else ""
    name = r["name"]
    ver = r.get("version", "?")

    if "error" in r:
        print(f"❌  {name}{tag}: {r['error']}", file=sys.stderr)
        return True
    if "rate_limited" in r:
        print(f"❌  {name}=={ver}{tag}: {r['rate_limited']}", file=sys.stderr)
        return True
    if "date_unknown" in r:
        print(f"⚠️   {name}=={ver}{tag}: {r['reason']} — allowing")
        return False
    if not r["safe"]:
        fb = r.get("fallback")
        print(
            f"⬇️   {name}=={ver} ({r['pub_date']}, {r['age_days']}d old){tag} — too new",
            file=sys.stderr if fb is None else sys.stdout,
        )
        if fb:
            fb_ver, _, fb_date = fb
            print(f"     📌 pinned:  {name}=={fb_ver} ({fb_date.strftime('%Y-%m-%d')})")
        else:
            print(f"❌  {name}{tag}: no safe fallback found — all versions >{SAFE_AGE_DAYS}d "
                  f"are too new", file=sys.stderr)
        return fb is None  # block only if no fallback
    print(f"✅  {name}=={ver} ({r['pub_date']}, {r['age_days']}d old){tag}")
    return False


# ─── Install handler ──────────────────────────────────────────────────────────

def handle_install(formulae: list[str], flags: list[str], is_cask: bool) -> None:
    if not formulae:
        run_real_brew(["install"] + flags)
        return

    kind = "cask" if is_cask else "formula"
    print(f"\n[safe-brew] Checking {len(formulae)} {kind}(s) — "
          f"only versions >{SAFE_AGE_DAYS} days old allowed\n")

    # ── Check top-level formulae ──────────────────────────────────────────────
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(check_formula, name, is_cask): name for name in formulae}
        for fut in as_completed(futures):
            name = futures[fut]
            results[name] = fut.result()

    blocked = False
    for name in formulae:
        if _print_result(results[name]):
            blocked = True

    if blocked:
        print(f"\n[safe-brew] ❌  Install blocked. Use --unsafe to bypass.\n", file=sys.stderr)
        sys.exit(1)

    # Build the actual install list: substitute raw URLs where needed.
    # For safe formulas we keep the name for now; if any dep turns out to need
    # pinning we'll upgrade names → raw URLs below (so brew can't re-resolve deps).
    install_targets: list[str] = []
    for name in formulae:
        r = results[name]
        if not r.get("safe") and r.get("fallback"):
            _, raw_url, _ = r["fallback"]
            install_targets.append(raw_url)
        else:
            install_targets.append(name)  # may be promoted to raw_url later

    # ── Resolve and check transitive dependencies ─────────────────────────────
    print(f"\n[safe-brew] Resolving dependencies...")
    try:
        # Use the is_cask that check_formula resolved (it auto-detects casks).
        # Tap names (contain '/') are unauditable via the Homebrew JSON API — skip them.
        top_level = [
            (name, results[name]["is_cask"])
            for name in formulae
            if "/" not in name
        ]
        all_deps = collect_transitive_deps(top_level)
    except RuntimeError as e:
        print(f"\n[safe-brew] ❌  {e}\n", file=sys.stderr)
        sys.exit(1)

    if all_deps:
        installed = get_installed_brew_packages()
        new_deps = [(n, c) for n, c in all_deps if n.lower() not in installed]

        if new_deps:
            print(f"[safe-brew] Checking {len(new_deps)} new "
                  f"dependenc{'y' if len(new_deps) == 1 else 'ies'}...\n")
            dep_results: dict[tuple, dict] = {}
            with ThreadPoolExecutor(max_workers=8) as ex:
                futures2 = {ex.submit(check_formula, n, c): (n, c) for n, c in new_deps}
                for fut in as_completed(futures2):
                    key = futures2[fut]
                    dep_results[key] = fut.result()

            dep_extra_targets: list[str] = []
            dep_blocked = False
            for n, c in new_deps:
                r = dep_results[(n, c)]
                if _print_result(r, label="dep"):
                    dep_blocked = True
                elif not r.get("safe") and r.get("fallback"):
                    _, raw_url, _ = r["fallback"]
                    dep_extra_targets.append(raw_url)

            if dep_blocked:
                print(
                    f"\n[safe-brew] ❌  Install blocked: dependency too new and no safe "
                    f"fallback found. Use --unsafe to bypass.\n",
                    file=sys.stderr,
                )
                sys.exit(1)

            # When deps need pinning, also promote any name-based parent targets to
            # raw URLs.  If we leave parents as plain names, brew resolves their deps
            # from homebrew-core at install time and ignores our dep raw URLs.
            # A safe parent's intro_date is always < CUTOFF < too-new dep intro_date
            # (by definition), so the safe version already predates the dep's update —
            # no further downgrade is needed, just an explicit URL.
            if dep_extra_targets:
                install_targets = [
                    results[name].get("raw_url") or target
                    if (target == name)   # only promote the un-substituted ones
                    else target
                    for name, target in zip(formulae, install_targets)
                ]

            install_targets += dep_extra_targets
        else:
            print("[safe-brew] All dependencies already installed — skipping dep audit.")

    # Use the is_cask resolved by check_formula (auto-detection may have changed it)
    resolved_types = {n: results[n].get("is_cask", is_cask) for n in formulae}
    has_formula = any(not v for v in resolved_types.values())
    has_cask = any(v for v in resolved_types.values())
    if has_formula and has_cask:
        mixed = ", ".join(
            f"{n}({'cask' if resolved_types[n] else 'formula'})" for n in formulae
        )
        print(
            f"\n[safe-brew] ❌  Cannot mix formulas and casks in one install command: {mixed}\n"
            f"           Run them separately: brew install <formulas> and brew install --cask <casks>\n",
            file=sys.stderr,
        )
        sys.exit(1)
    effective_is_cask = has_cask

    # brew refuses URL/local-path cask installs — they must come from a tap.
    # If any top-level cask target is a raw URL, use a temporary local tap.
    top_targets = install_targets[:len(formulae)]
    dep_targets = install_targets[len(formulae):]

    if effective_is_cask and any(t.startswith("https://") for t in top_targets):
        _install_casks_via_tap(list(zip(formulae, top_targets)), dep_targets, flags)
        return  # _install_casks_via_tap calls sys.exit internally

    cask_flags = ["--cask"] if effective_is_cask else []
    print(f"\n[safe-brew] Running: brew install {' '.join(cask_flags + install_targets + flags)}\n")
    run_real_brew(["install"] + cask_flags + install_targets + flags)


# ─── Management subcommands ───────────────────────────────────────────────────

def _cmd_disable():
    os.makedirs(INSTALL_DIR, exist_ok=True)
    with open(DISABLED_FLAG, "w"):
        pass
    print("[safe-brew] ✅  Disabled — safety checks bypassed until you run: safe-brew enable")


def _cmd_enable():
    if os.path.exists(DISABLED_FLAG):
        os.remove(DISABLED_FLAG)
        print("[safe-brew] ✅  Enabled — safety checks are active.")
    else:
        print("[safe-brew] Already enabled.")


def _cmd_status():
    state = "🔴 DISABLED" if os.path.exists(DISABLED_FLAG) else "🟢 ENABLED"
    print(f"[safe-brew] Status: {state}")
    print(f"           Age threshold: {SAFE_AGE_DAYS} days (SAFE_BREW_AGE_DAYS)")
    if GITHUB_TOKEN:
        print("           GitHub: authenticated (GITHUB_TOKEN set — 5000 req/hr)")
    else:
        print("           GitHub: unauthenticated (60 req/hr — set GITHUB_TOKEN for more)")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if args and args[0] == "disable":
        _cmd_disable(); return
    if args and args[0] == "enable":
        _cmd_enable(); return
    if args and args[0] == "status":
        _cmd_status(); return

    if os.path.exists(DISABLED_FLAG):
        run_real_brew(args)
        return

    if not args:
        run_real_brew([])
        return

    if os.environ.get("SAFE_BREW_ACTIVE"):
        run_real_brew(args)
        return

    if "--unsafe" in args:
        print("[safe-brew] ⚠️   --unsafe detected — skipping safety checks!\n")
        run_real_brew([a for a in args if a != "--unsafe"])
        return

    # Skip any leading global brew options (e.g. `brew --verbose install jq`)
    # so we don't miss the install subcommand.
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 1
    if i >= len(args) or args[i] != "install":
        run_real_brew(args)
        return

    # Collect the global flags we skipped, plus everything after "install"
    global_flags = args[:i]
    rest = args[i + 1:]

    # --HEAD bypasses the version-age model entirely — block it.
    if "--HEAD" in rest:
        print(
            "[safe-brew] ❌  --HEAD installs are not allowed: HEAD builds have no stable "
            "version date and bypass supply-chain age checks.\n"
            "           Use --unsafe to override.",
            file=sys.stderr,
        )
        sys.exit(1)

    is_cask = "--cask" in rest or "--casks" in rest

    # Flags that consume the next token as their value — those tokens must not
    # be treated as package names.
    _VALUE_FLAGS = {
        "--appdir", "--caskroom", "--language",  # cask install options that take a value
        "--cc",                                  # formula: compiler override (e.g. --cc gcc-14)
    }

    flags: list[str] = global_flags
    formulae: list[str] = []
    j = 0
    while j < len(rest):
        tok = rest[j]
        if tok in _VALUE_FLAGS and j + 1 < len(rest):
            flags.extend([tok, rest[j + 1]])
            j += 2
        elif tok.startswith("-"):
            # --cask/--casks tracked via is_cask; don't add to flags (handle_install adds it)
            if tok not in ("--cask", "--casks"):
                flags.append(tok)
            j += 1
        else:
            formulae.append(tok)
            j += 1

    handle_install(formulae, flags, is_cask)


if __name__ == "__main__":
    main()
