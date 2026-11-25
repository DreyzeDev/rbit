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
    home_raw = (os.environ.get("HOME") or "/nonexistent").rstrip("/")
    home = str(Path(home_raw).resolve()) if Path(home_raw).exists() else home_raw
    writable = [str(p.resolve()) if p.exists() else str(p) for p in policy.write_paths] or [str(policy.workspace)]
    subpaths = " ".join(f"(subpath {quote(p)})" for p in writable)

    readable: list[str] = list(writable)
    for p in policy.read_paths:
        rp = str(p.resolve()) if p.exists() else str(p)
        if rp not in readable:
            readable.append(rp)
    for py_root in (sys.prefix, sys.base_prefix, str(Path(sys.executable).resolve().parent.parent)):
        rp = str(Path(py_root).resolve()) if Path(py_root).exists() else py_root
        if rp not in readable:
            readable.append(rp)
    read_subpaths = " ".join(f"(subpath {quote(p)})" for p in readable)

    ws_resolved = str(policy.workspace.resolve()) if policy.workspace.exists() else str(policy.workspace)
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* {subpaths} (literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper"))',
        f'(deny file-read* (subpath {quote(home)}) (subpath "/private/var/folders") (subpath "/private/tmp"))',
        f"(allow file-read* {read_subpaths})",
    ]
    for rel in SECRET_HOME_DIRS:
        rules.append(f"(deny file-read* (subpath {quote(f'{home}/{rel}')}))")
    rules.append(f"(deny file-read* (subpath {quote(f'{ws_resolved}/.rbit')}))")
    rules.append(f"(deny file-write* (subpath {quote(f'{ws_resolved}/.rbit')}))")
    rules.append(f"(deny file-write* (subpath {quote(f'{ws_resolved}/.git/hooks')}))")
    if not policy.network:
        rules.append("(deny network*)")
    return "\n".join(rules)


def argv(args: list[str], policy: Policy) -> list[str]:
    return ["sandbox-exec", "-p", profile(policy), *args]
