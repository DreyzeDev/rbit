"""What a sandboxed program may touch: the `Policy`, and the environment it gets.

Everything here is pure (no process is started), so it behaves the same on every system and
is unit-tested directly.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODES = ("off", "auto", "strict")

#: Memory one sandboxed program tree may use (Windows job limit). Generous on purpose: language
#: servers and bundlers need a lot; the point is a runaway process, not a tight budget.
DEFAULT_MEMORY_MB = 8192

#: Variables a sandboxed program may inherit. Everything else (API keys, tokens, cloud
#: credentials, SSH agent sockets, the whole rest of the user's environment) is dropped.
ENV_ALLOWLIST = frozenset(
    {
        "PATH", "PATHEXT", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TERM", "TZ", "COLORTERM",
        "PYTHONIOENCODING", "PYTHONUTF8", "NO_COLOR", "CI",
        # Windows system variables a program cannot start without
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS", "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA",
        "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    }
)

#: Directories under the home directory that hold credentials. Never added to a policy, even
#: when they are on PATH or listed by the user.
SECRET_HOME_DIRS = (
    ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", ".rbit", ".config/gh", ".config/gcloud",
    ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".password-store", ".local/share/keyrings",
)


def mode(config: dict[str, Any] | None) -> str:
    raw = (config or {}).get("shell_sandbox")
    value = raw.strip().lower() if isinstance(raw, str) else "off"
    return value if value in MODES else "off"


def network_allowed(config: dict[str, Any] | None) -> bool:
    return (config or {}).get("shell_sandbox_network") is True


def _paths(config: dict[str, Any] | None, key: str) -> tuple[Path, ...]:
    raw = (config or {}).get(key)
    if not isinstance(raw, list):
        return ()
    return tuple(Path(os.path.expanduser(str(item))) for item in raw if isinstance(item, str) and item.strip())


@dataclass(frozen=True)
class Policy:
    """Access granted to one sandboxed program."""

    workspace: Path
    network: bool = False
    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    #: Variables passed through from the parent in addition to ENV_ALLOWLIST (by name).
    env_names: tuple[str, ...] = ()
    #: Variables set explicitly (for example an MCP server's own `environment`).
    env_values: dict[str, str] = field(default_factory=dict)
    #: Working directory of the program when it differs from the workspace (always inside it).
    cwd: Path | None = None
    #: Memory the whole program tree may use, in MB (0 = no limit). Enforced where the OS can.
    memory_mb: int = 0
    #: Folders widened for this program only (read-only and writable extras), as a stable key.
    extras_key: str = ""
    #: Windows: start the program on a private desktop (no screenshots or input into the user's own).
    desktop: bool = True

    def with_network(self, network: bool) -> "Policy":
        return Policy(
            self.workspace, network, self.read_paths, self.write_paths, self.env_names, self.env_values,
            self.cwd, self.memory_mb, self.extras_key, self.desktop,
        )

    @property
    def start_dir(self) -> Path:
        return self.cwd or self.workspace


def private_tmp(workspace: Path) -> Path:
    """A per-workspace scratch directory outside the workspace (mode 0700), created on demand."""
    digest = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8", "replace")).hexdigest()[:12]
    uid = os.getuid() if hasattr(os, "getuid") else "u"
    path = Path(tempfile.gettempdir()) / f"rbit-sandbox-{uid}" / digest
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o700)
    return path


def _is_secret_location(path: Path) -> bool:
    home = Path.home()
    try:
        relative = path.resolve().relative_to(home.resolve())
    except (ValueError, OSError):
        return False
    text = relative.as_posix()
    return any(text == d or text.startswith(d + "/") for d in SECRET_HOME_DIRS)


def _is_too_broad(path: Path) -> bool:
    """True for the home directory, any ancestor of it, and any folder that CONTAINS a credential
    folder: granting such a path would hand over `~/.ssh` and friends as a side effect."""
    try:
        resolved = path.resolve()
        home = Path.home().resolve()
    except OSError:
        return True
    if resolved == home or resolved in home.parents:
        return True
    for rel in SECRET_HOME_DIRS:
        try:
            (home / rel).relative_to(resolved)
        except ValueError:
            continue
        return True
    return False


def is_forbidden_extra(path: Path) -> bool:
    """A folder that may never be added to a program's access (credentials, or too broad)."""
    return _is_secret_location(path) or _is_too_broad(path)


def interpreter_paths() -> tuple[Path, ...]:
    """The running Python's prefixes: scripts the agent runs need the standard library."""
    seen: dict[str, Path] = {}
    for raw in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix):
        if raw:
            seen[str(raw)] = Path(raw)
    return tuple(seen.values())


def path_directories() -> tuple[Path, ...]:
    """Existing PATH directories that are not credential stores (read and execute only)."""
    found: dict[str, Path] = {}
    for part in (os.environ.get("PATH") or "").split(os.pathsep):
        if not part:
            continue
        path = Path(part)
        if path.is_dir() and not _is_secret_location(path):
            found[str(path)] = path
    return tuple(found.values())


def policy_from_config(
    config: dict[str, Any] | None,
    workspace: Path,
    *,
    network: bool | None = None,
    extra_read: tuple[Path, ...] = (),
    extra_write: tuple[Path, ...] = (),
    env_names: tuple[str, ...] = (),
    env_values: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> Policy:
    """The policy for a program rbit starts in `workspace`, from the user's configuration.

    Raises ValueError when the workspace (or the requested working directory) is inside a
    credential folder, or the working directory is outside the workspace. Extra read/write
    folders that are credential folders, or contain one (`~`, its parents), are dropped.
    """
    workspace = Path(workspace).resolve()
    if _is_secret_location(workspace):
        raise ValueError(f"refusing to sandbox a workspace inside a credential folder: {workspace}")
    start: Path | None = None
    if cwd is not None:
        start = Path(cwd).resolve()
        try:
            start.relative_to(workspace)
        except ValueError:
            raise ValueError(
                f"the working directory {start} is outside the workspace {workspace}; "
                "list extra folders under `sandbox.read` / `sandbox.write` instead"
            ) from None
    # PATH directories are exposed read-only on POSIX (a bind/landlock rule, nothing persists).
    # On Windows every grant is a permanent ACL change in the user's profile, so there only the
    # interpreter and the started executable's own directory are opened (see windows.prepare).
    on_path = path_directories() if os.name != "nt" else ()
    wanted_reads = [*_paths(config, "shell_sandbox_read"), *extra_read]
    wanted_writes = [*_paths(config, "shell_sandbox_write"), *extra_write]
    # the extras are what a configuration (maybe a hostile one) asks for: filter them hard
    extra_reads = [p for p in wanted_reads if not is_forbidden_extra(p)]
    extra_writes = [p for p in wanted_writes if not is_forbidden_extra(p)]
    reads = [*interpreter_paths(), *on_path, *extra_reads]
    writes = [workspace, private_tmp(workspace), *extra_writes]
    configured_env = (config or {}).get("shell_sandbox_env")
    names = list(env_names)
    if isinstance(configured_env, list):
        names += [str(n) for n in configured_env if isinstance(n, str)]
    memory = (config or {}).get("shell_sandbox_memory_mb")
    extras = sorted({str(p) for p in (*extra_reads, *extra_writes)})
    return Policy(
        workspace=workspace,
        network=network_allowed(config) if network is None else network,
        read_paths=tuple(dict.fromkeys(p for p in reads if not _is_secret_location(p))),
        write_paths=tuple(dict.fromkeys(writes)),
        env_names=tuple(dict.fromkeys(names)),
        env_values=dict(env_values or {}),
        cwd=start,
        memory_mb=int(memory) if isinstance(memory, int) and memory > 0 else DEFAULT_MEMORY_MB,
        extras_key=hashlib.sha256("\n".join(extras).encode("utf-8", "replace")).hexdigest()[:10] if extras else "",
        desktop=(config or {}).get("shell_sandbox_desktop") is not False,
    )


def sandbox_env(policy: Policy, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a sandboxed program starts with.

    An allowlist, not a blacklist: a variable the user never thought about (a new provider's
    key, a cloud token, `SSH_AUTH_SOCK`) is not inherited. HOME and the temp variables point
    at the private scratch directory, so tools do not go looking in the real home.
    """
    source = os.environ if base is None else base
    allowed = ENV_ALLOWLIST | {name.upper() for name in policy.env_names}
    env = {key: value for key, value in source.items() if key.upper() in allowed}
    scratch = str(private_tmp(policy.workspace))
    # (Windows cannot create an AppContainer process without LOCALAPPDATA, so it points there too.)
    for key in ("TMPDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        env[key] = scratch
    env.update(policy.env_values)
    return env
