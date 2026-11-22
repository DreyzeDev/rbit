"""Opt-in sandbox for shell commands the agent runs (`bash`, `terminal`, git bisect).

Config (`.rbit/config.json` or the user config):

    "shell_sandbox": "off" | "auto" | "strict"     (default "off")
    "shell_sandbox_network": false                  (default false: no network inside the sandbox)

* `off`     - commands run as before.
* `auto`    - commands run inside the sandbox when this machine has a backend, else as before.
* `strict`  - commands run inside the sandbox or not at all.

Inside the sandbox the whole file system is read-only except the workspace and a private
/tmp, the user's home directory is hidden (so `~/.ssh`, `~/.rbit/config.json` with the API
key, `~/.aws` are not readable) unless the workspace lives in it, and the network is cut
unless `shell_sandbox_network` is true.

Backends: bubblewrap (`bwrap`) on Linux, `sandbox-exec` on macOS. Windows has no
comparable unprivileged mechanism, so there `strict` refuses to run shell commands and
`auto` runs them unsandboxed. Only shell commands are covered: the file tools have their
own workspace checks, and `git`/`rg`/formatters started by rbit itself are not sandboxed.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

MODES = ("off", "auto", "strict")

#: Home-directory entries a macOS sandbox hides (bubblewrap hides the whole home instead).
_MAC_SECRET_DIRS = (".ssh", ".aws", ".gnupg", ".rbit", ".config/gh", ".netrc")


def mode(config: dict[str, Any] | None) -> str:
    raw = (config or {}).get("shell_sandbox")
    value = raw.strip().lower() if isinstance(raw, str) else "off"
    return value if value in MODES else "off"


def network_allowed(config: dict[str, Any] | None) -> bool:
    return (config or {}).get("shell_sandbox_network") is True


def backend() -> str | None:
    """Name of the sandbox tool available on this machine, or None."""
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    return None


def unavailable_message() -> str:
    return (
        "`shell_sandbox` is `strict`, but this machine has no sandbox backend "
        "(needs bubblewrap on Linux or sandbox-exec on macOS; Windows has none), "
        "so the shell command was not run. Install a backend, or set `shell_sandbox` to "
        "`auto` or `off`."
    )


def _bwrap_argv(command: str, workspace: Path, network: bool) -> list[str]:
    ws = str(workspace)
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    home = os.environ.get("HOME")
    if home and Path(home) != Path("/"):
        argv += ["--tmpfs", home]  # hide ~/.ssh, ~/.rbit/config.json, ~/.aws ...
    argv += ["--bind", ws, ws]  # after the tmpfs: a workspace inside $HOME stays visible
    argv += ["--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session"]
    if not network:
        argv.append("--unshare-net")
    argv += ["--chdir", ws, "/bin/sh", "-c", command]
    return argv


def _sb_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sandbox_exec_argv(command: str, workspace: Path, network: bool) -> list[str]:
    ws = _sb_quote(str(workspace.resolve()))
    home = (os.environ.get("HOME") or "/nonexistent").rstrip("/")
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* (subpath {ws}) (subpath "/private/tmp") (subpath "/private/var/folders") (literal "/dev/null") (literal "/dev/tty"))',
    ]
    for rel in _MAC_SECRET_DIRS:
        rules.append(f"(deny file-read* (subpath {_sb_quote(f'{home}/{rel}')}))")
    if not network:
        rules.append("(deny network*)")
    return ["sandbox-exec", "-p", "\n".join(rules), "/bin/sh", "-c", command]


def wrap(command: str, workspace: Path, *, network: bool = False, tool: str | None = None) -> list[str] | None:
    """The argv that runs `command` inside the sandbox, or None when there is no backend."""
    tool = tool or backend()
    if tool == "bwrap":
        return _bwrap_argv(command, workspace, network)
    if tool == "sandbox-exec":
        return _sandbox_exec_argv(command, workspace, network)
    return None
