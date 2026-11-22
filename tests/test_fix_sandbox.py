"""Opt-in shell sandbox: argv for each backend, and what `bash`/`terminal` do with it."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from rbit import sandbox
from rbit.models import Session
from rbit.storage import StorageManager
from rbit.tools import ToolExecutor


def _executor(tmp_path: Path, cfg: dict[str, Any]) -> ToolExecutor:
    return ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: cfg)


def test_mode_parsing_defaults_to_off_and_rejects_unknown_values() -> None:
    assert sandbox.mode({}) == "off" and sandbox.mode(None) == "off"
    assert sandbox.mode({"shell_sandbox": "STRICT"}) == "strict"
    assert sandbox.mode({"shell_sandbox": "auto"}) == "auto"
    assert sandbox.mode({"shell_sandbox": "yes please"}) == "off"
    assert sandbox.mode({"shell_sandbox": True}) == "off"


def test_network_is_cut_unless_the_user_explicitly_allows_it() -> None:
    assert sandbox.network_allowed({}) is False
    assert sandbox.network_allowed({"shell_sandbox_network": "true"}) is False  # only a real boolean
    assert sandbox.network_allowed({"shell_sandbox_network": True}) is True


def test_bubblewrap_argv_makes_everything_read_only_but_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/home/me")
    argv = sandbox.wrap("echo hi", tmp_path, network=False, tool="bwrap")
    assert argv[0] == "bwrap" and argv[-3:] == ["/bin/sh", "-c", "echo hi"]
    first = argv.index("--ro-bind")
    assert argv[first : first + 3] == ["--ro-bind", "/", "/"]
    assert "--unshare-net" in argv and "--die-with-parent" in argv
    # home is hidden first, the workspace is bound afterwards so one inside $HOME stays usable
    assert argv.index("/home/me") < argv.index("--bind")
    bind = argv.index("--bind")
    assert argv[bind + 1 : bind + 3] == [str(tmp_path)] * 2
    assert "--unshare-net" not in sandbox.wrap("x", tmp_path, network=True, tool="bwrap")


def test_a_hostile_command_is_one_argument_not_shell_syntax_for_the_wrapper(tmp_path: Path) -> None:
    nasty = 'x"; rm -rf / #'
    for tool in ("bwrap", "sandbox-exec"):
        argv = sandbox.wrap(nasty, tmp_path, tool=tool)
        assert argv.count(nasty) == 1 and argv[-1] == nasty


def test_macos_profile_denies_writes_outside_the_workspace_secrets_and_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/Users/me")
    argv = sandbox.wrap("ls", tmp_path, network=False, tool="sandbox-exec")
    profile = argv[2]
    assert argv[:2] == ["sandbox-exec", "-p"] and "(deny file-write*)" in profile
    assert "(deny network*)" in profile
    assert '"/Users/me/.ssh"' in profile and '"/Users/me/.rbit"' in profile
    assert "(deny network*)" not in sandbox.wrap("ls", tmp_path, network=True, tool="sandbox-exec")[2]


def test_paths_with_quotes_cannot_break_out_of_the_macos_profile(tmp_path: Path) -> None:
    odd = tmp_path / 'a") (allow file-write* (subpath "/'
    profile = sandbox.wrap("ls", odd, tool="sandbox-exec")[2]
    assert '\\"' in profile and '(subpath "/")' not in profile


def test_no_backend_means_no_argv() -> None:
    assert sandbox.wrap("ls", Path("."), tool="nothing") is None


@pytest.mark.asyncio
async def test_strict_refuses_every_shell_command_when_there_is_no_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "backend", lambda: None)
    ex = _executor(tmp_path, {"shell_sandbox": "strict"})
    ex.always_allow_bash = True

    for command in ("echo hi > out.txt", "cat missing.txt"):  # mutating and read-only alike
        result = await ex.execute("bash", {"command": command})
        assert result.ok is False and "no sandbox backend" in result.output
    assert not (tmp_path / "out.txt").exists()


@pytest.mark.asyncio
async def test_auto_and_off_run_unsandboxed_without_a_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "backend", lambda: None)
    for mode in ("auto", "off"):
        ex = _executor(tmp_path, {"shell_sandbox": mode})
        ex.always_allow_bash = True
        assert (await ex.execute("bash", {"command": f"echo {mode} > {mode}.txt"})).ok is True
        assert (tmp_path / f"{mode}.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no argv wrapper: it creates the process itself (tests/test_sandbox_isolation.py)")
@pytest.mark.asyncio
async def test_with_a_backend_the_command_is_started_through_the_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "backend", lambda: "bwrap")
    seen: list[list[str]] = []

    async def fake_exec(*argv: str, **_kw: Any) -> None:
        seen.append(list(argv))
        raise RuntimeError("stop")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ex = _executor(tmp_path, {"shell_sandbox": "auto"})
    with pytest.raises(RuntimeError):
        await ex._spawn_shell("echo hi", capture=True)

    assert seen and seen[0][0] == "bwrap" and seen[0][-1] == "echo hi" and "--unshare-net" in seen[0]
    off = _executor(tmp_path, {})
    seen.clear()

    async def fake_shell(command: str, **_kw: Any) -> None:  # POSIX starts unsandboxed commands through a shell
        seen.append(["<shell>", command])
        raise RuntimeError("stop")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    with pytest.raises(RuntimeError):
        await off._spawn_shell("echo hi", capture=True)
    assert seen and seen[0][0] != "bwrap"


@pytest.mark.asyncio
async def test_persistent_terminal_starts_inside_the_sandbox_with_the_workspace_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "backend", lambda: "bwrap")
    seen: list[Any] = []

    async def fake_spawn(argv: list[str], policy: Any, **_kw: Any) -> None:
        seen.append((argv, policy))
        raise sandbox.SandboxUnavailable("stop")

    monkeypatch.setattr(sandbox, "spawn", fake_spawn)
    ex = _executor(tmp_path, {"shell_sandbox": "auto"})
    ex.always_allow_bash = True

    result = await ex.execute("terminal", {"action": "start"})

    assert result.ok is False and seen, "the terminal did not go through the sandbox"
    argv, policy = seen[0]
    assert policy.workspace == tmp_path.resolve() and policy.network is False


def test_a_project_cannot_switch_the_sandbox_off_or_open_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RBIT_TRUST_ALL", raising=False)
    (tmp_path / ".rbit").mkdir()
    config = tmp_path / ".rbit" / "config.json"
    config.write_text(json.dumps({"shell_sandbox": "off", "shell_sandbox_network": True}), encoding="utf-8")
    storage = StorageManager(tmp_path)
    cfg = storage.load_config()
    assert storage.untrusted_keys == ["shell_sandbox", "shell_sandbox_network"]
    assert sandbox.mode(cfg) == "off" and sandbox.network_allowed(cfg) is False  # defaults, not the project's value

    config.write_text(json.dumps({"shell_sandbox": "strict", "shell_sandbox_network": False}), encoding="utf-8")
    narrowed = StorageManager(tmp_path)
    assert sandbox.mode(narrowed.load_config()) == "strict"
    assert narrowed.untrusted_keys == []  # narrowing needs no trust
