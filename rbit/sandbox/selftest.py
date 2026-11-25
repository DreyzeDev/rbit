"""Isolation self-test: starts real programs in the real sandbox and checks what they can reach.

Used by `rbit sandbox check` and by the test-suite, so that "the sandbox isolates" is a
measured fact on each machine and not a claim. Every check has two halves: things that must
WORK inside (a sandbox that breaks everything would pass every "denied" check trivially) and
things that must be DENIED.

The probe program is the running Python interpreter, so no other tool is needed.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import backend, policy_for, run_sync

CANARY_ENV = "RBIT_SANDBOX_CANARY"
SECRET_TEXT = "TOP-SECRET-OUTSIDE-THE-WORKSPACE"
PROJECT_SECRET = '{"api_key": "sk-project-state-secret"}'


@dataclass(frozen=True)
class Check:
    name: str
    expect: str  # "works" or "denied"
    passed: bool
    detail: str = ""


def _run(code: str, arg: str, policy: Any, timeout: float = 60.0) -> tuple[int, str]:
    proc = run_sync([sys.executable, "-c", code, arg], policy, timeout=timeout)
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
    return proc.returncode, text


def run_checks(config: dict[str, Any] | None = None, *, keep: bool = False) -> list[Check]:
    """Runs every applicable check against the sandbox backend of this machine."""
    if backend() is None:
        raise RuntimeError("no sandbox backend on this machine")
    cfg = {**(config or {}), "shell_sandbox": "strict"}
    # one folder in the system temp directory: `workspace` is granted, `outside` is not
    arena = Path(tempfile.mkdtemp(prefix="rbit-sandbox-check-")).resolve()
    workspace, outside = arena / "workspace", arena / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET_TEXT, encoding="utf-8")
    (workspace / "hello.txt").write_text("hello", encoding="utf-8")
    # the project's own state, as rbit leaves it in every workspace
    state = workspace / ".rbit"
    state.mkdir()
    (state / "config.json").write_text(PROJECT_SECRET, encoding="utf-8")
    hooks = workspace / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    policy = policy_for(cfg, workspace)
    os.environ[CANARY_ENV] = "canary-must-not-reach-the-sandbox"
    checks: list[Check] = []

    def record(name: str, expect: str, passed: bool, detail: str = "") -> None:
        checks.append(Check(name, expect, bool(passed), detail[:200]))

    server = socket.socket()
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        server.settimeout(0.2)
        port = server.getsockname()[1]

        code, out = _run("import sys;print(open(sys.argv[1]).read())", str(workspace / "hello.txt"), policy)
        record("reads files in the workspace", "works", code == 0 and "hello" in out, out)

        target = workspace / "written.txt"
        code, out = _run("import sys;open(sys.argv[1],'w').write('data')", str(target), policy)
        record("writes files in the workspace", "works", code == 0 and target.is_file(), out)

        code, out = _run(
            "import subprocess,sys;print(subprocess.run([sys.executable,'-c','print(40+2)'],capture_output=True,text=True).stdout.strip())",
            "-",
            policy,
        )
        record("starts child processes", "works", code == 0 and "42" in out, out)

        scratch = str(policy.write_paths[1])
        code, out = _run("import os,sys;open(os.path.join(sys.argv[1],'t.txt'),'w').write('x');print('ok')", scratch, policy)
        record("writes to its private scratch directory", "works", code == 0 and "ok" in out, out)

        code, out = _run("import sys;print(open(sys.argv[1]).read())", str(outside / "secret.txt"), policy)
        record("cannot read a file outside the workspace", "denied", code != 0 and SECRET_TEXT not in out, out)

        victim = outside / "planted.txt"
        _run("import sys;open(sys.argv[1],'w').write('pwned')", str(victim), policy)
        record("cannot write a file outside the workspace", "denied", not victim.exists())

        code, out = _run("import os,sys;print(os.listdir(sys.argv[1]))", str(Path.home()), policy)
        if backend() == "bwrap":
            # bwrap replaces $HOME with an empty tmpfs (hiding ~/.ssh, ~/.rbit, etc.)
            record("cannot list the user's home directory", "denied", code != 0 or ".ssh" not in out, out)
        else:
            record("cannot list the user's home directory", "denied", code != 0, out)

        rbit_home = Path.home() / ".rbit"
        code, out = _run("import os,sys;print(os.listdir(sys.argv[1]))", str(rbit_home), policy)
        record("cannot read ~/.rbit (config with the API key)", "denied", code != 0, out)

        code, out = _run(
            "import socket,sys;s=socket.socket();s.settimeout(3);s.connect(('127.0.0.1',int(sys.argv[1])));print('con'+'nected')",
            str(port),
            policy,
        )
        connected = False
        try:
            server.accept()
            connected = True
        except (socket.timeout, OSError):
            pass
        record("has no network (not even loopback)", "denied", code != 0 and not connected and "connected" not in out.splitlines()[-1:], out)

        code, out = _run("import os,sys;print(repr(os.environ.get(sys.argv[1])))", CANARY_ENV, policy)
        record("does not inherit the parent's environment", "denied", code == 0 and out == "None", out)

        code, out = _run("import os;print(os.environ.get('HOME') or os.environ.get('USERPROFILE'))", "-", policy)
        record("HOME points at the private scratch directory", "works", code == 0 and str(Path.home()) != out, out)

        code, out = _run("import sys;print(open(sys.argv[1]).read())", str(state / "config.json"), policy)
        record("cannot read the project's own .rbit state", "denied", "sk-project-state-secret" not in out, out)

        tamper = state / "tamper.txt"
        _run("import sys;open(sys.argv[1],'w').write('x')", str(tamper), policy)
        record("cannot write the project's own .rbit state", "denied", not tamper.exists())

        hook = hooks / "post-checkout"
        _run("import sys;open(sys.argv[1],'w').write('#!/bin/sh\\ntouch pwned\\n')", str(hook), policy)
        record("cannot plant a git hook (it would run outside the sandbox)", "denied", not hook.exists())

        escape_link = workspace / "escape_secret.txt"
        try:
            escape_link.symlink_to(outside / "secret.txt")
            code, out = _run("import sys;print(open(sys.argv[1]).read())", str(escape_link), policy)
            record(
                "cannot read outside secret via workspace symlink",
                "denied",
                code != 0 and SECRET_TEXT not in out,
                out,
            )
        except OSError:
            # Windows without Developer Mode may disallow creating symlinks for unprivileged users.
            pass

        if sys.platform.startswith("linux"):
            sock_path = outside / "host.sock"
            host_socket = socket.socket(socket.AF_UNIX)
            host_socket.bind(str(sock_path))
            host_socket.listen(1)
            host_socket.settimeout(0.5)
            code, out = _run(
                "import socket,sys;s=socket.socket(socket.AF_UNIX);s.settimeout(3);s.connect(sys.argv[1]);print('con'+'nected')",
                str(sock_path),
                policy,
            )
            accepted = False
            try:
                host_socket.accept()
                accepted = True
            except (socket.timeout, OSError):
                pass
            host_socket.close()
            record("cannot connect to a unix socket outside the workspace", "denied", code != 0 and not accepted, out)

            if backend() == "landlock":
                code, out = _run(
                    "import os;print([p for p in ('/run/WSL','/run/docker.sock','/run/user','/run/dbus','/run/systemd/private',"
                    "'/var/run/docker.sock','/mnt/c','/mnt/wsl','/mnt/wslg','/home','/root','/media','/srv') if os.path.lexists(p)])",
                    "-",
                    policy,
                )
                record("host sockets, /mnt, /home and /root do not exist inside", "denied", code == 0 and out == "[]", out)

            code, out = _run("import sys;print(repr(open('/etc/shadow').read()[:16]))", "-", policy)
            record("cannot read /etc/shadow", "denied", "root:" not in out and "$" not in out, out)

            if backend() == "landlock":
                code, out = _run("import socket;socket.socket(40, socket.SOCK_STREAM);print('con'+'nected')", "-", policy)
                record("cannot open a VM socket (AF_VSOCK)", "denied", code != 0, out)

            release = tuple(int(part) for part in os.uname().release.split(".")[:2] if part.isdigit())
            if backend() == "landlock" and release >= (6, 12):  # Landlock ABI 6: abstract unix sockets are scoped to the sandbox
                abstract_name = f"rbit-selftest-{os.getpid()}"
                abstract = socket.socket(socket.AF_UNIX)
                abstract.bind("\0" + abstract_name)
                abstract.listen(1)
                abstract.settimeout(0.5)
                code, out = _run(
                    "import socket,sys;s=socket.socket(socket.AF_UNIX);s.settimeout(3);s.connect('\\0'+sys.argv[1]);print('con'+'nected')",
                    abstract_name,
                    policy.with_network(True),  # a shared network namespace is where abstract sockets leak
                )
                reached = False
                try:
                    abstract.accept()
                    reached = True
                except (socket.timeout, OSError):
                    pass
                abstract.close()
                record("cannot reach the host's abstract unix sockets (network on)", "denied", code != 0 and not reached, out)

            parent = str(os.getpid())
            code, out = _run("import os,sys;print(os.path.exists('/proc/'+sys.argv[1]))", parent, policy)
            record("cannot see the parent process", "denied", code == 0 and out == "False", out)
            code, out = _run("import os;print(os.getpid())", "-", policy)
            record("is pid 1 of its own pid namespace", "works", code == 0 and out in ("1", "2"), out)
            code, out = _run(
                "import ctypes;l=ctypes.CDLL(None,use_errno=True);r=l.ptrace(16,1,0,0);print(r,ctypes.get_errno())", "-", policy
            )
            record("cannot ptrace", "denied", code == 0 and out.startswith("-1"), out)
        if sys.platform == "win32":
            parent = str(os.getpid())
            code, out = _run(
                "import ctypes,sys;h=ctypes.windll.kernel32.OpenProcess(0x1000,False,int(sys.argv[1]));print(bool(h))",
                parent,
                policy,
            )
            record("cannot open the parent process", "denied", code == 0 and out == "False", out)
    finally:
        server.close()
        os.environ.pop(CANARY_ENV, None)
        if not keep:
            shutil.rmtree(arena, ignore_errors=True)
    return checks
