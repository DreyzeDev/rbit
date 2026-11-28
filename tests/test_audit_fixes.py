"""Regressions for the audit fixes: approval prefixes, serve hardening, schedules, TUI keys."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rbit.models import Session
from rbit.storage import StorageManager
from rbit.tools import ToolExecutor
from test_serve_security import _request, _start_server


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
    # The page itself is only served from a source checkout (the renderer is not in the wheel):
    # 200 there, 404 from an installed package. Either way the token was accepted (not 401).
    assert status in (200, 404)
    if status == 200:
        assert "rbit_pair=secret-token" in headers.get("set-cookie", "")
        assert "SameSite=Strict" in headers["set-cookie"]
    assert _request(port, "GET", "/?token=wrong")[0] == 401


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


# --- i18n -------------------------------------------------------------------


def test_locale_follows_config_and_environment() -> None:
    from rbit.i18n import detect_locale, set_locale, tr

    assert detect_locale({"LANG": "ru_RU.UTF-8"}) == "ru"
    assert detect_locale({"LC_ALL": "en_US.UTF-8", "LANG": "ru_RU.UTF-8"}) == "en"
    assert detect_locale({}) == "en"
    try:
        assert set_locale("ru") == "ru"
        assert tr("agent_busy").startswith("Агент занят")
        assert set_locale("en") == "en"
        assert tr("agent_busy").startswith("Agent is busy")
        assert tr("auto_compact", pct=81.4) == "Context 81% — auto-compacting…"
        assert set_locale("klingon") == "en"
        assert tr("no-such-key") == "no-such-key"
    finally:
        set_locale("en")


def test_every_russian_string_has_an_english_twin() -> None:
    from rbit.i18n import _STRINGS

    assert set(_STRINGS["ru"]) == set(_STRINGS["en"])
    for key, text in _STRINGS["ru"].items():
        # same placeholders in both languages
        import re

        assert sorted(re.findall(r"\{(\w+)", text)) == sorted(re.findall(r"\{(\w+)", _STRINGS["en"][key])), key


# --- `rbit plugin` ----------------------------------------------------------


def test_plugin_subcommands_are_routed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rbit.cli import _is_package_plugin_command, main as cli_main

    assert _is_package_plugin_command(["add", "pkg"])
    assert _is_package_plugin_command(["check"])
    assert not _is_package_plugin_command(["install", "x"])
    assert not _is_package_plugin_command(["list"])
    assert _is_package_plugin_command(["remove", "not-installed", "-d", str(tmp_path)])
    assert cli_main(["plugin", "check", "-d", str(tmp_path)]) == 0
    assert "No updates available" in capsys.readouterr().out


# --- schedules --------------------------------------------------------------


def test_schedule_input_is_validated(tmp_path: Path) -> None:
    from rbit.scheduler import MAX_SCHEDULES, Scheduler

    sched = Scheduler(tmp_path)
    assert sched.add("x" * 9000, "every 30m")[0] is None
    assert "unknown agent" in sched.add("do it", "every 30m", agent="root")[1]
    assert sched.add("do it", "every 30m", model="has space")[0] is None
    entry, err = sched.add("do it", "every 30m", name="  a   b  ", agent="REVIEW")
    assert err == "" and entry is not None
    assert entry["name"] == "a b" and entry["agent"] == "review"
    for _ in range(MAX_SCHEDULES - 1):
        assert sched.add("p", "every 30m")[0] is not None
    assert "too many schedules" in sched.add("p", "every 30m")[1]


# --- sandbox default from the environment -----------------------------------


def test_env_sets_the_default_sandbox_mode_but_config_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RBIT_SHELL_SANDBOX", "auto")
    assert StorageManager(tmp_path).load_config()["shell_sandbox"] == "auto"
    monkeypatch.setenv("RBIT_SHELL_SANDBOX", "nonsense")
    assert "shell_sandbox" not in StorageManager(tmp_path).load_config()
    monkeypatch.setenv("RBIT_SHELL_SANDBOX", "auto")
    (tmp_path / ".rbit").mkdir(exist_ok=True)
    (tmp_path / ".rbit" / "config.json").write_text('{"shell_sandbox": "off"}', encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))  # home-dir workspace: config is fully trusted
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert StorageManager(tmp_path).load_config()["shell_sandbox"] == "off"


# --- sandbox -----------------------------------------------------------------


def test_unavailable_message_does_not_claim_strict_in_status_output() -> None:
    from rbit import sandbox

    assert "`strict`" in sandbox.unavailable_message()  # a refused command: strict was configured
    soft = sandbox.unavailable_message(strict=False)
    assert "`strict`" not in soft and "cannot be turned on" in soft


def test_bwrap_protects_git_config_and_system_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rbit.sandbox import linux
    from rbit.sandbox.policy import policy_from_config

    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    monkeypatch.setattr("os.path.lexists", lambda p: str(p) in ("/etc/shadow", "/etc/ssl/private") or Path(p).exists())
    monkeypatch.setattr("os.path.isdir", lambda p: str(p) == "/etc/ssl/private" or Path(p).is_dir())
    argv = linux.bwrap_argv(["true"], policy_from_config({}, tmp_path))
    joined = " ".join(argv)
    assert f"--ro-bind-try {tmp_path}/.git/config {tmp_path}/.git/config" in joined
    assert f"--ro-bind-try {tmp_path}/.git/hooks {tmp_path}/.git/hooks" in joined
    assert "--ro-bind /dev/null /etc/shadow" in joined
    assert "--tmpfs /etc/ssl/private" in joined
    # the secrets are masked AFTER the whole root was bound, or they would be exposed again
    assert joined.index("--ro-bind / /") < joined.index("/etc/shadow")


def test_macos_profile_protects_git_config_like_the_other_backends(tmp_path: Path) -> None:
    from rbit.sandbox import macos
    from rbit.sandbox.policy import policy_from_config

    profile = macos.profile(policy_from_config({}, tmp_path))
    resolved = str(tmp_path.resolve())
    assert f'(deny file-write* (literal "{resolved}/.git/config"))' in profile
    assert f'(deny file-write* (subpath "{resolved}/.git/hooks"))' in profile


# --- release templates -------------------------------------------------------


def test_render_distribution_fills_every_placeholder(tmp_path: Path) -> None:
    import hashlib
    import importlib.util
    import json as _json

    spec = importlib.util.spec_from_file_location(
        "render_distribution", Path(__file__).resolve().parent.parent / "scripts" / "render_distribution.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sdist = tmp_path / "rbit-1.7.0.tar.gz"
    sdist.write_bytes(b"sdist bytes")
    zip_path = tmp_path / "rbit-1.7.0-windows-x64.zip"
    zip_path.write_bytes(b"zip bytes")
    out = tmp_path / "out"
    assert mod.main(["--version", "1.7.0", "--sdist", str(sdist), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["rbit.rb"]  # no Windows zip: no scoop/winget
    brew = (out / "rbit.rb").read_text(encoding="utf-8")
    assert 'version "1.7.0"' in brew and hashlib.sha256(b"sdist bytes").hexdigest() in brew
    assert "<VERSION>" not in brew and "PLACEHOLDER" not in brew

    assert mod.main(["--version", "1.7.0", "--sdist", str(sdist), "--windows-zip", str(zip_path), "--out", str(out)]) == 0
    scoop = _json.loads((out / "rbit.scoop.json").read_text(encoding="utf-8"))
    assert scoop["version"] == "1.7.0"
    assert scoop["architecture"]["64bit"]["hash"] == hashlib.sha256(b"zip bytes").hexdigest()
    winget = (out / "rbit.winget").read_text(encoding="utf-8")
    assert "PackageVersion: 1.7.0" in winget and "v1.7.0/rbit-1.7.0-windows-x64.zip" in winget
    assert mod.main(["--version", "v1.7", "--sdist", str(sdist), "--out", str(out)]) == 2
