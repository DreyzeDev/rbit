"""OS-level sandbox for every program rbit starts on the agent's behalf.

    "shell_sandbox": "off" | "auto" | "strict"     (default "off")
    "shell_sandbox_network": false                  network inside the sandbox (default: none)
    "shell_sandbox_read":  ["/path", ...]           extra read-only locations
    "shell_sandbox_write": ["/path", ...]           extra writable locations
    "shell_sandbox_env":   ["NAME", ...]            extra environment variables to pass through

* `off`     - programs run as before.
* `auto`    - programs run inside the sandbox when this machine has a backend, else as before.
* `strict`  - programs run inside the sandbox or not at all.

What the sandbox gives a program: read/write access to the workspace and a private scratch
directory, read-only access to the system and the interpreter, nothing else on disk (not the
home directory, not `~/.ssh`, not `~/.rbit` with the API key), no network, no view of other
processes, no way to gain privileges, and an environment reduced to an allowlist.

Backends, chosen by platform:

* Linux    - built-in Landlock launcher (user/mount/pid/net namespaces, Landlock, seccomp,
             no_new_privs, empty capabilities); bubblewrap when the launcher cannot run.
* Windows  - AppContainer + Job Object.
* macOS    - `sandbox-exec` with a generated Seatbelt profile.

The sandbox confines programs started through `spawn` / `run_sync`. Code that runs inside
the rbit process itself (Python plugins) cannot be confined by the OS: under `strict` it is
not loaded.

Known limits (also in the CHANGELOG):

* Linux: the project's own `.rbit/` folder is inside the writable workspace, so a confined
  command can modify it (Landlock rules are additive, a folder cannot be carved out). On
  Windows it is closed to the sandbox.
* Linux/macOS: the PATH directories are readable by confined programs.
* macOS: not run on a Mac. The Seatbelt profile is covered by tests of the generated text only.
* Windows: access is granted with persistent ACL entries (recorded in
  `~/.rbit/sandbox-grants.json`, removed by `rbit sandbox cleanup`), and loopback is never
  reachable from the container.
* A kernel or OS vulnerability can defeat any of these; the sandbox narrows what a mistaken
  or hostile command can do, it is not a hypervisor.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import linux, macos, windows
from .policy import (  # noqa: F401  (re-exported)
    ENV_ALLOWLIST,
    MODES,
    SECRET_HOME_DIRS,
    Policy,
    mode,
    network_allowed,
    policy_from_config,
    private_tmp,
    sandbox_env,
)


class SandboxUnavailable(RuntimeError):
    """The sandbox was required but could not be used. The program was NOT started."""


def backend() -> str | None:
    """Name of the sandbox mechanism available on this machine, or None."""
    if sys.platform.startswith("linux"):
        return linux.backend()
    if sys.platform == "darwin":
        return "sandbox-exec" if macos.available() else None
    if sys.platform == "win32":
        return "appcontainer" if windows.available() else None
    return None


def unavailable_message() -> str:
    return (
        "`shell_sandbox` is `strict`, but this machine has no sandbox backend "
        "(needs a Linux kernel with Landlock and user namespaces or bubblewrap, macOS "
        "sandbox-exec, or Windows AppContainers), so the command was not run. Fix the "
        "backend (`rbit sandbox check` tells what is missing), or set `shell_sandbox` to "
        "`auto` or `off`."
    )


def enabled(config: dict[str, Any] | None) -> bool:
    """True when programs must be started through the sandbox right now."""
    return mode(config) in ("auto", "strict") and backend() is not None


def ensure(config: dict[str, Any] | None) -> None:
    """Raises SandboxUnavailable when `strict` is configured but this machine cannot sandbox."""
    if mode(config) == "strict" and backend() is None:
        raise SandboxUnavailable(unavailable_message())


def policy_for(
    config: dict[str, Any] | None,
    workspace: Path,
    options: dict[str, Any] | None = None,
    env_values: dict[str, str] | None = None,
) -> Policy:
    """The policy for one program, from the global settings plus that program's own options.

    `options` is the program's `"sandbox"` block (an MCP server, a language server):
    `{"network": true, "read": [...], "write": [...], "env": ["NAME", ...]}`. The program's
    own options can open things for that program only, never loosen the global switch.
    """
    options = options if isinstance(options, dict) else {}

    def paths(key: str) -> tuple[Path, ...]:
        raw = options.get(key)
        if not isinstance(raw, list):
            return ()
        import os as _os

        return tuple(Path(_os.path.expanduser(str(p))) for p in raw if isinstance(p, str) and p.strip())

    env_names = options.get("env")
    return policy_from_config(
        config,
        workspace,
        network=True if options.get("network") is True else None,
        extra_read=paths("read"),
        extra_write=paths("write"),
        env_names=tuple(str(n) for n in env_names if isinstance(n, str)) if isinstance(env_names, list) else (),
        env_values=env_values,
    )


def plugins_allowed(config: dict[str, Any] | None) -> bool:
    """Python plugins run inside the rbit process, where no OS sandbox can confine them."""
    return mode(config) != "strict"


def wrap_argv(argv: list[str], policy: Policy, tool: str | None = None) -> list[str] | None:
    """The command line that runs `argv` confined, for backends that wrap a command line."""
    tool = tool or backend()
    if tool == "landlock":
        return linux.landlock_argv(argv, policy)
    if tool == "bwrap":
        return linux.bwrap_argv(argv, policy)
    if tool == "sandbox-exec":
        return macos.argv(argv, policy)
    return None


def wrap(command: str, workspace: Path, *, network: bool = False, tool: str | None = None) -> list[str] | None:
    """The argv that runs a shell `command` inside the sandbox, or None when there is no backend."""
    return wrap_argv(["/bin/sh", "-c", command], Policy(Path(workspace), network), tool)


async def spawn(
    argv: list[str],
    policy: Policy,
    *,
    env: dict[str, str] | None = None,
    stdin: bool = False,
    stdout: bool = True,
    stderr: str = "pipe",
) -> Any:
    """Starts `argv` confined by `policy`; returns an `asyncio.subprocess.Process` look-alike.

    `stderr` is "pipe", "stdout" (merged into stdout) or "null". Raises SandboxUnavailable
    when there is no backend or it failed: the program is never started unconfined.
    """
    if backend() is None:
        raise SandboxUnavailable(unavailable_message())
    child_env = dict(env) if env is not None else sandbox_env(policy)
    if sys.platform == "win32":
        try:
            return await windows.spawn(argv, policy, env=child_env, stdin=stdin, stdout=stdout, stderr=stderr)
        except windows.WindowsSandboxError as exc:
            raise SandboxUnavailable(f"sandbox could not start the program: {exc}") from exc
    wrapped = wrap_argv(argv, policy)
    if wrapped is None:
        raise SandboxUnavailable(unavailable_message())
    pipe, null = asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL
    err = {"pipe": pipe, "stdout": asyncio.subprocess.STDOUT, "null": null}[stderr]
    return await asyncio.create_subprocess_exec(
        *wrapped,
        cwd=str(policy.workspace),
        env=child_env,
        stdin=pipe if stdin else null,
        stdout=pipe if stdout else null,
        stderr=err,
        start_new_session=True,
    )


def run_sync(
    argv: list[str],
    policy: Policy,
    *,
    timeout: float = 30.0,
    input: bytes | None = None,  # noqa: A002 - mirrors subprocess.run
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Blocking `subprocess.run` look-alike that runs `argv` confined. Raises TimeoutExpired."""
    import concurrent.futures

    async def go() -> subprocess.CompletedProcess[bytes]:
        proc = await spawn(argv, policy, env=env, stdin=input is not None)
        try:
            out, err = await asyncio.wait_for(proc.communicate(input), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise subprocess.TimeoutExpired(argv, timeout) from None
        return subprocess.CompletedProcess(argv, proc.returncode or 0, out, err)

    # a worker thread with its own event loop: callers may already be inside a running loop
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, go()).result()


def status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """What `rbit sandbox check` reports."""
    return {
        "platform": sys.platform,
        "mode": mode(config),
        "backend": backend(),
        "network": network_allowed(config),
    }
