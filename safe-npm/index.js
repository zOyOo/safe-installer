#!/usr/bin/env node
'use strict';

const https   = require('https');
const { spawn } = require('child_process');
const fs      = require('fs');
const os      = require('os');
const path    = require('path');
const semver  = require('semver');

// ─── Config ───────────────────────────────────────────────────────────────────

let SAFE_AGE_DAYS  = parseInt(process.env.SAFE_NPM_AGE_DAYS || '30', 10);
let CUTOFF         = new Date(Date.now() - SAFE_AGE_DAYS * 24 * 60 * 60 * 1000);

const MIN_AGE_DAYS = 2;

/**
 * Parse and strip --min-age=N or --min-age N from argv.
 * Updates SAFE_AGE_DAYS and CUTOFF if the flag is present.
 * Returns the argv with the flag removed.
 */
function applyMinAgeFlag(argv) {
  const out = [];
  let pastDashDash = false;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--') { pastDashDash = true; out.push(a); continue; }
    let val = null;
    if (!pastDashDash && a.startsWith('--min-age=')) {
      val = a.slice('--min-age='.length);
    } else if (!pastDashDash && a === '--min-age' && i + 1 < argv.length) {
      val = argv[++i];
    } else {
      out.push(a);
      continue;
    }
    const n = parseInt(val, 10);
    if (!Number.isInteger(n) || n < MIN_AGE_DAYS) {
      console.error(`[safe-npm] ❌  --min-age must be an integer >= ${MIN_AGE_DAYS} (got: ${val})`);
      process.exit(1);
    }
    SAFE_AGE_DAYS = n;
    CUTOFF = new Date(Date.now() - n * 24 * 60 * 60 * 1000);
  }
  return out;
}
const INSTALL_DIR    = path.join(process.env.HOME || process.env.USERPROFILE, '.safe-npm');
const DISABLED_FLAG  = path.join(INSTALL_DIR, 'disabled');

const INSTALL_CMDS = new Set(['install', 'i', 'add']);
const UPDATE_CMDS  = new Set(['update', 'up', 'upgrade']);

// ─── Registry ─────────────────────────────────────────────────────────────────

function fetchPackageInfo(name) {
  return new Promise((resolve, reject) => {
    // Scoped packages: @scope/pkg → @scope%2Fpkg
    const urlPath = name.startsWith('@')
      ? `/${name.replace('/', '%2F')}`
      : `/${name}`;

    const req = https.get({
      hostname: 'registry.npmjs.org',
      path: urlPath,
      headers: { 'User-Agent': 'safe-npm/1.0', Accept: 'application/json' },
    }, (res) => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => {
        if (res.statusCode === 404) return reject(new Error(`Package "${name}" not found`));
        if (res.statusCode !== 200) return reject(new Error(`Registry ${res.statusCode} for "${name}"`));
        try { resolve(JSON.parse(Buffer.concat(chunks).toString())); }
        catch { reject(new Error(`Bad JSON from registry for "${name}"`)); }
      });
    });
    req.on('error', reject);
  });
}

// ─── Version resolution ───────────────────────────────────────────────────────

/**
 * Parse "lodash@^4" or "@scope/pkg@1.x" into { name, range }.
 */
function parsePackageArg(arg) {
  if (arg.startsWith('@')) {
    // e.g. @babel/core or @babel/core@^7
    const m = arg.match(/^(@[^/]+\/[^@]+)(?:@(.+))?$/);
    if (!m) return { name: arg, range: '*' };
    return { name: m[1], range: m[2] || '*' };
  }
  const at = arg.indexOf('@');
  if (at === -1) return { name: arg, range: '*' };
  return { name: arg.slice(0, at), range: arg.slice(at + 1) || '*' };
}

/**
 * Find the highest version satisfying `range` that was published before CUTOFF.
 * Returns null if no such version exists.
 */
function findSafeVersion(info, range) {
  const time     = info.time || {};
  const versions = Object.keys(info.versions || {});
  const r        = (!range || range === 'latest') ? '*' : range;

  const safe = versions
    .filter(v => semver.valid(v) && !semver.prerelease(v))
    .filter(v => time[v] && new Date(time[v]) < CUTOFF)
    .filter(v => r === '*' || semver.satisfies(v, r));

  return semver.maxSatisfying(safe, '*');
}

/**
 * Count how many of a package's versions fall before CUTOFF.
 */
function countSafeVersions(info) {
  const time = info.time || {};
  return Object.keys(info.versions || {})
    .filter(v => semver.valid(v) && !semver.prerelease(v))
    .filter(v => time[v] && new Date(time[v]) < CUTOFF)
    .length;
}

/**
 * Find the latest stable version satisfying `range` (ignores publish age).
 */
function findLatestMatching(info, range) {
  const time     = info.time || {};
  const versions = Object.keys(info.versions || {});
  const r        = (!range || range === 'latest') ? '*' : range;
  const candidates = versions
    .filter(v => semver.valid(v) && !semver.prerelease(v))
    .filter(v => time[v])
    .filter(v => r === '*' || semver.satisfies(v, r));
  return semver.maxSatisfying(candidates, '*');
}

/**
 * Fetch dependencies for a specific package version from the registry.
 * Uses the compact per-version endpoint to avoid downloading the full manifest.
 */
function fetchVersionDeps(name, version) {
  return new Promise((resolve) => {
    const urlPath = name.startsWith('@')
      ? `/${name.replace('/', '%2F')}/${version}`
      : `/${name}/${version}`;
    const req = https.get({
      hostname: 'registry.npmjs.org',
      path: urlPath,
      headers: { 'User-Agent': 'safe-npm/1.0', Accept: 'application/json' },
    }, (res) => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => {
        try {
          const data = JSON.parse(Buffer.concat(chunks).toString());
          // Include dependencies, optionalDependencies, and peerDependencies
          // (npm v7+ installs all three automatically)
          resolve({
            ...data.dependencies,
            ...data.optionalDependencies,
            ...data.peerDependencies,
          });
        } catch { resolve({}); }
      });
    });
    req.on('error', () => resolve({}));
  });
}

/**
 * BFS over npm registry to resolve ALL transitive deps.
 * Returns [{ name, safeVer, latestVer, publishDate, downgraded, skipped }]
 * for every transitive dep (top-level packages excluded).
 *
 * Each BFS level is fetched in parallel.
 */
async function resolveTransitiveDeps(topLevelPinned) {
  // visited: name → version (the safe/best version we resolved)
  const visited   = new Map();
  const topNames  = new Set();
  let   frontier  = [];

  for (const { name, safeVer } of topLevelPinned) {
    visited.set(name.toLowerCase(), { version: safeVer, isNpmAlias: false });
    topNames.add(name.toLowerCase());
    frontier.push({ name, version: safeVer });
  }

  while (frontier.length > 0) {
    // Step 1: fetch deps for all packages in the current frontier in parallel
    const depsPerPkg = await Promise.all(
      frontier.map(({ name, version }) => fetchVersionDeps(name, version))
    );

    // Step 2: collect new unseen dep names + ranges
    const newDeps = new Map(); // name_lower → { name, range }
    for (const deps of depsPerPkg) {
      for (const [depName, range] of Object.entries(deps)) {
        const key = depName.toLowerCase();
        if (!visited.has(key) && !newDeps.has(key)) {
          newDeps.set(key, { name: depName, range });
        }
      }
    }

    if (newDeps.size === 0) break;

    // Step 3: resolve safe version for each new dep in parallel
    frontier = [];
    await Promise.all(Array.from(newDeps.values()).map(async ({ name, range }) => {
      const key = name.toLowerCase();
      try {
        // Handle npm: aliases e.g. "npm:@scope/pkg@1.2.3"
        // These are exact-version pins — check the real package but don't install explicitly.
        const isNpmAlias = typeof range === 'string' && range.startsWith('npm:');
        let pkgName = name, pkgRange = range;
        if (isNpmAlias) {
          ({ name: pkgName, range: pkgRange } = parsePackageArg(range.slice(4)));
        }

        const info = await fetchPackageInfo(pkgName);

        // For exact pre-release pins (platform packages like 1.0.0-darwin-arm64)
        // fall back to checking that version directly via time field.
        let safeVer = findSafeVersion(info, pkgRange) || findLatestMatching(info, pkgRange);
        if (!safeVer && semver.valid(pkgRange) && (info.time || {})[pkgRange]) {
          safeVer = pkgRange;
        }

        if (safeVer) {
          // Store { version, isNpmAlias } so caller knows not to pin alias packages explicitly
          visited.set(key, { version: safeVer, isNpmAlias });
          if (!isNpmAlias) {
            frontier.push({ name: pkgName, version: safeVer });
          }
        }
      } catch { /* skip unreachable packages */ }
    }));
  }

  // Build result list (exclude top-level packages)
  const results = [];
  for (const [key, { version, isNpmAlias }] of visited) {
    if (topNames.has(key)) continue;
    results.push({ name: key, version, isNpmAlias });
  }
  return results;
}

// ─── npm subprocess ───────────────────────────────────────────────────────────

// Use SAFE_NPM_REAL / SAFE_NPX_REAL to avoid calling our own wrappers recursively.
const REAL_NPM = process.env.SAFE_NPM_REAL || 'npm';
const REAL_NPX = process.env.SAFE_NPX_REAL || 'npx';

// All npm/npx spawns set SAFE_NPM_ACTIVE=1 to prevent re-entry if npm/npx
// in PATH is our wrapper (which would call safe-npm again recursively).
const safeEnv = { ...process.env, SAFE_NPM_ACTIVE: '1' };

function runCmd(bin, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, args, { stdio: 'inherit', env: safeEnv });
    child.on('close', code => { process.exitCode = code ?? 0; resolve(); });
    child.on('error', reject);
  });
}

function runNpm(args) { return runCmd(REAL_NPM, args); }
function runNpx(args) { return runCmd(REAL_NPX, args); }

// Silent npm — suppresses stdout, used for --package-lock-only resolution phase.
function runNpmSilent(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(REAL_NPM, args, { stdio: ['inherit', 'ignore', 'inherit'], env: safeEnv });
    child.on('close', code => resolve(code ?? 0));
    child.on('error', reject);
  });
}

// Silent npm in a specific directory — used for global install temp-dir resolution.
function runNpmSilentInDir(cwd, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(REAL_NPM, args, { stdio: ['inherit', 'ignore', 'inherit'], env: safeEnv, cwd });
    child.on('close', code => resolve(code ?? 0));
    child.on('error', reject);
  });
}

// ─── Utilities ────────────────────────────────────────────────────────────────

const isFlag = a => a.startsWith('-');

// npm install/update flags that take a value argument (so values aren't mistaken for packages)
const NPM_INSTALL_VALUE_FLAGS = new Set([
  '--workspace', '-w', '--prefix', '--registry', '--tag', '--otp',
  '--userconfig', '--globalconfig', '--cache', '--proxy', '--https-proxy',
  '--noproxy', '--cert', '--key', '--cafile', '--depth', '--maxsockets',
  '--location',
]);

/**
 * Split args into { flags, pkgArgs }, correctly handling value-taking flags.
 * e.g. ['--workspace', 'app', 'lodash'] → flags=['--workspace','app'], pkgArgs=['lodash']
 */
function splitFlagsAndPkgs(args) {
  const flags = [];
  const pkgArgs = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a.startsWith('-')) {
      flags.push(a);
      // Space-separated value: --flag value (not --flag=value)
      const bare = a.includes('=') ? null : a;
      if (bare && NPM_INSTALL_VALUE_FLAGS.has(bare) && i + 1 < args.length && !args[i + 1].startsWith('-')) {
        flags.push(args[++i]);
      }
    } else {
      pkgArgs.push(a);
    }
  }
  return { flags, pkgArgs };
}

/**
 * Detect and strip global install flags (-g, --global, --location=global, --location global).
 * Returns { isGlobal, strippedFlags } where strippedFlags has global flags removed.
 */
function extractGlobalFlag(flags) {
  const stripped = [];
  let isGlobal = false;
  for (let i = 0; i < flags.length; i++) {
    const f = flags[i];
    if (f === '-g' || f === '--global' || f === '--location=global') {
      isGlobal = true;
    } else if (f === '--location' && flags[i + 1] === 'global') {
      isGlobal = true;
      i++; // skip the 'global' value token
    } else {
      stripped.push(f);
    }
  }
  return { isGlobal, strippedFlags: stripped };
}

function readJson(filePath) {
  try { return JSON.parse(fs.readFileSync(filePath, 'utf8')); }
  catch { return null; }
}

function daysAgo(isoDate) {
  return Math.floor((Date.now() - new Date(isoDate)) / 86400000);
}

/**
 * Extract all { name, version } entries from a parsed package-lock.json.
 * Works with both lockfileVersion 1 (dependencies) and 2/3 (packages).
 */
function extractLockfilePackages(lockJson) {
  if (!lockJson) return [];
  const out = [];
  if (lockJson.packages) {
    for (const [key, val] of Object.entries(lockJson.packages)) {
      if (!key || !val.version) continue; // skip root entry ("")
      // For nested deps like "node_modules/a/node_modules/b", take the last segment
      const parts = key.split('node_modules/');
      out.push({ name: parts[parts.length - 1], version: val.version });
    }
  } else if (lockJson.dependencies) {
    const collect = (deps) => {
      for (const [name, info] of Object.entries(deps)) {
        if (info.version) out.push({ name, version: info.version });
        if (info.dependencies) collect(info.dependencies);
      }
    };
    collect(lockJson.dependencies);
  }
  return out;
}

/**
 * Check a list of { name, version } against the registry.
 * Returns an array of unsafe entries: { name, version, age, date }.
 * Batches requests to avoid registry rate limits.
 */
async function auditPackageList(packages, { silent = false } = {}) {
  const BATCH = 25;
  const unsafe = [];
  let checked = 0;

  for (let i = 0; i < packages.length; i += BATCH) {
    const batch = packages.slice(i, i + BATCH);
    const results = await Promise.all(batch.map(async ({ name, version }) => {
      try {
        const info = await fetchPackageInfo(name);
        const pub  = info.time?.[version];
        if (!pub) return null;
        const date = new Date(pub);
        if (date >= CUTOFF) {
          return { name, version, age: daysAgo(pub), date: date.toISOString().slice(0, 10) };
        }
        return null;
      } catch { return null; }
    }));
    unsafe.push(...results.filter(Boolean));
    checked += batch.length;
    if (!silent) process.stdout.write(`  [${checked}/${packages.length}]...\r`);
  }
  if (!silent) process.stdout.write('\n');
  return unsafe;
}

// ─── Handlers ─────────────────────────────────────────────────────────────────

/**
 * safe-npm install <pkg...>
 * Finds the latest safe version of each requested package, then delegates to npm.
 * If isGlobal is true, lockfile resolution happens in a temp dir (npm ignores
 * --package-lock-only for global installs), then the actual install uses -g.
 */
async function handleInstallWithPackages(pkgArgs, flags, { isGlobal = false } = {}) {
  console.log(`\n[safe-npm] Checking ${pkgArgs.length} package(s) — only versions >${SAFE_AGE_DAYS} days old allowed\n`);

  const results = await Promise.all(pkgArgs.map(async (arg) => {
    const { name, range } = parsePackageArg(arg);
    try {
      const info     = await fetchPackageInfo(name);
      const latest   = info['dist-tags']?.latest;
      const safeVer  = findSafeVersion(info, range);
      const total    = Object.keys(info.versions || {}).filter(v => semver.valid(v) && !semver.prerelease(v)).length;
      const safeCount = countSafeVersions(info);

      if (!safeVer) {
        return { name, range, safeVer: null, latest, safeCount, total };
      }

      const publishDate = new Date(info.time[safeVer]).toISOString().slice(0, 10);
      const downgraded  = latest && safeVer !== latest;

      // Collect versions newer than safeVer that were skipped for being too recent
      const r = (!range || range === 'latest') ? '*' : range;
      const skipped = Object.keys(info.versions || {})
        .filter(v => semver.valid(v) && !semver.prerelease(v))
        .filter(v => info.time[v] && new Date(info.time[v]) >= CUTOFF)
        .filter(v => r === '*' || semver.satisfies(v, r))
        .filter(v => semver.gt(v, safeVer))
        .sort(semver.compare)
        .map(v => `${v} (${new Date(info.time[v]).toISOString().slice(0, 10)}, ${daysAgo(info.time[v])}d old)`);

      return { name, range, safeVer, latest, publishDate, downgraded, skipped };
    } catch (e) {
      return { name, range, safeVer: null, fetchError: e.message };
    }
  }));

  let blocked = false;

  for (const r of results) {
    if (r.fetchError) {
      console.error(`❌  ${r.name}: ${r.fetchError}`);
      blocked = true;
    } else if (!r.safeVer) {
      console.error(`❌  ${r.name}: no safe version (${r.safeCount}/${r.total} versions are >${SAFE_AGE_DAYS} days old, none match "${r.range}")`);
      blocked = true;
    } else if (r.downgraded) {
      console.log(`⚠️   ${r.name}@${r.range}: latest=${r.latest} too new → pinning to ${r.safeVer} (${r.publishDate})`);
      if (r.skipped?.length > 0) {
        console.log(`     skipped: ${r.skipped.join(', ')}`);
      }
    } else {
      console.log(`✅  ${r.name}@${r.safeVer} (${r.publishDate}) — safe`);
    }
  }

  if (blocked) {
    console.error(`\n[safe-npm] ❌  Install blocked. Use --unsafe to bypass (not recommended).\n`);
    process.exit(1);
  }

  const pinnedArgs = results.map(r => `${r.name}@${r.safeVer}`);
  let allPinned  = [...pinnedArgs]; // will grow with transitive pins

  // ── Phase 1: BFS over registry — informational display + collect safe pins ──
  const transitive = await resolveTransitiveDeps(results.filter(r => r.safeVer));

  if (transitive.length > 0) {
    console.log(`\n[safe-npm] Checking ${transitive.length} transitive dependenc${transitive.length === 1 ? 'y' : 'ies'}...\n`);

    const transResults = await Promise.all(transitive.map(async ({ name, version, isNpmAlias }) => {
      try {
        const info      = await fetchPackageInfo(name);
        const latest    = info['dist-tags']?.latest;
        const safeVer   = findSafeVersion(info, '*');
        const latestVer = findLatestMatching(info, '*');

        if (!safeVer) {
          return { name, version, safeVer: null, latest };
        }

        const publishDate = new Date(info.time[safeVer]).toISOString().slice(0, 10);
        const downgraded  = latestVer && safeVer !== latestVer;
        const skipped = downgraded
          ? Object.keys(info.versions || {})
              .filter(v => semver.valid(v) && !semver.prerelease(v))
              .filter(v => info.time[v] && new Date(info.time[v]) >= CUTOFF)
              .filter(v => semver.gt(v, safeVer))
              .sort(semver.compare)
              .map(v => `${v} (${new Date(info.time[v]).toISOString().slice(0, 10)}, ${daysAgo(info.time[v])}d old)`)
          : [];

        return { name, version, safeVer, latestVer, publishDate, downgraded, skipped, isNpmAlias };
      } catch {
        // Registry unreachable — warn but don't block (npm will handle resolution)
        return { name, version, safeVer: version, publishDate: '?', downgraded: false, skipped: [], isNpmAlias, unchecked: true };
      }
    }));

    let transBlocked = false;
    const transPinned = [];

    for (const r of transResults) {
      if (!r.safeVer) {
        console.error(`❌  ${r.name} (dep): no safe version exists`);
        transBlocked = true;
      } else if (r.downgraded) {
        console.log(`⬇️   ${r.name} (dep): ${r.latestVer} too new → ${r.safeVer} (${r.publishDate})`);
        if (r.skipped?.length) {
          console.log(`     🚫 skipped (too new): ${r.skipped.join(', ')}`);
        }
        if (!r.isNpmAlias) {
          console.log(`     📌 pinned:  ${r.name}@${r.safeVer}`);
          transPinned.push(`${r.name}@${r.safeVer}`);
        }
      } else if (r.unchecked) {
        console.warn(`⚠️   ${r.name}@${r.version} (dep): registry unreachable, could not verify`);
      } else {
        const alias = r.isNpmAlias ? ' (alias)' : '';
        console.log(`✅  ${r.name}@${r.safeVer} (${r.publishDate}) — dep safe${alias}`);
        if (!r.isNpmAlias) transPinned.push(`${r.name}@${r.safeVer}`);
      }
    }

    if (transBlocked) {
      console.error(`\n[safe-npm] ❌  Install blocked: transitive dep(s) have no safe version.\n`);
      process.exit(1);
    }
    allPinned = [...allPinned, ...transPinned];
  }

  // ── Phase 2: Resolve full dep tree into a lockfile ──────────────────────────
  // For local installs: resolve in cwd using --package-lock-only (writes lockfile
  // + package.json), back up both so we can restore on failure.
  // For global installs: npm ignores --package-lock-only with -g, so resolve in a
  // temp directory instead — no cwd files are touched.
  let lockPath, lockBackup = null, pkgBackup = null, tmpDir = null;

  if (isGlobal) {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'safe-npm-global-'));
    fs.writeFileSync(
      path.join(tmpDir, 'package.json'),
      JSON.stringify({ name: 'safe-npm-audit-tmp', version: '1.0.0', private: true })
    );
    lockPath = path.join(tmpDir, 'package-lock.json');

    console.log(`\n[safe-npm] Resolving full dependency tree in temp dir...`);
    // Pass only top-level pinned packages — npm's own resolver handles transitive
    // resolution with correct platform filtering (e.g. excludes darwin packages on linux).
    // Phase 3 then audits the full platform-correct lockfile npm produces.
    const resolveCode = await runNpmSilentInDir(tmpDir, ['install', ...pinnedArgs, ...flags, '--package-lock-only']);
    if (resolveCode !== 0) {
      fs.rmSync(tmpDir, { recursive: true, force: true });
      process.exit(resolveCode);
    }
  } else {
    const pkgPath = path.join(process.cwd(), 'package.json');
    lockPath  = path.join(process.cwd(), 'package-lock.json');
    lockBackup = fs.existsSync(lockPath) ? fs.readFileSync(lockPath) : null;
    pkgBackup  = fs.existsSync(pkgPath)  ? fs.readFileSync(pkgPath)  : null;

    console.log(`\n[safe-npm] Resolving full dependency tree (npm --package-lock-only)...`);
    const resolveCode = await runNpmSilent(['install', ...allPinned, ...flags, '--package-lock-only']);
    if (resolveCode !== 0) {
      if (lockBackup) fs.writeFileSync(lockPath, lockBackup);
      else if (fs.existsSync(lockPath)) fs.unlinkSync(lockPath);
      if (pkgBackup) fs.writeFileSync(path.join(process.cwd(), 'package.json'), pkgBackup);
      process.exit(resolveCode);
    }
  }

  // ── Phase 3: Audit the lockfile npm just wrote ───────────────────────────────
  const lockJson = readJson(lockPath);
  const allLockPkgs = extractLockfilePackages(lockJson);
  console.log(`[safe-npm] Auditing all ${allLockPkgs.length} resolved packages...\n`);

  const unsafe = await auditPackageList(allLockPkgs);

  if (unsafe.length > 0) {
    if (tmpDir) {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    } else {
      const pkgPath = path.join(process.cwd(), 'package.json');
      if (lockBackup) fs.writeFileSync(lockPath, lockBackup);
      else if (fs.existsSync(lockPath)) fs.unlinkSync(lockPath);
      if (pkgBackup) fs.writeFileSync(pkgPath, pkgBackup);
    }

    console.error(`\n[safe-npm] ❌  Lockfile audit found ${unsafe.length} unsafe package(s):\n`);
    for (const p of unsafe) {
      console.error(`  ${p.name}@${p.version} — only ${p.age}d old (${p.date})`);
    }
    console.error(`\n[safe-npm] Install blocked. Use --unsafe to bypass.\n`);
    process.exit(1);
  }

  // ── Phase 4: Actual install ──────────────────────────────────────────────────
  console.log(`[safe-npm] ✅  All ${allLockPkgs.length} packages passed. Installing...\n`);
  if (isGlobal) {
    // Temp dir no longer needed — clean up before the real install
    fs.rmSync(tmpDir, { recursive: true, force: true });
    // Pass only the top-level pinned packages. Transitive enforcement is not possible
    // for global installs: explicitly passing transitive pins causes EBADPLATFORM for
    // platform-specific optional packages (e.g. @img/sharp-darwin-arm64 on linux) because
    // npm lockfiles don't record the libc constraint — only os/cpu — so we can't fully
    // filter them. The full transitive tree was audited in Phase 3, and npm will re-resolve
    // the same versions in Phase 4 using its own platform-aware logic.
    await runNpm(['install', '-g', ...pinnedArgs, ...flags]);
  } else {
    // lockfile + package.json already written correctly — install from lockfile
    await runNpm(['install', ...flags]);
  }
}

/**
 * safe-npm install  (no packages — restore from package.json / lock file)
 * Audits ALL packages in the lockfile (direct + transitive) before installing.
 * If no lockfile exists, lets npm resolve first (--package-lock-only), audits, then installs.
 */
async function handleInstallNoPackages(flags) {
  const lockPath = path.join(process.cwd(), 'package-lock.json');
  let lockJson = readJson(lockPath);

  // No lockfile yet — let npm resolve fresh, then audit before installing
  if (!lockJson) {
    const pkgJson = readJson(path.join(process.cwd(), 'package.json'));
    if (!pkgJson) { await runNpm(['install', ...flags]); return; }

    console.log(`\n[safe-npm] No lock file found. Resolving dependency tree...`);
    const code = await runNpmSilent(['install', ...flags, '--package-lock-only']);
    if (code !== 0) {
      if (fs.existsSync(lockPath)) fs.unlinkSync(lockPath);
      process.exit(code);
    }
    lockJson = readJson(lockPath);
    if (!lockJson) { await runNpm(['install', ...flags]); return; }
  }

  const packages = extractLockfilePackages(lockJson);
  if (packages.length === 0) { await runNpm(['install', ...flags]); return; }

  console.log(`\n[safe-npm] Auditing all ${packages.length} packages in lock file...\n`);
  const unsafe = await auditPackageList(packages);

  if (unsafe.length > 0) {
    console.error(`\n[safe-npm] ❌  ${unsafe.length} recently-published package(s) found in lock file:\n`);
    for (const p of unsafe) {
      console.error(`  ${p.name}@${p.version} — only ${p.age}d old (published ${p.date})`);
    }
    console.error(`\n[safe-npm] Install blocked. Use --unsafe to bypass.\n`);
    process.exit(1);
  }

  console.log(`[safe-npm] ✅  All ${packages.length} packages are safe.\n`);
  await runNpm(['install', ...flags]);
}

/**
 * safe-npm update [pkg...]
 * Finds the latest safe version within each dep's semver range and installs it.
 */
async function handleUpdate(pkgArgs, flags) {
  const pkgJson = readJson(path.join(process.cwd(), 'package.json'));
  const allDeps = pkgJson
    ? { ...pkgJson.dependencies, ...pkgJson.devDependencies, ...pkgJson.optionalDependencies }
    : {};

  let targets;
  if (pkgArgs.length > 0) {
    // Explicit packages: update them in all selected workspaces using their declared ranges.
    targets = pkgArgs.map(p => ({ name: p, range: allDeps[p] || '*' }));
  } else {
    targets = Object.entries(allDeps).map(([name, range]) => ({ name, range }));
  }

  if (targets.length === 0) {
    console.log('[safe-npm] No packages to update.');
    return;
  }

  console.log(`\n[safe-npm] Finding safe updates for ${targets.length} package(s)...\n`);

  const results = await Promise.all(targets.map(async ({ name, range }) => {
    try {
      const info      = await fetchPackageInfo(name);
      const safeVer   = findSafeVersion(info, range);
      const safeCount = countSafeVersions(info);
      const total     = Object.keys(info.versions || {}).filter(v => semver.valid(v) && !semver.prerelease(v)).length;
      if (!safeVer) return { name, range, safeVer: null, safeCount, total };
      const date = new Date(info.time[safeVer]).toISOString().slice(0, 10);
      return { name, range, safeVer, date };
    } catch (e) {
      return { name, range, safeVer: null, fetchError: e.message };
    }
  }));

  const good = results.filter(r => r.safeVer);
  const bad  = results.filter(r => !r.safeVer);

  for (const r of good) console.log(`✅  ${r.name} → ${r.safeVer} (${r.date})`);
  for (const r of bad) {
    if (r.fetchError) console.error(`❌  ${r.name}: ${r.fetchError}`);
    else console.warn(`⚠️   ${r.name}: no safe version in range "${r.range}" (${r.safeCount}/${r.total} safe)`);
  }

  if (good.length === 0) {
    console.error('\n[safe-npm] No packages can be safely updated.\n');
    process.exit(1);
  }

  const installArgs = good.map(r => `${r.name}@${r.safeVer}`);

  // Two-phase: resolve into lockfile, audit, then install
  const lockPath = path.join(process.cwd(), 'package-lock.json');
  const pkgPath  = path.join(process.cwd(), 'package.json');
  const lockBackup = fs.existsSync(lockPath) ? fs.readFileSync(lockPath) : null;
  const pkgBackup  = fs.existsSync(pkgPath)  ? fs.readFileSync(pkgPath)  : null;

  console.log(`\n[safe-npm] Resolving updated dependency tree...`);
  const code = await runNpmSilent(['install', ...installArgs, ...flags, '--package-lock-only']);
  if (code !== 0) {
    if (lockBackup) fs.writeFileSync(lockPath, lockBackup);
    else if (fs.existsSync(lockPath)) fs.unlinkSync(lockPath);
    if (pkgBackup) fs.writeFileSync(pkgPath, pkgBackup);
    process.exit(code);
  }

  const lockJson = readJson(lockPath);
  const allPkgs = extractLockfilePackages(lockJson);
  console.log(`[safe-npm] Auditing all ${allPkgs.length} resolved packages...\n`);

  const unsafe = await auditPackageList(allPkgs);

  if (unsafe.length > 0) {
    if (lockBackup) fs.writeFileSync(lockPath, lockBackup);
    else if (fs.existsSync(lockPath)) fs.unlinkSync(lockPath);
    if (pkgBackup) fs.writeFileSync(pkgPath, pkgBackup);

    console.error(`\n[safe-npm] ❌  Lockfile audit found ${unsafe.length} unsafe package(s):\n`);
    for (const p of unsafe) {
      console.error(`  ${p.name}@${p.version} — only ${p.age}d old (${p.date})`);
    }
    console.error(`\n[safe-npm] Update blocked. Use --unsafe to bypass.\n`);
    process.exit(1);
  }

  console.log(`[safe-npm] ✅  All ${allPkgs.length} packages passed. Installing updates...\n`);
  await runNpm(['install', ...flags]);
}

/**
 * safe-npm ci
 * Strict audit of ALL packages in package-lock.json before running npm ci.
 */
async function handleCi() {
  const lockJson = readJson(path.join(process.cwd(), 'package-lock.json'));
  if (!lockJson) {
    console.error('[safe-npm] No package-lock.json found.');
    process.exit(1);
  }

  const packages = extractLockfilePackages(lockJson);
  console.log(`\n[safe-npm] Auditing all ${packages.length} packages in lock file...\n`);

  const unsafe = await auditPackageList(packages);

  if (unsafe.length > 0) {
    console.error(`\n[safe-npm] ❌  ${unsafe.length} recently-published package(s) found:\n`);
    for (const p of unsafe) {
      console.error(`  ${p.name}@${p.version} — only ${p.age}d old (${p.date})`);
    }
    console.error(`\n[safe-npm] ci blocked. Use --unsafe to bypass.\n`);
    process.exit(1);
  }

  console.log(`[safe-npm] ✅  All ${packages.length} packages passed the safety check.\n`);
  await runNpm(['ci']);
}

// ─── safe-npx handler ─────────────────────────────────────────────────────────

/**
 * Audit a single npx package specifier (used for both positional and -p/--package values).
 * Returns the pinned specifier (e.g. "pkg@1.2.3") or exits on block.
 */
async function auditNpxSpec(spec) {
  const { name, range } = parsePackageArg(spec);
  const info      = await fetchPackageInfo(name);
  const latest    = info['dist-tags']?.latest;
  const safeVer   = findSafeVersion(info, range);
  const safeCount = countSafeVersions(info);
  const total     = Object.keys(info.versions || {}).filter(v => semver.valid(v) && !semver.prerelease(v)).length;

  if (!safeVer) {
    console.error(`❌  ${name}: no safe version (${safeCount}/${total} versions >${SAFE_AGE_DAYS}d old)`);
    console.error(`\n[safe-npx] ❌  Blocked. Use --unsafe to bypass.\n`);
    process.exit(1);
  }

  const publishDate = new Date(info.time[safeVer]).toISOString().slice(0, 10);
  const downgraded  = latest && safeVer !== latest;

  if (downgraded) {
    const skipped = Object.keys(info.versions || {})
      .filter(v => semver.valid(v) && !semver.prerelease(v))
      .filter(v => info.time[v] && new Date(info.time[v]) >= CUTOFF)
      .filter(v => semver.gt(v, safeVer))
      .sort(semver.compare)
      .map(v => `${v} (${new Date(info.time[v]).toISOString().slice(0, 10)}, ${daysAgo(info.time[v])}d old)`);
    console.log(`⬇️   ${name}: ${latest} too new → ${safeVer} (${publishDate})`);
    if (skipped.length) console.log(`     🚫 skipped (too new): ${skipped.join(', ')}`);
    console.log(`     📌 pinned: ${name}@${safeVer}`);
  } else {
    console.log(`✅  ${name}@${safeVer} (${publishDate}) — safe`);
  }
  return `${name}@${safeVer}`;
}

/**
 * safe-npx [flags] <pkg>[@range] [args...]
 * safe-npx -p <pkg> [-p <pkg>...] [-c <cmd>] [args...]
 *
 * Audits:
 *  - packages specified via -p/--package flags (when present)
 *  - the first positional argument (when -p is absent)
 */
async function mainNpx() {
  // Recursion guard
  if (process.env.SAFE_NPM_ACTIVE === '1') {
    await runCmd(REAL_NPX, process.argv.slice(2));
    return;
  }

  const argv = applyMinAgeFlag(process.argv.slice(2));

  // Built-in management subcommands (shared with safe-npm)
  if (argv[0] === 'disable') { cmdDisable(); return; }
  if (argv[0] === 'enable')  { cmdEnable();  return; }
  if (argv[0] === 'status')  { cmdStatus();  return; }

  // Sentinel file: disabled → pass everything straight to npx
  if (fs.existsSync(DISABLED_FLAG)) {
    await runNpx(argv);
    return;
  }

  if (argv.includes('--unsafe')) {
    console.warn(`[safe-npx] ⚠️   --unsafe detected — skipping safety checks!\n`);
    await runNpx(argv.filter(a => a !== '--unsafe'));
    return;
  }

  // npx flags that consume the next argument as a value (not a package specifier)
  const EXEC_VALUE_FLAGS = new Set(['-c', '--call', '--shell-auto-fallback', '--userconfig', '--npmPath']);

  // ── Collect packages from -p/--package flags and/or the positional arg ──────
  // -p pkg: the package to install+run (when present, positional is the binary name, not a package)
  // No -p:  the first positional arg is both the package and the binary name
  const pkgFlagEntries = [];  // { argIdx, spec } for each -p/--package value
  let positionalIdx = -1;

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--') { if (pkgFlagEntries.length === 0 && positionalIdx === -1 && i + 1 < argv.length) positionalIdx = i + 1; break; }
    if (a === '-p' || a === '--package') {
      if (i + 1 < argv.length) { pkgFlagEntries.push({ argIdx: i + 1, spec: argv[i + 1] }); i++; }
      continue;
    }
    if (a.startsWith('--package=')) {
      pkgFlagEntries.push({ argIdx: i, spec: a.slice('--package='.length), equalsForm: true });
      continue;
    }
    if (EXEC_VALUE_FLAGS.has(a)) { i++; continue; }
    // First non-flag arg: this is the positional package (or the command when -p is used).
    // Everything after it are arguments to the executed command — stop scanning.
    if (!a.startsWith('-')) { if (positionalIdx === -1) positionalIdx = i; break; }
  }

  // Which specs to audit
  const specsToAudit = pkgFlagEntries.length > 0
    ? pkgFlagEntries
    : (positionalIdx >= 0 ? [{ argIdx: positionalIdx, spec: argv[positionalIdx] }] : []);

  if (specsToAudit.length === 0) {
    await runNpx(argv);
    return;
  }

  // Local paths and URLs pass through without age check
  const allLocal = specsToAudit.every(({ spec }) =>
    spec.startsWith('.') || spec.startsWith('/') || spec.includes('://'));
  if (allLocal) { await runNpx(argv); return; }

  console.log(`\n[safe-npx] Checking ${specsToAudit.length} package(s) — only versions >${SAFE_AGE_DAYS} days old allowed\n`);

  const newArgv = [...argv];
  try {
    for (const { argIdx, spec, equalsForm } of specsToAudit) {
      if (spec.startsWith('.') || spec.startsWith('/') || spec.includes('://')) continue;
      const pinned = await auditNpxSpec(spec);
      newArgv[argIdx] = equalsForm ? `--package=${pinned}` : pinned;
    }
  } catch (e) {
    console.error(`[safe-npx] Error: ${e.message}`);
    process.exit(1);
  }

  console.log(`\n[safe-npx] Running: npx ${newArgv.join(' ')}\n`);
  await runNpx(newArgv);
}

// ─── Disable / enable / status ────────────────────────────────────────────────

function cmdDisable() {
  fs.mkdirSync(INSTALL_DIR, { recursive: true });
  fs.writeFileSync(DISABLED_FLAG, '');
  console.log('[safe-npm] ✅  Disabled — safety checks bypassed until you run: safe-npm enable');
}

function cmdEnable() {
  if (fs.existsSync(DISABLED_FLAG)) {
    fs.unlinkSync(DISABLED_FLAG);
    console.log('[safe-npm] ✅  Enabled — safety checks are active.');
  } else {
    console.log('[safe-npm] Already enabled.');
  }
}

function cmdStatus() {
  const disabled = fs.existsSync(DISABLED_FLAG);
  const ageSource = process.env.SAFE_NPM_AGE_DAYS ? 'SAFE_NPM_AGE_DAYS' : 'default';
  console.log(`[safe-npm] Status: ${disabled ? '🔴 DISABLED' : '🟢 ENABLED'}`);
  console.log(`           Age threshold: ${SAFE_AGE_DAYS} days (${ageSource}; override with --min-age, min ${MIN_AGE_DAYS})`);
  console.log(`           Real npm:      ${REAL_NPM}`);
}

// ─── Entry point ──────────────────────────────────────────────────────────────

async function main() {
  // Recursion guard: if we're being called from within our own runNpm/runNpmSilent,
  // pass through directly to avoid double-auditing.
  if (process.env.SAFE_NPM_ACTIVE === '1') {
    await runCmd(REAL_NPM, process.argv.slice(2));
    return;
  }

  const argv = applyMinAgeFlag(process.argv.slice(2));

  if (argv.length === 0) {
    await runNpm([]);
    return;
  }

  const cmd  = argv[0];
  const rest = argv.slice(1);

  // Built-in management subcommands
  if (cmd === 'disable') { cmdDisable(); return; }
  if (cmd === 'enable')  { cmdEnable();  return; }
  if (cmd === 'status')  { cmdStatus();  return; }

  // Sentinel file: disabled → pass everything straight to npm
  if (fs.existsSync(DISABLED_FLAG)) {
    await runNpm(argv);
    return;
  }

  // --unsafe bypasses all safety checks and passes args straight to npm
  if (argv.includes('--unsafe')) {
    console.warn(`[safe-npm] ⚠️   --unsafe detected — skipping safety checks!\n`);
    await runNpm(argv.filter(a => a !== '--unsafe'));
    return;
  }

  if (INSTALL_CMDS.has(cmd) || UPDATE_CMDS.has(cmd)) {
    const { flags, pkgArgs } = splitFlagsAndPkgs(rest);

    // Workspace and prefix installs are not yet supported — guard early.
    const WS_FLAGS = new Set(['--workspace', '-w', '--workspaces', '--ws', '-ws', '--prefix']);
    const hasWsFlag = flags.some(f =>
      WS_FLAGS.has(f) ||
      f.startsWith('--workspace=') || f.startsWith('--prefix=') ||
      f.startsWith('--workspaces=') || f.startsWith('--ws='));
    if (hasWsFlag) {
      console.error('[safe-npm] ❌  Workspace and --prefix installs are not yet supported by safe-npm.\n' +
                    '           Use --unsafe to bypass safety checks, or run npm directly.\n');
      process.exit(1);
    }

    // Flags that can override the registry or resolution source are blocked — safe-npm
    // only audits against registry.npmjs.org and can't inspect custom config files.
    const hasUnsupportedFlag = flags.some(f =>
      f === '--registry'    || f.startsWith('--registry=') ||
      f === '--tag'         || f.startsWith('--tag=') ||
      f === '--userconfig'  || f.startsWith('--userconfig=') ||
      f === '--globalconfig'|| f.startsWith('--globalconfig='));
    if (hasUnsupportedFlag) {
      console.error('[safe-npm] ❌  Flags that can override the registry are not supported by safe-npm\n' +
                    '           (--registry, --tag, --userconfig, --globalconfig).\n' +
                    '           Use --unsafe to bypass safety checks, or run npm directly.\n');
      process.exit(1);
    }

    // Detect global install flags (-g / --global / --location=global).
    // Strip them from flags passed to the handler; the handler adds -g back for Phase 4.
    const { isGlobal, strippedFlags } = extractGlobalFlag(flags);
    const effectiveFlags = isGlobal ? strippedFlags : flags;

    if (INSTALL_CMDS.has(cmd)) {
      if (pkgArgs.length > 0) {
        await handleInstallWithPackages(pkgArgs, effectiveFlags, { isGlobal });
      } else if (isGlobal) {
        // `npm install -g` with no packages would globally install the current project.
        // handleInstallNoPackages uses --package-lock-only which npm ignores for global
        // installs, so it would perform the real install before any audit. Block for now.
        console.error('[safe-npm] ❌  Global install with no packages (npm install -g) is not yet supported by safe-npm.\n' +
                      '           Specify the package(s) to install: safe-npm install -g <pkg...>\n' +
                      '           Use --unsafe to bypass safety checks, or run npm directly.\n');
        process.exit(1);
      } else {
        await handleInstallNoPackages(effectiveFlags);
      }
    } else {
      // Global updates not yet supported — block early rather than silently operating locally.
      if (isGlobal) {
        console.error('[safe-npm] ❌  Global updates (npm update -g) are not yet supported by safe-npm.\n' +
                      '           Use --unsafe to bypass safety checks, or run npm directly.\n');
        process.exit(1);
      }
      await handleUpdate(pkgArgs, effectiveFlags);
    }
  } else if (cmd === 'ci') {
    await handleCi();
  } else {
    // Pass through to npm unchanged
    await runNpm(argv);
  }
}

const isNpxMode = process.env.SAFE_NPX_MODE === '1';
(isNpxMode ? mainNpx() : main()).catch(e => {
  const prefix = isNpxMode ? '[safe-npx]' : '[safe-npm]';
  console.error(`${prefix} Fatal error:`, e.message);
  process.exit(1);
});
