"""Safe remote execution helpers for Docker containers and SSH hosts."""

from __future__ import annotations

import asyncio
import re

from .envscrub import sanitized_env

_SAFE_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


def validate_remote_target(target: str, label: str = "target") -> str:
    """Rejects empty targets or values starting with '-' (option injection)."""
    clean = (target or "").strip()
    if not clean:
        raise ValueError(f"{label} is required.")
    if clean.startswith("-") or not _SAFE_TARGET_RE.match(clean):
        raise ValueError(f"Unsafe {label}: {clean!r}")
    return clean


def build_docker_ps_cmd() -> list[str]:
    return ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"]


def build_docker_exec_cmd(container: str, command: str, *, workdir: str = "") -> list[str]:
    safe_container = validate_remote_target(container, "container")
    cmd = ["docker", "exec"]
    wd = (workdir or "").strip()
    if wd:
        if wd.startswith("-"):
            raise ValueError(f"Unsafe workdir: {wd!r}")
        cmd.extend(["-w", wd])
    cmd.extend([safe_container, "sh", "-lc", command])
    return cmd


def build_ssh_exec_cmd(
    host: str,
    command: str,
    *,
    port: int | None = None,
    identity_file: str = "",
) -> list[str]:
    safe_host = validate_remote_target(host, "host")
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
    ]
    if port is not None:
        if not (1 <= int(port) <= 65535):
            raise ValueError(f"Invalid SSH port: {port}")
        cmd.extend(["-p", str(int(port))])
    ident = (identity_file or "").strip()
    if ident:
        if ident.startswith("-"):
            raise ValueError(f"Unsafe identity_file: {ident!r}")
        cmd.extend(["-i", ident])
    cmd.extend([safe_host, command])
    return cmd


async def run_remote_argv(
    argv: list[str],
    *,
    timeout: float = 60.0,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Runs a remote CLI command with scrubbed LLM credentials and a timeout."""
    env = sanitized_env()
    if extra_env:
        env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=max(1.0, float(timeout)))
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, f"Remote command timed out after {timeout:.0f}s."
    text = (stdout or b"").decode("utf-8", errors="replace")
    return int(proc.returncode or 0), text
