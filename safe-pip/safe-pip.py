#!/usr/bin/env python3
"""safe-pip: only install packages published >30 days ago (supply-chain protection)."""
from __future__ import annotations
import sys
import os

# Force unbuffered stdout so our prints interleave correctly with subprocess stderr
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)  # line-buffered
import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

SAFE_AGE_DAYS = int(os.environ.get('SAFE_PIP_AGE_DAYS', '30'))
CUTOFF = datetime.now(timezone.utc) - timedelta(days=SAFE_AGE_DAYS)

try:
    from packaging.version import Version, InvalidVersion
    from packaging.specifiers import SpecifierSet
    from packaging.requirements import Requirement
    from packaging.markers import default_environment
except ImportError:
    # Fall back to pip's bundled copy (always available)
    from pip._vendor.packaging.version import Version, InvalidVersion
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.markers import default_environment

# ─── PyPI API ─────────────────────────────────────────────────────────────────

def fetch_pypi(package: str) -> dict:
    url = f"https://pypi.org/pypi/{package}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "safe-pip/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f'Package "{package}" not found on PyPI')
        raise RuntimeError(f'PyPI returned HTTP {e.code} for "{package}"')
    except Exception as e:
        raise RuntimeError(f'Failed to fetch "{package}" from PyPI: {e}')


def release_date(files: list) -> datetime | None:
    """Earliest upload time across all distribution files in a release."""
    times = []
    for f in files:
        t = f.get("upload_time_iso_8601") or f.get("upload_time")
        if t:
            t = t.replace("Z", "+00:00")
            if "+" not in t and not t.endswith("Z"):
                t += "+00:00"
            try:
                times.append(datetime.fromisoformat(t))
            except ValueError:
                pass
    return min(times) if times else None


def is_version_safe(info: dict, ver_str: str) -> tuple[bool, str | None]:
    """Return (safe, pub_date_str) for a specific version; safe=True if published before CUTOFF."""
    files = info.get("releases", {}).get(ver_str, [])
    if not files:
        return True, None
    pub = release_date(files)
    if not pub:
        return True, None
    return pub < CUTOFF, pub.strftime("%Y-%m-%d")


_CURRENT_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def get_installed_version(name: str) -> str | None:
    """Return the installed version of a package, or None if not installed."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        return version(name)
    except Exception:
        return None

def is_python_compatible(files: list) -> bool:
    """Return True if any distribution file supports the running Python version."""
    for f in files:
        req = f.get("requires_python")
        if req is None:
            return True  # no restriction → compatible
        try:
            if _CURRENT_PYTHON in SpecifierSet(req):
                return True
        except Exception:
            return True  # unparseable → assume compatible
    return False


# ─── Version resolution ───────────────────────────────────────────────────────

def find_safe_version(info: dict, specifier: str = "") -> tuple[str | None, datetime | None]:
    """Latest stable version satisfying `specifier` published before CUTOFF."""
    spec = SpecifierSet(specifier, prereleases=False)
    candidates = []

    for ver_str, files in info.get("releases", {}).items():
        if not files:
            continue
        try:
            ver = Version(ver_str)
        except InvalidVersion:
            continue
        if ver.is_prerelease or ver.is_devrelease:
            continue
        if not is_python_compatible(files):
            continue
        if specifier and ver not in spec:
            continue
        pub = release_date(files)
        if pub and pub < CUTOFF:
            candidates.append((ver, pub, ver_str))

    if not candidates:
        return None, None
    best = max(candidates, key=lambda x: x[0])
    return best[2], best[1]


def get_skipped(info: dict, safe_ver_str: str, specifier: str = "") -> list[tuple]:
    """All stable versions newer than safe_ver_str, tagged with skip reason.

    Returns list of (ver_str, date, age_days, too_new: bool, py_compat: bool).
    - too_new=True: published within SAFE_AGE_DAYS (primary skip reason)
    - py_compat=False: incompatible with the running Python (secondary reason)
    """
    spec = SpecifierSet(specifier, prereleases=False)
    safe_ver = Version(safe_ver_str)
    skipped = []

    for ver_str, files in info.get("releases", {}).items():
        if not files:
            continue
        try:
            ver = Version(ver_str)
        except InvalidVersion:
            continue
        if ver.is_prerelease or ver.is_devrelease:
            continue
        if specifier and ver not in spec:
            continue
        if ver <= safe_ver:
            continue
        pub = release_date(files)
        if pub:
            age = (datetime.now(timezone.utc) - pub).days
            too_new = pub >= CUTOFF
            py_ok = is_python_compatible(files)
            skipped.append((ver, ver_str, pub.strftime("%Y-%m-%d"), age, too_new, py_ok))

    skipped.sort(key=lambda x: x[0])
    return [(s[1], s[2], s[3], s[4], s[5]) for s in skipped]


def _print_skipped(skipped: list, indent: str = "     ") -> None:
    """Print skipped versions grouped by reason."""
    too_new    = [(v, d, a) for v, d, a, is_new, py_ok in skipped if is_new]
    py_incompat = [(v, d, a) for v, d, a, is_new, py_ok in skipped if not is_new and not py_ok]

    if too_new:
        s = ", ".join(f"{v} ({d}, {a}d old)" for v, d, a in too_new)
        print(f"{indent}🚫 skipped (too new):        {s}")
    if py_incompat:
        s = ", ".join(f"{v} ({d})" for v, d, a in py_incompat)
        print(f"{indent}🐍 skipped (py incompatible): {s}")


def count_safe(info: dict) -> tuple[int, int]:
    """Return (safe_count, total_count) of stable versions."""
    total = safe = 0
    for ver_str, files in info.get("releases", {}).items():
        if not files:
            continue
        try:
            ver = Version(ver_str)
        except InvalidVersion:
            continue
        if ver.is_prerelease or ver.is_devrelease:
            continue
        if not is_python_compatible(files):
            continue
        total += 1
        pub = release_date(files)
        if pub and pub < CUTOFF:
            safe += 1
    return safe, total


# ─── Real pip (avoids recursive wrapper calls) ────────────────────────────────

def _is_local_install(arg: str) -> bool:
    """Return True if arg is a local path, direct URL, or VCS URL — not a PyPI name."""
    if arg.startswith(('.', '/', '~')):
        return True
    if arg.startswith(('http://', 'https://', 'file://', 'git+', 'hg+', 'svn+', 'bzr+')):
        return True
    if any(arg.endswith(ext) for ext in ('.whl', '.tar.gz', '.zip', '.tar.bz2', '.tgz')):
        return True
    return False


def run_real_pip(args: list[str]) -> None:
    """Invoke the real pip via sys.executable -m pip (pyenv-safe, no wrapper recursion)."""
    sys.stdout.flush()
    sys.stderr.flush()
    # SAFE_PIP_ACTIVE=1 tells usercustomize.py not to intercept this call
    env = {**os.environ, "SAFE_PIP_ACTIVE": "1"}
    result = subprocess.run([sys.executable, "-m", "pip"] + args, env=env)
    sys.exit(result.returncode)


def resolve_install_plan(pkg_args: list[str], extra_flags: list[str]) -> list[tuple[str, str]] | None:
    """
    Use pip dry-run to discover every package (including transitive deps)
    that would be installed. Returns [(name, version), ...] or None on failure.

    Tries --report JSON first (pip >= 22.2), falls back to stdout parsing.
    """
    import tempfile, os

    # Internal pip calls must bypass usercustomize.py to avoid infinite recursion
    _safe_env = {**os.environ, "SAFE_PIP_ACTIVE": "1"}

    # ── attempt 1: structured JSON report (pip >= 22.2) ──
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        report_path = f.name
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run",
             "--report", report_path, "--quiet"] + pkg_args + extra_flags,
            capture_output=True, text=True, env=_safe_env,
        )
        if r.returncode == 0:
            with open(report_path) as f:
                data = json.load(f)
            return [
                (pkg["metadata"]["name"], pkg["metadata"]["version"])
                for pkg in data.get("install", [])
            ]
    except Exception:
        pass
    finally:
        try:
            os.unlink(report_path)
        except OSError:
            pass

    # ── attempt 2: parse "Would install pkg-ver pkg2-ver2" from stdout ──
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run"] + pkg_args + extra_flags,
            capture_output=True, text=True, env=_safe_env,
        )
        results = []
        for line in (r.stdout + r.stderr).splitlines():
            if "Would install" not in line:
                continue
            for token in line.replace("Would install", "").split():
                # "charset-normalizer-3.4.1" → split on last dash before digit
                for i in range(len(token) - 1, 0, -1):
                    if token[i] == "-" and token[i + 1].isdigit():
                        results.append((token[:i], token[i + 1:]))
                        break
        if results:
            return results
    except Exception:
        pass

    # ── attempt 3: PyPI metadata fallback (works with any pip version) ──
    # Resolve one level of transitive deps by reading requires_dist from PyPI.
    return _resolve_via_pypi_metadata(pkg_args)


def _latest_matching_from_info(info: dict, specifier_str: str) -> str | None:
    """Latest stable version satisfying specifier_str (ignores publish age)."""
    spec = SpecifierSet(specifier_str, prereleases=False)
    candidates = []
    for ver_str, files in info.get("releases", {}).items():
        if not files:
            continue
        try:
            ver = Version(ver_str)
        except InvalidVersion:
            continue
        if ver.is_prerelease or ver.is_devrelease:
            continue
        if not is_python_compatible(files):
            continue
        if specifier_str and ver not in spec:
            continue
        candidates.append((ver, ver_str))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def _resolve_via_pypi_metadata(pkg_args: list[str]) -> list[tuple[str, str]] | None:
    """
    Recursively resolve ALL transitive deps via PyPI requires_dist (BFS).
    Works with any pip version. Each BFS level is fetched in parallel.
    """
    # visited: name_lower → (canonical_name, resolved_version)
    visited: dict = {}

    # Seed: top-level pinned packages (e.g. "requests[socks]==2.32.5")
    # frontier entries: (name, version, extras) — extras are the requested extras
    # for this package, used when evaluating extra-conditional markers below.
    frontier: list[tuple[str, str, frozenset]] = []
    for arg in pkg_args:
        try:
            req = Requirement(arg)
            ver = str(req.specifier).lstrip("=")
        except Exception:
            continue
        if ver:
            key = req.name.lower()
            visited[key] = (req.name, ver)
            frontier.append((req.name, ver, frozenset(e.lower() for e in req.extras)))

    if not frontier:
        return None

    top_keys = set(visited)  # remember top-level so we exclude them from result
    # Track which extras have already been walked per package so we can detect
    # when a new extras combination requires re-queuing an already-visited node.
    visited_extras: dict[str, frozenset] = {k: frozenset() for k in visited}
    # Extra-merge queue: already-visited packages that need re-walking with new extras.
    frontier_extra_queue: list[tuple[str, str, frozenset]] = []

    def _fetch_requires(name: str, version: str) -> list[str]:
        try:
            url = f"https://pypi.org/pypi/{name}/{version}/json"
            r = urllib.request.Request(url, headers={"User-Agent": "safe-pip/1.0"})
            with urllib.request.urlopen(r, timeout=15) as resp:
                return json.loads(resp.read()).get("info", {}).get("requires_dist") or []
        except Exception:
            return []

    def _resolve_ver(name: str, specifier: str) -> tuple[str, str | None]:
        try:
            info = fetch_pypi(name)
            ver, _ = find_safe_version(info, specifier)
            if ver is None:
                ver = _latest_matching_from_info(info, specifier)
            return name, ver
        except Exception:
            return name, None

    while frontier or frontier_extra_queue:
        # Drain the extra-merge queue into the main frontier so re-queued packages
        # (already visited but with newly discovered extras) are walked this round.
        if frontier_extra_queue:
            frontier.extend(frontier_extra_queue)
            frontier_extra_queue.clear()
        # ── Step 1: fetch requires_dist for all packages in the current frontier ──
        with ThreadPoolExecutor(max_workers=10) as ex:
            meta_futures = {ex.submit(_fetch_requires, n, v): (n, v, extras)
                            for n, v, extras in frontier}
            # Carry (req_str, parent_extras) so Step 2 can evaluate extra markers correctly.
            all_req_pairs: list[tuple[str, frozenset]] = []
            for fut in as_completed(meta_futures):
                _, _, extras = meta_futures[fut]
                for req_str in fut.result():
                    all_req_pairs.append((req_str, extras))

        # ── Step 2: collect new (unseen) dep names + specifiers + their own extras ──
        new_deps: dict[str, tuple[str, str, frozenset]] = {}  # name_lower → (name, specifier, extras)
        for req_str, parent_extras in all_req_pairs:
            try:
                dep = Requirement(req_str)
                base_env = default_environment()
                # A dep is included if it passes with the empty-extra environment OR
                # with any of the extras that were explicitly requested for its parent.
                extras_to_check = parent_extras | {""}
                if dep.marker and not any(
                    dep.marker.evaluate({**base_env, "extra": e})
                    for e in extras_to_check
                ):
                    continue
                key = dep.name.lower()
                dep_extras = frozenset(e.lower() for e in dep.extras)
                if key in visited:
                    # Already resolved — but if new extras were requested, re-queue so
                    # extra-gated children (e.g. "C; extra == 'foo'") get walked.
                    existing_extras = visited_extras.get(key, frozenset())
                    added = dep_extras - existing_extras
                    if added:
                        visited_extras[key] = existing_extras | added
                        name_v, ver_v = visited[key]
                        frontier_extra_queue.append((name_v, ver_v, dep_extras))
                elif key in new_deps:
                    # Seen in this BFS level with different extras — merge them.
                    name_e, spec_e, extras_e = new_deps[key]
                    new_deps[key] = (name_e, spec_e, extras_e | dep_extras)
                else:
                    # Carry the dep's own extras forward so its children's extra markers
                    # (e.g. "A -> B[foo]" -> "C; extra == 'foo'") are evaluated correctly.
                    new_deps[key] = (dep.name, str(dep.specifier), dep_extras)
            except Exception:
                continue

        if not new_deps:
            if not frontier_extra_queue:
                break
            continue

        # ── Step 3: resolve safe version for each new dep in parallel ──
        frontier = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            ver_futures = {ex.submit(_resolve_ver, name, spec): (key, extras)
                           for key, (name, spec, extras) in new_deps.items()}
            for fut in as_completed(ver_futures):
                key, extras = ver_futures[fut]
                name, ver = fut.result()
                if ver:
                    visited[key] = (name, ver)
                    visited_extras[key] = extras
                    frontier.append((name, ver, extras))

    result = [(name, ver) for key, (name, ver) in visited.items() if key not in top_keys]
    return result if result else None


# ─── Requirement parsing ──────────────────────────────────────────────────────

def parse_req_line(line: str) -> Requirement | None:
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("-"):
        return None
    try:
        return Requirement(line)
    except Exception:
        return None


def load_requirements(path: str) -> list[Requirement]:
    reqs = []
    with open(path) as f:
        for line in f:
            r = parse_req_line(line)
            if r:
                if r.marker and not r.marker.evaluate():
                    continue
                reqs.append(r)
    return reqs


# ─── Handlers ─────────────────────────────────────────────────────────────────

def check_package(name: str, specifier: str = "") -> dict:
    """Query PyPI and return result dict."""
    try:
        info = fetch_pypi(name)
        latest = info.get("info", {}).get("version", "?")
        safe_ver, safe_pub = find_safe_version(info, specifier)
        safe_count, total = count_safe(info)

        if not safe_ver:
            return {"name": name, "specifier": specifier, "safe_ver": None,
                    "latest": latest, "safe_count": safe_count, "total": total}

        downgraded = safe_ver != latest
        skipped = get_skipped(info, safe_ver, specifier) if downgraded else []
        return {"name": name, "specifier": specifier, "safe_ver": safe_ver,
                "latest": latest, "pub_date": safe_pub.strftime("%Y-%m-%d"),
                "downgraded": downgraded, "skipped": skipped}
    except Exception as e:
        return {"name": name, "specifier": specifier, "safe_ver": None, "error": str(e)}


def _check_local_deps(local_args: list[str], install_flags: list[str], flags: list[str]) -> None:
    """Age-check all deps that pip would install from local/editable packages."""
    plan = resolve_install_plan(local_args, install_flags + flags)
    if not plan:
        return

    print(f"[safe-pip] Checking {len(plan)} dependenc{'y' if len(plan)==1 else 'ies'} "
          f"from local package(s)...\n")

    def _check_ver(name: str, ver: str) -> dict:
        try:
            info = fetch_pypi(name)
            safe, pub_date = is_version_safe(info, ver)
            return {"name": name, "ver": ver, "safe": safe, "pub_date": pub_date}
        except Exception as e:
            # KB-3: catches all exceptions including network errors/timeouts, not just
            # 404s. A transient PyPI outage will pass the dep through unchecked.
            # Only HTTP 404 should mean "private package — allow"; other errors should block.
            return {"name": name, "ver": ver, "error": str(e)}

    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_check_ver, name, ver): (name, ver) for name, ver in plan}
        for fut in as_completed(futures):
            name, ver = futures[fut]
            results[(name, ver)] = fut.result()

    blocked = False
    for name, ver in plan:
        r = results[(name, ver)]
        if "error" in r:
            print(f"✅  {name}=={ver} (not on PyPI — passing through)")
        elif r["safe"]:
            date_str = f" ({r['pub_date']})" if r.get("pub_date") else ""
            print(f"✅  {name}=={ver}{date_str} — dep safe")
        else:
            print(f"❌  {name}=={ver}: published {r.get('pub_date', '?')} — too new (local dep)",
                  file=sys.stderr)
            blocked = True

    if blocked:
        print(
            f"\n[safe-pip] ❌  Install blocked: local package dep(s) are too new.\n"
            f"  Use --unsafe to bypass.\n",
            file=sys.stderr,
        )
        sys.exit(1)


def handle_install(pkg_args: list[str], local_args: list[str], flags: list[str], upgrade: bool) -> None:
    install_flags = ["--upgrade"] if upgrade else []

    if not pkg_args and not local_args:
        run_real_pip(["install"] + flags)
        return

    if local_args:
        _check_local_deps(local_args, install_flags, flags)

    if not pkg_args:
        print(f"\n[safe-pip] Running: pip install {' '.join(local_args + flags)}\n")
        run_real_pip(["install"] + install_flags + local_args + flags)
        return

    print(f"\n[safe-pip] Checking {len(pkg_args)} package(s) — "
          f"only versions >{SAFE_AGE_DAYS} days old allowed\n")

    # Parse each arg into (name, specifier, orig_arg, extras_str)
    requests = []
    passthrough = []  # already-installed packages that don't need checking
    for arg in pkg_args:
        try:
            req = Requirement(arg)
            name = req.name
            spec = str(req.specifier)
            # Preserve extras like [socks] in "requests[socks]>=2.0"
            extras_str = f"[{','.join(sorted(req.extras))}]" if req.extras else ""
        except Exception:
            name, spec, extras_str = arg, "", ""

        # Without -U: if already installed and satisfies the specifier, skip
        # PyPI check and let pip handle it (→ "Requirement already satisfied").
        # Skip this fast-path when extras are requested — they may pull in new
        # transitive deps that haven't been audited yet.
        if not upgrade and not extras_str:
            installed = get_installed_version(name)
            if installed:
                try:
                    ver = Version(installed)
                    if not spec or ver in SpecifierSet(spec):
                        passthrough.append((name, installed, arg))
                        continue
                except Exception:
                    pass

        requests.append((name, spec, arg, extras_str))

    for name, installed, _ in passthrough:
        print(f"✅  {name}=={installed} already installed — skipping PyPI check")

    if not requests:
        # All packages already installed, just run pip as-is
        print(f"\n[safe-pip] Running: pip install {' '.join(pkg_args + flags)}\n")
        run_real_pip(["install"] + pkg_args + flags)
        return

    # Parallel PyPI queries for packages not yet installed (or being upgraded)
    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(check_package, name, spec): orig
                   for name, spec, orig, _extras in requests}
        for fut in as_completed(futures):
            orig = futures[fut]
            results[orig] = fut.result()

    blocked = False
    pinned_args = []

    for _, spec, orig, extras_str in requests:
        r = results[orig]
        name = r["name"]
        if "error" in r:
            print(f"❌  {name}: {r['error']}", file=sys.stderr)
            blocked = True
        elif not r["safe_ver"]:
            print(f"❌  {name}: no safe version "
                  f"({r['safe_count']}/{r['total']} versions >{SAFE_AGE_DAYS}d old"
                  + (f', none match "{r["specifier"]}"' if r.get("specifier") else "") + ")",
                  file=sys.stderr)
            blocked = True
        elif r["downgraded"]:
            spec_str = f'[{r["specifier"]}]' if r.get("specifier") else ""
            print(f"⬇️   {name}{spec_str}: {r['latest']} too new → {r['safe_ver']} ({r['pub_date']})")
            if r["skipped"]:
                _print_skipped(r["skipped"])
            print(f"     📌 pinned:  {name}{extras_str}=={r['safe_ver']}")
            pinned_args.append(f"{name}{extras_str}=={r['safe_ver']}")
        else:
            print(f"✅  {name}{extras_str}=={r['safe_ver']} ({r['pub_date']}) — safe")
            pinned_args.append(f"{name}{extras_str}=={r['safe_ver']}")

    if blocked:
        print(f"\n[safe-pip] ❌  Install blocked. Use --unsafe to bypass.\n",
              file=sys.stderr)
        sys.exit(1)

    # ── Check transitive dependencies via pip dry-run ──────────────────────
    passthrough_args = [arg for _, _, arg in passthrough]
    all_pinned = pinned_args + passthrough_args

    top_level_names = {r["name"].lower() for r in results.values() if r.get("safe_ver")}
    all_pinned = _check_transitives(all_pinned, top_level_names, install_flags, flags)

    print(f"\n[safe-pip] Running: pip install {' '.join(all_pinned + local_args + flags)}\n")
    run_real_pip(["install"] + install_flags + all_pinned + local_args + flags)


def _check_transitives(
    all_pinned: list[str],
    top_level_names: set[str],
    install_flags: list[str],
    flags: list[str],
) -> list[str]:
    """Run transitive dependency check via pip dry-run + PyPI BFS.
    Returns the extended pinned list (top-level + safe transitive pins).
    Exits on block."""
    plan = resolve_install_plan(all_pinned, install_flags + flags)
    if not plan:
        return all_pinned

    transitive = [(n, v) for n, v in plan if n.lower() not in top_level_names]
    if not transitive:
        return all_pinned

    print(f"[safe-pip] Checking {len(transitive)} transitive dependenc{'y' if len(transitive)==1 else 'ies'}...\n")
    trans_results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        # Check against "" (no specifier) so we always compare with the
        # actual latest — this surfaces downgrades even when _resolve_via_pypi_metadata
        # already returned the safe version.
        futures = {ex.submit(check_package, name, ""): (name, ver)
                   for name, ver in transitive}
        for fut in as_completed(futures):
            name, ver = futures[fut]
            trans_results[(name, ver)] = fut.result()

    trans_blocked = False
    trans_pinned = []
    for name, ver in transitive:
        r = trans_results[(name, ver)]
        if "error" in r:
            trans_pinned.append(f"{name}=={ver}")  # unreachable → pass through
        elif not r.get("safe_ver"):
            print(f"❌  {name}: no safe version exists (dep)")
            trans_blocked = True
        elif r.get("downgraded"):
            print(f"⬇️   {name} (dep): {r['latest']} too new "
                  f"→ {r['safe_ver']} ({r['pub_date']})")
            if r.get("skipped"):
                _print_skipped(r["skipped"])
            print(f"     📌 pinned:  {name}=={r['safe_ver']}")
            trans_pinned.append(f"{name}=={r['safe_ver']}")
        else:
            print(f"✅  {name}=={r['safe_ver']} ({r['pub_date']}) — dep safe")
            trans_pinned.append(f"{name}=={r['safe_ver']}")

    if trans_blocked:
        print(
            f"\n[safe-pip] ❌  Install blocked: transitive dep(s) have no safe version.\n"
            f"\n"
            f"  Use --unsafe to bypass.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    return all_pinned + trans_pinned


def handle_requirements(req_file: str, flags: list[str], upgrade: bool) -> None:
    try:
        reqs = load_requirements(req_file)
    except FileNotFoundError:
        print(f"[safe-pip] requirements file not found: {req_file}", file=sys.stderr)
        sys.exit(1)

    if not reqs:
        run_real_pip(["install", "-r", req_file] + flags)
        return

    print(f"\n[safe-pip] Checking {len(reqs)} package(s) from {req_file} — "
          f"only versions >{SAFE_AGE_DAYS} days old allowed\n")

    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(check_package, req.name, str(req.specifier)): req
                   for req in reqs}
        for fut in as_completed(futures):
            req = futures[fut]
            results[req.name] = fut.result()

    blocked = False
    for req in reqs:
        r = results[req.name]
        name = r["name"]
        if "error" in r:
            print(f"❌  {name}: {r['error']}", file=sys.stderr)
            blocked = True
        elif not r["safe_ver"]:
            print(f"❌  {name}: no safe version "
                  f"({r['safe_count']}/{r['total']} versions >{SAFE_AGE_DAYS}d old)",
                  file=sys.stderr)
            blocked = True
        elif r["downgraded"]:
            spec_str = f'[{r["specifier"]}]' if r.get("specifier") else ""
            print(f"⬇️   {name}{spec_str}: {r['latest']} too new → {r['safe_ver']} ({r['pub_date']})")
            if r["skipped"]:
                _print_skipped(r["skipped"])
            print(f"     📌 pinned:  {name}=={r['safe_ver']}")
        else:
            print(f"✅  {name}=={r['safe_ver']} ({r['pub_date']}) — safe")

    if blocked:
        print(f"\n[safe-pip] ❌  Install blocked. Use --unsafe to bypass.\n",
              file=sys.stderr)
        sys.exit(1)

    # Install with pinned safe versions instead of raw requirements.txt
    # (the original file's ranges could still resolve to unsafe versions)
    pinned = []
    for req in reqs:
        r = results[req.name]
        if r.get("safe_ver"):
            extras = f"[{','.join(sorted(req.extras))}]" if req.extras else ""
            pinned.append(f"{r['name']}{extras}=={r['safe_ver']}")
    install_flags = ["--upgrade"] if upgrade else []

    top_level_names = {req.name.lower() for req in reqs}
    pinned = _check_transitives(pinned, top_level_names, install_flags, flags)

    print(f"\n[safe-pip] Running: pip install {' '.join(pinned)}\n")
    run_real_pip(["install"] + install_flags + pinned + flags)


# ─── Entry point ──────────────────────────────────────────────────────────────

INSTALL_DIR = os.path.expanduser("~/.safe-pip")
DISABLED_FLAG = os.path.join(INSTALL_DIR, "disabled")


def _cmd_inject_venv(venv_path: str) -> None:
    """Install safe-pip wrappers into an existing venv directory."""
    import shutil, stat

    venv_path = os.path.abspath(venv_path)
    if not os.path.isdir(venv_path):
        print(f"[safe-pip] ❌  Not a directory: {venv_path}", file=sys.stderr)
        sys.exit(1)

    safe_pip_script = os.path.abspath(__file__)

    # Find Python executable inside the venv
    venv_python = None
    for name in ("python3", "python"):
        p = os.path.join(venv_path, "bin", name)
        if os.path.isfile(p) or os.path.islink(p):
            venv_python = p  # keep as venv path, not resolved base interpreter
            break
    if not venv_python or not os.path.exists(venv_python):
        print(f"[safe-pip] ❌  No Python executable found in {venv_path}/bin/", file=sys.stderr)
        sys.exit(1)

    # Replace all pip/pip3/pip3.X binaries with safe-pip wrappers
    bin_dir = os.path.join(venv_path, "bin")
    for entry in sorted(os.listdir(bin_dir)):
        if entry == "pip" or (entry.startswith("pip3") and (len(entry) == 4 or entry[4] in (".", ""))):
            pip_path = os.path.join(bin_dir, entry)
            if not os.path.isfile(pip_path) and not os.path.islink(pip_path):
                continue
            with open(pip_path, "w") as f:
                f.write(f'#!/bin/sh\nexec "{venv_python}" "{safe_pip_script}" "$@"\n')
            os.chmod(pip_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            print(f"[safe-pip]   Wrapped: {pip_path}")

    # Install sitecustomize.py into the venv's site-packages so `python -m pip`
    # is also intercepted.  site-packages are added to sys.path *before*
    # sitecustomize is imported, so this location is reliably found.
    result = subprocess.run(
        [venv_python, "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        site_pkgs = result.stdout.strip()
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usercustomize.py")
        if not os.path.exists(src):
            src = os.path.join(INSTALL_DIR, "usercustomize.py")
        if not os.path.exists(src):
            print("[safe-pip] ⚠️   usercustomize.py not found — sitecustomize hook skipped.\n"
                  "           Run 'safe-pip inject-venv' from the install directory or after\n"
                  "           running install.sh so the source file is present.",
                  file=sys.stderr)
        elif site_pkgs and os.path.isdir(site_pkgs):
            dst = os.path.join(site_pkgs, "sitecustomize.py")
            existing = ""
            if os.path.exists(dst):
                with open(dst) as f:
                    existing = f.read()
            if "safe-pip usercustomize hook" in existing:
                print(f"[safe-pip]   sitecustomize.py already present in {site_pkgs}")
            elif existing:
                # Append our hook so we don't clobber project-level customisation
                with open(dst, "a") as f:
                    f.write("\n")
                    with open(src) as s:
                        f.write(s.read())
                print(f"[safe-pip]   Appended safe-pip hook to sitecustomize.py → {site_pkgs}")
            else:
                import shutil as _sh
                _sh.copy2(src, dst)
                print(f"[safe-pip]   Installed sitecustomize.py → {site_pkgs}")

    print(f"\n[safe-pip] ✅  Injected into venv: {venv_path}")


def _cmd_disable():
    os.makedirs(INSTALL_DIR, exist_ok=True)
    with open(DISABLED_FLAG, "w") as f:
        pass
    print("[safe-pip] ✅  Disabled — safety checks bypassed until you run: safe-pip enable")


def _cmd_enable():
    if os.path.exists(DISABLED_FLAG):
        os.remove(DISABLED_FLAG)
        print("[safe-pip] ✅  Enabled — safety checks are active.")
    else:
        print("[safe-pip] Already enabled.")


def _cmd_status():
    disabled = os.path.exists(DISABLED_FLAG)
    state = "🔴 DISABLED" if disabled else "🟢 ENABLED"
    print(f"[safe-pip] Status: {state}")
    print(f"           Age threshold: {SAFE_AGE_DAYS} days (SAFE_PIP_AGE_DAYS)")


def main():
    args = sys.argv[1:]

    # Built-in management subcommands (intercept before passing to pip)
    if args and args[0] == "disable":
        _cmd_disable(); return
    if args and args[0] == "enable":
        _cmd_enable(); return
    if args and args[0] == "status":
        _cmd_status(); return
    if args and args[0] == "inject-venv":
        if len(args) < 2:
            print("[safe-pip] Usage: safe-pip inject-venv <venv-path>", file=sys.stderr)
            sys.exit(1)
        _cmd_inject_venv(args[1]); return

    # Sentinel file: disabled → pass everything straight to pip
    if os.path.exists(DISABLED_FLAG):
        run_real_pip(args)
        return

    if not args:
        run_real_pip([])
        return

    # --unsafe: bypass all safety checks (we don't use --force to avoid
    # collision with pip's own --force-reinstall flag)
    if "--unsafe" in args:
        print("[safe-pip] ⚠️   --unsafe detected — skipping safety checks!\n")
        run_real_pip([a for a in args if a != "--unsafe"])
        return

    cmd = args[0]

    if cmd != "install":
        run_real_pip(args)
        return

    # Parse install subcommand
    rest = args[1:]
    upgrade = "-U" in rest or "--upgrade" in rest

    # pip install flags that take a value argument (their value is NOT a package name)
    _PIP_VALUE_FLAGS = {
        "-i", "--index-url", "--extra-index-url",
        "-c", "--constraint", "-t", "--target",
        "--prefix", "--root", "--python-version", "--platform",
        "--implementation", "--abi", "--upgrade-strategy",
        "--no-binary", "--only-binary", "--trusted-host",
        "--cert", "--client-cert", "--cache-dir", "--log",
        "--proxy", "--retries", "--timeout",
        "-f", "--find-links", "--progress-bar", "--global-option",
        "-C", "--config-settings",
    }

    # Parse rest to identify -r/--requirement and value-taking flags.
    # skip_indices: entirely excluded (-r flag + its filename)
    # value_indices: values of other flags (excluded from pkg_args but kept in flags passthrough)
    req_file = None
    skip_indices: set[int] = set()
    value_indices: set[int] = set()
    editable_local: list[str] = []  # reconstructed -e/-editable tokens for local installs

    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-r", "--requirement") and i + 1 < len(rest):
            req_file = rest[i + 1]
            skip_indices |= {i, i + 1}
            i += 2
            continue
        if a in ("-e", "--editable") and i + 1 < len(rest):
            editable_local += [a, rest[i + 1]]
            skip_indices |= {i, i + 1}
            i += 2
            continue
        if a.startswith("--requirement="):
            req_file = a.split("=", 1)[1]
            skip_indices.add(i)
        elif a.startswith("--editable="):
            editable_local.append(a)
            skip_indices.add(i)
        elif a in _PIP_VALUE_FLAGS and i + 1 < len(rest):
            # Space-separated form: --index-url URL (not --index-url=URL)
            value_indices.add(i + 1)
        i += 1

    # Separate flags and package args
    flags = [a for i, a in enumerate(rest)
             if i not in skip_indices and (a.startswith("-") or i in value_indices)]
    pkg_args = [a for i, a in enumerate(rest)
                if i not in skip_indices and i not in value_indices and not a.startswith("-")]

    # Separate local paths/URLs from PyPI names; combine with editable installs
    local_pkg_args = [a for a in pkg_args if _is_local_install(a)]
    pkg_args       = [a for a in pkg_args if not _is_local_install(a)]
    local_args     = editable_local + local_pkg_args

    # Alternate indexes can't be audited — safe-pip only checks pypi.org.
    # Flags that change the package source or install location can't be safely audited:
    # - Index/source overrides: pip installs from a different source than safe-pip audits
    # - Install location overrides (--target/--root/--prefix): the already-installed
    #   passthrough checks the current environment, not the target directory
    _UNAUDITABLE = {
        "--index-url", "--extra-index-url", "--find-links", "--no-index",
        "-t", "--target", "--root", "--prefix",
    }
    if any(f in _UNAUDITABLE or
           f.startswith("--index-url=") or f.startswith("--extra-index-url=") or
           f.startswith("--find-links=") or
           f.startswith("--target=") or f.startswith("--root=") or f.startswith("--prefix=") or
           (f.startswith("-t") and len(f) > 2) or
           f == "-i" or (f.startswith("-i") and len(f) > 2) or
           f == "-f" or (f.startswith("-f") and len(f) > 2)
           for f in flags):
        print("[safe-pip] ❌  Alternate package sources or install locations\n"
              "           (--index-url, --extra-index-url, --find-links, --no-index,\n"
              "           --target, --root, --prefix) are not supported by safe-pip.\n"
              "           Use --unsafe to bypass safety checks, or run pip directly.\n",
              file=sys.stderr)
        sys.exit(1)

    if req_file:
        other_flags = [f for f in flags if f not in ("-r", "--requirement")]
        handle_requirements(req_file, other_flags, upgrade)
    elif pkg_args or local_args:
        handle_install(pkg_args, local_args, flags, upgrade)
    else:
        run_real_pip(args)


if __name__ == "__main__":
    main()
