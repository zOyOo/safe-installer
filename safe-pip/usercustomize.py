# safe-pip usercustomize hook
# Intercepts `python -m pip install ...` and routes it through safe-pip.
# Installed into each Python version's user site-packages by safe-pip's install.sh.
#
# Requires Python 3.10+ for sys.orig_argv (the only reliable way to know
# which module was passed to -m without false-positives).
# On Python 3.9-, `pip install` is still intercepted via the pip wrapper.
import sys
import os

_script = os.path.expanduser("~/.safe-pip/safe-pip.py")

if os.environ.get("SAFE_PIP_ACTIVE") or not os.path.exists(_script):
    pass  # already inside safe-pip, or safe-pip not installed
else:
    # sys.orig_argv is available from Python 3.10+
    # e.g. ['python3', '-m', 'pip', 'install', 'requests']
    _orig = getattr(sys, "orig_argv", None)
    if _orig:
        try:
            _m = _orig.index("-m")
            if _m + 1 < len(_orig) and _orig[_m + 1] in ("pip", "pip3"):
                import subprocess
                r = subprocess.run([sys.executable, _script] + sys.argv[1:])
                # os._exit avoids raising SystemExit inside site.py's usercustomize loader
                os._exit(r.returncode)
        except ValueError:
            pass
