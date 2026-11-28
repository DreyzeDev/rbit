"""Regressions for the audit fixes: approval prefixes, serve hardening, schedules, TUI keys."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rbit.models import Session
from rbit.storage import StorageManager
from rbit.tools import ToolExecutor
from tests.test_serve_security import _request, _start_server


def _executor(tmp_path: Path) -> ToolExecutor:
    return ToolExecutor(workspace=tmp_path, session=Session(), storage=StorageManager(tmp_path))


# --- "always allow" prefixes -------------------------------------------------


def test_interpreter_prefix_is_exact_only(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    prefix = ex._bash_prefix('python -c "print(1)"')
    assert prefix == 'python -c "print(1)"'  # the whole command, not `python -c`
    ex.allowed_bash_prefixes.append(prefix)
    assert ex._prefix_allowed('python -c "print(1)"')
    assert not ex._prefix_allowed('python -c "import os; os.system(1)"')
    assert not ex._prefix_allowed("python -c anything-else")


@pytest.mark.parametrize("command", ["bash -c 'ls'", "node -e 1", "sudo ls", "npm --prefix x test"])
def test_exec_programs_and_option_second_word_do_not_extend(tmp_path: Path, command: str) -> None:
    ex = _executor(tmp_path)
    prefix = ex._bash_prefix(command)
    assert prefix == command
    ex.allowed_bash_prefixes.append(prefix)
    assert not ex._prefix_allowed(command + " extra")


def test_ordinary_prefix_still_extends(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    prefix = ex._bash_prefix("npm test --silent")
    assert prefix == "npm test"
    ex.allowed_bash_prefixes.append(prefix)
    assert ex._prefix_allowed("npm test --silent")
    assert not ex._prefix_allowed("npm testing")
    assert not ex._prefix_allowed("npm test; rm -rf /")


# --- serve ------------------------------------------------------------------


def test_status_lists_every_mode_the_engine_enforces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _start_server(tmp_path, monkeypatch)
    _s, _h, body = _request(port, "GET", "/v1/agents")
    names = [a["name"] for a in json.loads(body)["agents"]]
    assert names == ["build", "plan", "ask", "debug", "review", "autopilot"]


def test_token_in_query_only_opens_the_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _start_server(tmp_path, monkeypatch, token="secret-token")
    assert _request(port, "GET", "/v1/sessions?token=secret-token")[0] == 401
    assert _request(port, "GET", "/v1/sessions?pair=secret-token")[0] == 401
    assert _request(port, "GET", "/v1/sessions", {"Authorization": "Bearer secret-token"})[0] == 200
    status, headers, _b = _request(port, "GET", "/?token=secret-token")
    assert status == 200
    assert "rbit_pair=secret-token" in headers.get("set-cookie", "")
    assert "SameSite=Strict" in headers["set-cookie"]


def test_cookie_needs_a_trusted_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _start_server(tmp_path, monkeypatch, token="secret-token")
    cookie = {"Cookie": "rbit_pair=secret-token"}
    assert _request(port, "GET", "/v1/sessions", cookie)[0] == 200
    assert _request(port, "GET", "/v1/sessions", {**cookie, "Origin": "https://evil.example"})[0] == 401


def test_vcs_status_neutralises_repo_configured_programs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    marker = tmp_path / "pwned"
    hook = tmp_path / "evil.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=tmp_path, check=True)
    port = _start_server(tmp_path, monkeypatch)
    status, _h, body = _request(port, "GET", "/v1/vcs/status")
    assert status == 200 and json.loads(body)["is_git"]
    assert not marker.exists()


def test_fs_works_when_the_workspace_is_a_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    port = _start_server(link, monkeypatch)
    status, _h, body = _request(port, "GET", "/v1/fs?path=.")
    assert status == 200
    assert any(e["name"] == "main.py" for e in json.loads(body)["entries"])


def test_http_schedule_is_not_unattended_when_approvals_are_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RBIT_APPROVALS", "1")
    port = _start_server(tmp_path, monkeypatch)
    body = json.dumps({"prompt": "run tests", "schedule": "every 30m"}).encode()
    status, _h, raw = _request(port, "POST", "/v1/schedules", {"Content-Type": "application/json"}, body)
    assert status == 201
    assert json.loads(raw)["schedule"]["unattended"] is False


def test_http_schedule_is_unattended_when_approvals_are_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RBIT_APPROVALS", raising=False)
    port = _start_server(tmp_path, monkeypatch)
    body = json.dumps({"prompt": "run tests", "schedule": "every 30m"}).encode()
    _s, _h, raw = _request(port, "POST", "/v1/schedules", {"Content-Type": "application/json"}, body)
    assert json.loads(raw)["schedule"]["unattended"] is True


# --- TUI --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_models_and_history_keys_survive_a_real_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ctrl+m arrives as Enter and ctrl+h as Backspace in a terminal: the F-keys must work."""
    from textual._xterm_parser import XTermParser

    from rbit.app import RbitApp

    keys = {e.key for seq in ("\r", "\x08") for e in XTermParser(debug=False).feed(seq) if hasattr(e, "key")}
    assert keys == {"enter", "backspace"}  # why the old bindings could never fire

    monkeypatch.setenv("HOME", str(tmp_path))
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for key, screen in (("f2", "ModelSelectorModal"), ("f3", "PromptHistoryModal")):
            await pilot.press(key)
            await pilot.pause()
            assert type(app.screen).__name__ == screen
            await pilot.press("escape")
            await pilot.pause()


@pytest.mark.asyncio
async def test_configured_bindings_take_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rbit.app import RbitApp

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".rbit").mkdir()
    (tmp_path / ".rbit" / "config.json").write_text(json.dumps({"bindings": {"open_themes": "f5"}}), encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("f5")
        await pilot.pause()
        assert type(app.screen).__name__ == "ThemeSelectorModal"
        await pilot.press("escape")
        await pilot.press("ctrl+t")  # the default key no longer opens it
        await pilot.pause()
        assert type(app.screen).__name__ != "ThemeSelectorModal"


@pytest.mark.asyncio
async def test_small_terminal_keeps_the_prompt_box_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rbit.app import RbitApp

    monkeypatch.setenv("HOME", str(tmp_path))
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(40, 12)) as pilot:
        await pilot.pause()
        box = app.query_one("#prompt-box")
        assert box.region.bottom <= app.size.height
