"""macOS backend: `sandbox-exec` with a generated Seatbelt profile.

Seatbelt is deprecated by Apple but still the only unprivileged mechanism. The profile
denies every write outside the workspace and the private scratch directory, denies reading
the credential directories of the home directory, and denies the network unless allowed.
This backend could only be checked by tests of the generated profile: it has not been run
on macOS.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .policy import SECRET_HOME_DIRS, Policy


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(policy: Policy) -> str:
    home = (os.environ.get("HOME") or "/nonexistent").rstrip("/")
    writable = [str(p.resolve()) if p.exists() else str(p) for p in policy.write_paths] or [str(policy.workspace)]
    subpaths = " ".join(f"(subpath {quote(p)})" for p in writable)
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* {subpaths} (subpath "/private/tmp") (subpath "/private/var/folders") '
        '(literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper"))',
    ]
    for rel in SECRET_HOME_DIRS:
        rules.append(f"(deny file-read* (subpath {quote(f'{home}/{rel}')}))")
    ws_resolved = str(policy.workspace.resolve()) if policy.workspace.exists() else str(policy.workspace)
    rules.append(f"(deny file-write* (subpath {quote(f'{ws_resolved}/.rbit')}))")
    rules.append(f"(deny file-write* (subpath {quote(f'{ws_resolved}/.git/hooks')}))")
    if not policy.network:
        rules.append("(deny network*)")
    return "\n".join(rules)


def argv(args: list[str], policy: Policy) -> list[str]:
    return ["sandbox-exec", "-p", profile(policy), *args]
