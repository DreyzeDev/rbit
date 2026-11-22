"""OS-level sandbox: real isolation on this machine, plus the pure logic on every machine.

Tests marked `needs_backend` start real programs inside the real sandbox (AppContainer on
Windows, Landlock/bubblewrap on Linux, sandbox-exec on macOS) and measure what they can
reach. They are skipped, not faked, on a machine without a backend.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rbit import sandbox
from rbit.models import Session
from rbit.sandbox import Policy, policy_for, policy_from_config, sandbox_env
from rbit.sandbox.policy import SECRET_HOME_DIRS
from rbit.tools import ToolExecutor

needs_backend = pytest.mark.skipif(sandbox.backend() is None, reason="no sandbox backend on this machine")
SECRET = "TOP-SECRET-OUTSIDE-THE-WORKSPACE"


# ---------------------------------------------------------------- pure logic (every OS)


def test_environment_is_an_allowlist_not_a_blacklist(tmp_path: Path) -> None:
    base = {
        "PATH": "/bin", "LANG": "C", "SYSTEMROOT": "C:\\Windows",
        "OPENAI_API_KEY": "sk-1", "SOME_NEW_PROVIDER_KEY": "sk-2", "AWS_SECRET_ACCESS_KEY": "aws",
        "GITHUB_TOKEN": "gh", "SSH_AUTH_SOCK": "/tmp/agent", "RBIT_API_KEY": "sk-3", "HOME": "/home/me",
    }
    env = sandbox_env(Policy(tmp_path), base)
    assert {"PATH", "LANG", "SYSTEMROOT"} <= set(env)
    leaked = {"OPENAI_API_KEY", "SOME_NEW_PROVIDER_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "RBIT_API_KEY"}
    assert not leaked & set(env), "a variable the user never listed reached the sandbox"
    assert env["HOME"] != "/home/me" and "rbit-sandbox" in env["HOME"]  # the real home is not exposed
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == env["HOME"]


def test_variables_are_passed_through_only_when_asked_for(tmp_path: Path) -> None:
    base = {"PATH": "/bin", "GITHUB_TOKEN": "gh", "OTHER": "x"}
    env = sandbox_env(Policy(tmp_path, env_names=("github_token",)), base)
    assert env["GITHUB_TOKEN"] == "gh" and "OTHER" not in env  # names compare case-insensitively
    explicit = sandbox_env(Policy(tmp_path, env_values={"MY_SERVER_KEY": "k"}), base)
    assert explicit["MY_SERVER_KEY"] == "k"
    configured = policy_from_config({"shell_sandbox_env": ["GITHUB_TOKEN"]}, tmp_path)
    assert sandbox_env(configured, base)["GITHUB_TOKEN"] == "gh"


def test_policy_grants_the_workspace_and_a_private_scratch_dir_and_nothing_secret(tmp_path: Path) -> None:
    home = Path.home()
    config = {"shell_sandbox_read": [str(home / ".ssh"), "~/.aws", str(tmp_path / "libs")], "shell_sandbox_write": [str(home / ".rbit")]}
    policy = policy_from_config(config, tmp_path / "ws")
    assert policy.workspace in policy.write_paths
    assert any("rbit-sandbox" in str(p) for p in policy.write_paths)
    assert tmp_path / "libs" in policy.read_paths  # an ordinary extra location is honoured
    for forbidden in SECRET_HOME_DIRS:
        for granted in (*policy.read_paths, *policy.write_paths):
            assert Path(granted) != home / forbidden, f"{forbidden} was handed to the sandbox"
    assert policy.network is False


def test_a_servers_own_options_open_things_for_that_server_only(tmp_path: Path) -> None:
    config = {"shell_sandbox": "strict"}
    plain = policy_for(config, tmp_path)
    opened = policy_for(config, tmp_path, {"network": True, "write": [str(tmp_path / "cache")], "env": ["TOKEN"]})
    assert plain.network is False and opened.network is True
    assert tmp_path / "cache" in opened.write_paths and "TOKEN" in opened.env_names
    assert policy_for(config, tmp_path).network is False  # the first policy is untouched


def test_plugins_are_not_loaded_under_strict_because_the_os_cannot_confine_them(tmp_path: Path) -> None:
    from rbit.plugins import PluginRegistry, load_plugins

    marker = tmp_path / "plugin_ran.txt"
    (tmp_path / "evil_plugin.py").write_text(f"open(r'{marker}', 'w').write('ran')\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        loaded = load_plugins(object(), {"shell_sandbox": "strict", "plugins": ["evil_plugin"]}, PluginRegistry())
    finally:
        sys.path.remove(str(tmp_path))
    assert loaded == [] and not marker.exists()
    assert sandbox.plugins_allowed({"shell_sandbox": "auto"}) and sandbox.plugins_allowed({})


def _load_launcher() -> Any:
    path = Path(sandbox.__file__).with_name("_linux_exec.py")
    spec = importlib.util.spec_from_file_location("rbit_linux_exec", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_seccomp_program_is_well_formed_and_denies_the_escape_syscalls(machine: str) -> None:
    launcher = _load_launcher()
    program = launcher._seccomp_program(machine)
    for index, (code, jt, jf, _k) in enumerate(program):
        if code in (0x15, 0x45):  # conditional jumps must land inside the program
            assert index + 1 + jt < len(program) and index + 1 + jf < len(program)
    assert program[-1][3] & 0xFFFF == 38 and program[-2][3] & 0xFFFF == 1  # ENOSYS, EPERM returns
    denied = launcher._DENIED_SYSCALLS[machine]
    for name in ("ptrace", "mount", "unshare", "setns", "bpf", "io_uring_setup", "pivot_root", "keyctl", "process_vm_writev"):
        assert name in denied, f"{name} is not blocked"
        assert any(insn[0] == 0x15 and insn[3] == denied[name] for insn in program)
    assert launcher._seccomp_program("riscv64") is None  # unknown architecture: no guessing


def test_landlock_masks_cover_every_filesystem_right_of_the_abi() -> None:
    launcher = _load_launcher()
    handled, read, write = launcher._landlock_masks(5)
    assert read & write == 0 and handled == read | write
    assert launcher._landlock_masks(1)[0] < handled  # older kernels handle fewer rights, never more


def test_bubblewrap_and_seatbelt_profiles_keep_secrets_and_network_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/Users/me")
    policy = Policy(tmp_path)
    bwrap = sandbox.wrap_argv(["x"], policy, "bwrap")
    assert "--unshare-net" in bwrap and "--cap-drop" in bwrap and "--new-session" in bwrap
    profile = sandbox.wrap_argv(["x"], policy, "sandbox-exec")[2]
    for secret in (".ssh", ".aws", ".rbit", ".gnupg"):
        assert f'"/Users/me/{secret}"' in profile
    assert "(deny network*)" in profile and "(deny file-write*)" in profile
    assert "(deny network*)" not in sandbox.wrap_argv(["x"], policy.with_network(True), "sandbox-exec")[2]


def test_strict_without_a_backend_never_starts_the_program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "backend", lambda: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.ensure({"shell_sandbox": "strict"})
    sandbox.ensure({"shell_sandbox": "auto"})  # auto and off simply run unconfined
    with pytest.raises(sandbox.SandboxUnavailable):
        asyncio.run(sandbox.spawn(["echo", "hi"], Policy(tmp_path)))


# ----------------------------------------------- real isolation (needs a backend on this OS)


@needs_backend
def test_selftest_proves_isolation_on_this_machine() -> None:
    from rbit.sandbox.selftest import run_checks

    checks = run_checks()
    failed = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
    assert not failed, "the sandbox does not isolate:\n" + "\n".join(failed)
    kinds = {c.expect for c in checks}
    assert kinds == {"works", "denied"}  # both halves were measured
    assert len(checks) >= 12


def _probe_script(workspace: Path) -> Path:
    script = workspace / "probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, socket, sys
            mode, arg = sys.argv[1], sys.argv[2]
            try:
                if mode == "read":
                    print(open(arg).read())
                elif mode == "write":
                    open(arg, "w").write("x"); print("wrote")
                elif mode == "connect":
                    socket.create_connection(("127.0.0.1", int(arg)), 3); print("con" + "nected")
                elif mode == "env":
                    print("canary=" + repr(os.environ.get(arg)))
            except Exception as exc:
                print("DENIED " + type(exc).__name__)
                sys.exit(3)
            """
        ),
        encoding="utf-8",
    )
    return script


def _shell(script: Path, mode: str, arg: str | Path) -> str:
    """The same probe invoked through the platform's real shell syntax."""
    py = sys.executable
    return f'& "{py}" "{script}" {mode} "{arg}"' if sys.platform == "win32" else f'"{py}" "{script}" {mode} "{arg}"'


@needs_backend
@pytest.mark.asyncio
async def test_bash_tool_runs_commands_confined_to_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    script = _probe_script(workspace)
    monkeypatch.setenv("RBIT_SANDBOX_CANARY", "leak-me")
    cfg = {"shell_sandbox": "strict"}
    ex = ToolExecutor(workspace=workspace, session=Session(), get_config=lambda: cfg)
    ex.always_allow_bash = True

    inside = await ex.execute("bash", {"command": _shell(script, "write", workspace / "made.txt")})
    assert inside.ok and (workspace / "made.txt").exists(), inside.output

    read = await ex.execute("bash", {"command": _shell(script, "read", outside / "secret.txt")})
    assert SECRET not in read.output and "DENIED" in read.output

    planted = outside / "planted.txt"
    await ex.execute("bash", {"command": _shell(script, "write", planted)})
    assert not planted.exists()

    env = await ex.execute("bash", {"command": _shell(script, "env", "RBIT_SANDBOX_CANARY")})
    assert "leak-me" not in env.output and "None" in env.output

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(0.3)
        net = await ex.execute("bash", {"command": _shell(script, "connect", server.getsockname()[1])})
        with pytest.raises((socket.timeout, OSError)):
            server.accept()  # nothing ever connected
    assert "connected" not in net.output


@needs_backend
@pytest.mark.asyncio
async def test_persistent_terminal_is_confined_too(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    cfg = {"shell_sandbox": "strict"}
    ex = ToolExecutor(workspace=workspace, session=Session(), get_config=lambda: cfg)
    ex.always_allow_bash = True
    reader = f'type "{outside / "secret.txt"}"' if sys.platform == "win32" else f'cat "{outside / "secret.txt"}"'
    try:
        started = await ex.execute("terminal", {"action": "start"})
        assert started.ok, started.output
        result = await ex.execute("terminal", {"command": reader})
        assert SECRET not in result.output
        own = await ex.execute("terminal", {"command": "echo still-works"})
        assert "still-works" in own.output  # not merely broken: it runs, it just cannot reach the secret
    finally:
        await ex.execute("terminal", {"action": "close"})


_FAKE_MCP = '''
import json, os, socket, sys

def probe():
    out = {}
    try:
        out["outside"] = open(os.environ["PROBE_OUTSIDE"]).read()
    except Exception as exc:
        out["outside"] = "DENIED " + type(exc).__name__
    try:
        socket.create_connection(("127.0.0.1", int(os.environ["PROBE_PORT"])), 3)
        out["network"] = "reachable"
    except Exception as exc:
        out["network"] = "DENIED " + type(exc).__name__
    out["canary"] = os.environ.get("RBIT_SANDBOX_CANARY")
    out["declared"] = os.environ.get("PROBE_OUTSIDE") is not None  # the server's own env still arrives
    return out

def reply(msg_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method, msg_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        reply(msg_id, {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}})
    elif method == "tools/list":
        reply(msg_id, {"tools": [{"name": "probe", "description": "d", "inputSchema": {"type": "object", "properties": {}}}]})
    elif method == "tools/call":
        reply(msg_id, {"content": [{"type": "text", "text": json.dumps(probe())}]})
'''


@needs_backend
@pytest.mark.asyncio
async def test_an_mcp_server_is_confined_and_still_gets_its_declared_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rbit.mcp import MCPManager

    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    (workspace / "server.py").write_text(_FAKE_MCP, encoding="utf-8")
    monkeypatch.setenv("RBIT_SANDBOX_CANARY", "leak-me")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        cfg = {
            "shell_sandbox": "strict",
            "mcp": {
                "fake": {
                    "type": "local",
                    "command": [sys.executable, str(workspace / "server.py")],
                    "environment": {"PROBE_OUTSIDE": str(outside / "secret.txt"), "PROBE_PORT": str(listener.getsockname()[1])},
                }
            },
        }
        manager = MCPManager.from_config(cfg, workspace)
        try:
            await manager.start()
            assert manager.status["fake"]["connected"], manager.status
            result = await manager.call("fake_probe", {})
        finally:
            await manager.close()
    report = json.loads(result.output)
    assert SECRET not in result.output and report["outside"].startswith("DENIED"), report
    assert report["network"].startswith("DENIED"), report
    assert report["canary"] is None, report
    assert report["declared"] is True, report


@needs_backend
def test_sync_helpers_and_formatters_run_confined(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    policy = policy_from_config({"shell_sandbox": "strict"}, workspace)
    done = sandbox.run_sync([sys.executable, "-c", "print(6*7)"], policy, timeout=60)
    assert done.returncode == 0 and b"42" in done.stdout
    code = f"print(open(r'{outside / 'secret.txt'}').read())"
    spy = sandbox.run_sync([sys.executable, "-c", code], policy, timeout=60)
    assert spy.returncode != 0 and SECRET.encode() not in spy.stdout


@needs_backend
def test_a_command_that_runs_too_long_is_killed_with_its_children(tmp_path: Path) -> None:
    import subprocess

    policy = policy_from_config({"shell_sandbox": "strict"}, tmp_path)
    code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox.run_sync([sys.executable, "-c", code], policy, timeout=3)


# ------------------------------------------------------------- platform specific proofs


@pytest.mark.skipif(sys.platform != "win32" or sandbox.backend() is None, reason="Windows AppContainer only")
@pytest.mark.asyncio
async def test_windows_project_state_directory_is_closed_to_the_sandbox(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    state = workspace / ".rbit"
    state.mkdir(parents=True)
    (state / "config.json").write_text('{"api_key": "sk-project-secret"}', encoding="utf-8")
    script = _probe_script(workspace)
    cfg = {"shell_sandbox": "strict"}
    ex = ToolExecutor(workspace=workspace, session=Session(), get_config=lambda: cfg)
    ex.always_allow_bash = True

    read = await ex.execute("bash", {"command": _shell(script, "read", state / "config.json")})
    assert "sk-project-secret" not in read.output and "DENIED" in read.output
    await ex.execute("bash", {"command": _shell(script, "write", state / "audit-tamper.txt")})
    assert not (state / "audit-tamper.txt").exists()
    fine = await ex.execute("bash", {"command": _shell(script, "write", workspace / "ok.txt")})
    assert fine.ok and (workspace / "ok.txt").exists()


def _internet_reachable() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 443), 3).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(sys.platform != "win32" or sandbox.backend() is None, reason="Windows AppContainer only")
@pytest.mark.skipif(not _internet_reachable(), reason="this machine is offline")
@pytest.mark.asyncio
async def test_windows_network_is_a_capability_given_only_on_request(tmp_path: Path) -> None:
    code = "import socket;socket.create_connection(('1.1.1.1', 443), 5);print('reached')"
    closed = policy_from_config({"shell_sandbox": "strict"}, tmp_path)
    opened = policy_from_config({"shell_sandbox": "strict", "shell_sandbox_network": True}, tmp_path)
    for policy, expected in ((closed, False), (opened, True)):
        proc = await sandbox.spawn([sys.executable, "-c", code], policy)
        out, err = await asyncio.wait_for(proc.communicate(), 60)
        assert (b"reached" in out) is expected, (expected, out, err)


@pytest.mark.skipif(sys.platform != "win32" or sandbox.backend() is None, reason="Windows AppContainer only")
def test_windows_refuses_to_open_the_whole_user_profile_to_the_sandbox() -> None:
    from rbit.sandbox import windows

    with pytest.raises(windows.WindowsSandboxError):
        windows.grant(Path.home(), "M")
    with pytest.raises(windows.WindowsSandboxError):
        windows.grant(Path(Path.home().anchor), "RX")


@pytest.mark.skipif(not sys.platform.startswith("linux") or sandbox.backend() is None, reason="Linux only")
def test_linux_launcher_refuses_to_run_unconfined_when_setup_fails(tmp_path: Path) -> None:
    import subprocess

    launcher = Path(sandbox.__file__).with_name("_linux_exec.py")
    bad = json.dumps({"workspace": str(tmp_path), "read": [], "write": [], "network": False})
    proc = subprocess.run(
        [sys.executable, str(launcher), bad, "--", "/definitely/not/a/program"], capture_output=True, timeout=30
    )
    assert proc.returncode in (126, 127)  # reported, never silently run without the restrictions
    probe = subprocess.run([sys.executable, str(launcher), "{not json", "--", "/bin/true"], capture_output=True, timeout=30)
    assert probe.returncode == 2


# ------------------------------------------------------------------------------- CLI


def test_cli_sandbox_status_reports_backend_and_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rbit.cli import main

    (tmp_path / ".rbit").mkdir()
    code = main(["sandbox", "status", str(tmp_path)])
    out = capsys.readouterr().out
    assert f"platform: {sys.platform}" in out and "backend:" in out and "mode: off" in out
    assert code == 0


@needs_backend
def test_cli_sandbox_check_passes_on_a_machine_with_a_backend(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rbit.cli import main

    (tmp_path / ".rbit").mkdir()
    assert main(["sandbox", "check", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and "checks passed" in out


def test_project_cannot_widen_the_sandbox_without_trust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rbit.storage import StorageManager

    monkeypatch.delenv("RBIT_TRUST_ALL", raising=False)
    (tmp_path / ".rbit").mkdir()
    (tmp_path / ".rbit" / "config.json").write_text(
        json.dumps({"shell_sandbox_read": ["~"], "shell_sandbox_write": ["~"], "shell_sandbox_env": ["OPENAI_API_KEY"]}),
        encoding="utf-8",
    )
    storage = StorageManager(tmp_path)
    cfg = storage.load_config()
    assert storage.untrusted_keys == ["shell_sandbox_env", "shell_sandbox_read", "shell_sandbox_write"]
    assert "shell_sandbox_read" not in cfg and "shell_sandbox_env" not in cfg
