"""Linux backends: the built-in Landlock launcher (preferred) and bubblewrap (fallback)."""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .policy import Policy

HELPER = Path(__file__).with_name("_linux_exec.py")

#: System locations a program needs to read in order to run at all.
SYSTEM_READ = (
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/proc", "/opt",
    "/run/systemd/resolve",  # where /etc/resolv.conf points on systemd systems
)


@functools.lru_cache(maxsize=1)
def landlock_available() -> bool:
    """True when the launcher can really confine a process here (kernel features, permissions)."""
    if not sys.platform.startswith("linux") or not HELPER.is_file():
        return False
    try:
        proc = subprocess.run(
            [sys.executable, str(HELPER), "--probe"], capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def bwrap_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("bwrap") is not None


def backend() -> str | None:
    if landlock_available():
        return "landlock"
    if bwrap_available():
        return "bwrap"
    return None


def landlock_spec(policy: Policy) -> dict[str, object]:
    return {
        "workspace": str(policy.workspace),
        "network": policy.network,
        "read": [*SYSTEM_READ, *(str(p) for p in policy.read_paths)],
        "write": [str(p) for p in policy.write_paths],
    }


def landlock_argv(argv: list[str], policy: Policy) -> list[str]:
    return [sys.executable, str(HELPER), json.dumps(landlock_spec(policy)), "--", *argv]


def bwrap_argv(argv: list[str], policy: Policy) -> list[str]:
    ws = str(policy.workspace)
    out = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    home = os.environ.get("HOME")
    if home and Path(home) != Path("/"):
        out += ["--tmpfs", home]  # hide ~/.ssh, ~/.rbit/config.json, ~/.aws ...
    for path in policy.read_paths:
        out += ["--ro-bind-try", str(path), str(path)]
    for path in policy.write_paths:
        out += ["--bind-try", str(path), str(path)]
    out += ["--bind", ws, ws]  # after the tmpfs: a workspace inside $HOME stays visible
    out += ["--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL"]
    if not policy.network:
        out.append("--unshare-net")
    out += ["--chdir", ws, *argv]
    return out
