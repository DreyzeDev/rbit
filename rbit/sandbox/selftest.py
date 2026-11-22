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

        if sys.platform.startswith("linux"):
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
