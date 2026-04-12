# tools

Supply-chain attack protection tools for npm and pip.

## Tools

| Tool | Description |
|------|-------------|
| [safe-npm](safe-npm/) | Wraps npm/npx — only installs packages published >30 days ago |
| [safe-pip](safe-pip/) | Wraps pip — only installs packages published >30 days ago |

## Background

Supply-chain attacks typically work by publishing a malicious version of a popular package. The malicious version is usually discovered and removed within days, but users who installed during that window are already compromised.

These tools defend against that attack vector by enforcing a 30-day age requirement on all packages (configurable). This gives the community time to audit and flag malicious releases before you install them.

Both tools also perform recursive transitive dependency checking — if A depends on B depends on C, all three are verified.

## Quick Start

```bash
# npm/npx protection
cd safe-npm && bash install.sh --npm-wrapper

# pip protection
cd safe-pip && bash install.sh --pip-wrapper
```

See each tool's directory for detailed documentation.
