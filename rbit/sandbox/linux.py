"""Linux backends: the built-in Landlock launcher (preferred) and bubblewrap (fallback)."""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .policy import SECRET_HOME_DIRS, Policy, private_tmp

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


#: System files that hold credentials even though /etc is otherwise readable.
SYSTEM_SECRETS = (
    "/etc/shadow", "/etc/shadow-", "/etc/gshadow", "/etc/gshadow-", "/etc/sudoers", "/etc/sudoers.d",
    "/etc/ssl/private", "/etc/security/opasswd",
)


def landlock_spec(policy: Policy) -> dict[str, object]:
    workspace = policy.workspace
    root = private_tmp(workspace) / "root"  # an empty host directory the launcher builds the new root in
    root.mkdir(exist_ok=True)
    home = Path.home()
    reads = [*SYSTEM_READ, *(str(p) for p in policy.read_paths)]
    if policy.network and os.path.lexists("/etc/resolv.conf"):
        reads.append(os.path.realpath("/etc/resolv.conf"))  # on WSL and systemd it points outside /etc
    return {
        "workspace": str(workspace),
        "cwd": str(policy.start_dir),
        "root": str(root),
        "network": policy.network,
        "read": reads,
        "write": [str(p) for p in policy.write_paths],
        # invisible inside, even when a granted folder contains them: the project's own state,
        # the user's credential folders (a user who lists "~" must not expose ~/.ssh), system secrets
        "hide": [str(workspace / ".rbit"), *(str(home / rel) for rel in SECRET_HOME_DIRS), *SYSTEM_SECRETS],
        # visible but not writable: files a later `git` run OUTSIDE the sandbox would execute
        "readonly": [str(workspace / ".git" / "hooks"), str(workspace / ".git" / "config")],
    }


def landlock_argv(argv: list[str], policy: Policy) -> list[str]:
    return [sys.executable, str(HELPER), json.dumps(landlock_spec(policy)), "--", *argv]


def bwrap_argv(argv: list[str], policy: Policy) -> list[str]:
    ws = str(policy.workspace)
    out = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    # /run and /mnt hold the sockets that matter (ssh-agent, docker, D-Bus, the WSL bridge)
    out += ["--tmpfs", "/run", "--tmpfs", "/mnt"]
    home = os.environ.get("HOME")
    if home and Path(home) != Path("/"):
        out += ["--tmpfs", home]  # hide ~/.ssh, ~/.rbit/config.json, ~/.aws ...
    for path in policy.read_paths:
        out += ["--ro-bind-try", str(path), str(path)]
    for path in policy.write_paths:
        out += ["--bind-try", str(path), str(path)]
    out += ["--bind", ws, ws]  # after the tmpfs: a workspace inside $HOME stays visible
    if (policy.workspace / ".rbit").exists():
        out += ["--tmpfs", str(policy.workspace / ".rbit")]
    # visible but not writable: files a later `git` run OUTSIDE the sandbox would execute
    for guarded in (policy.workspace / ".git" / "hooks", policy.workspace / ".git" / "config"):
        if guarded.exists():
            out += ["--ro-bind-try", str(guarded), str(guarded)]
    # system credential files stay unreadable (as with the Landlock launcher): an empty file or
    # directory replaces them
    for secret in SYSTEM_SECRETS:
        if os.path.isdir(secret):
            out += ["--tmpfs", secret]
        elif os.path.lexists(secret):
            out += ["--ro-bind", os.devnull, secret]
    out += ["--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL"]
    if not policy.network:
        out.append("--unshare-net")
    out += ["--chdir", str(policy.start_dir), *argv]
    return out
