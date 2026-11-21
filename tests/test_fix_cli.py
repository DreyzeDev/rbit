"""Regression tests for CLI / ACP / MCP / LSP / scheduler / service / install fixes."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from rbit.acp import ACPServer
from rbit.models import ChatMessage, short_id


async def _noop_notify(_payload: dict[str, Any]) -> None:
    return None


# --------------------------------------------------------------------------- ACP


@pytest.mark.asyncio
async def test_acp_default_runner_builds_engine_and_saves_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rbit.agent import AgentEngine

    async def fake_run_turn(self: Any, session: Any, user_msg: Any, **kwargs: Any) -> ChatMessage:
        assert callable(self.get_config)
        await kwargs["on_stream_chunk"]("hi")
        return ChatMessage(id=short_id(), role="assistant", content="hi")

    monkeypatch.setattr(AgentEngine, "run_turn", fake_run_turn)
    server = ACPServer(workspace=tmp_path)
    created = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}}, _noop_notify)
    sid = created["result"]["sessionId"]
    resp = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": "yo"}},
        _noop_notify,
    )
    assert resp is not None and resp["result"]["stopReason"] == "end_turn"
    assert [m.role for m in server.sessions[sid].messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_acp_handler_exception_becomes_internal_error(tmp_path: Path) -> None:
    async def runner(prompt: str, session_id: str, send_chunk: Any) -> str:
        raise RuntimeError("boom")

    server = ACPServer(runner=runner, workspace=tmp_path)
    created = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}}, _noop_notify)
    sid = created["result"]["sessionId"]
    resp = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": "x"}},
        _noop_notify,
    )
    assert resp is not None and resp["error"]["code"] == -32603


@pytest.mark.asyncio
async def test_acp_params_not_object_and_bad_protocol_version(tmp_path: Path) -> None:
    server = ACPServer(workspace=tmp_path)
    resp = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": [1]}, _noop_notify)
    assert resp is not None and resp["error"]["code"] == -32602
    init = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "garbage"}}, _noop_notify
    )
    assert init is not None and init["result"]["protocolVersion"] == 1
    from rbit import __version__

    assert init["result"]["agentInfo"]["version"] == __version__


@pytest.mark.asyncio
async def test_acp_cancel_is_processed_while_prompt_runs(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def runner(prompt: str, session_id: str, send_chunk: Any) -> str:
        started.set()
        await asyncio.sleep(30)
        return "end_turn"

    server = ACPServer(runner=runner, workspace=tmp_path)

    class _Reader:
        def __init__(self) -> None:
            self.lines: list[str] = []
            self.sid = ""
            self.step = 0

        def readline(self) -> str:
            import time

            if self.step == 0:
                self.step = 1
                return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}}) + "\n"
            if self.step == 1:
                time.sleep(0.2)
                self.step = 2
                sid = next(iter(server.sessions))
                return json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": "go"}}
                ) + "\n"
            if self.step == 2:
                for _ in range(100):
                    if started.is_set():
                        break
                    time.sleep(0.05)
                self.step = 3
                sid = next(iter(server.sessions))
                return json.dumps({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}) + "\n"
            return ""

    out = io.StringIO()
    await asyncio.wait_for(server.serve(stdin=_Reader(), stdout=out), timeout=15)
    lines = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    prompt_resp = [x for x in lines if x.get("id") == 2][0]
    assert prompt_resp["result"]["stopReason"] == "cancelled"


# --------------------------------------------------------------------------- MCP

_BIG_MCP_SERVER = r'''
import json, os, sys
def send(p):
    sys.stdout.write(json.dumps(p) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get("method"); rid = msg.get("id")
    if m == "initialize":
        # a server-initiated ping reusing the id of our pending request must not be
        # mistaken for the response
        send({"jsonrpc": "2.0", "id": rid, "method": "ping"})
        send({"jsonrpc": "2.0", "id": rid, "result": {"serverInfo": {"name": "big"}}})
    elif m == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "big", "inputSchema": {}}, {"name": "env", "inputSchema": {}}, {"name": "die", "inputSchema": {}}]}})
    elif m == "tools/call":
        name = msg["params"]["name"]
        if name == "big":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "x" * 300000}]}})
        elif name == "env":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": os.environ.get("RBIT_SECRET_KEY", "ABSENT") + "|" + os.environ.get("EXPLICIT", "none")}]}})
        else:
            sys.exit(3)
'''


@pytest.mark.asyncio
async def test_mcp_large_line_ping_collision_env_and_dead_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from rbit.mcp import MCPManager

    monkeypatch.setenv("RBIT_SECRET_KEY", "hunter2")
    script = tmp_path / "big.py"
    script.write_text(_BIG_MCP_SERVER, encoding="utf-8")
    cfg = {
        "mcp": {
            "big": {
                "type": "local",
                "command": [sys.executable, str(script)],
                "timeout": 10,
                "call_timeout": 60,
                "environment": {"EXPLICIT": "yes"},
            }
        }
    }
    manager = MCPManager.from_config(cfg, tmp_path)
    await manager.start()
    try:
        assert manager.status["big"]["connected"] is True, manager.status
        big = await manager.call("big_big", {})
        assert big.ok and len(big.output) > 70_000  # truncated but a >64 KiB line was read
        env = await manager.call("big_env", {})
        assert env.output == "ABSENT|yes"  # RBIT_* secrets are not inherited
        t0 = time.monotonic()
        dead = await manager.call("big_die", {})
        assert dead.ok is False
        assert time.monotonic() - t0 < 20  # fails immediately, not after call_timeout (60s)
    finally:
        await manager.close()


def test_mcp_bad_command_config_does_not_break_manager(tmp_path: Path) -> None:
    from rbit.mcp import MCPManager

    manager = MCPManager.from_config(
        {"mcp": {"bad": {"command": "python 'unterminated"}, "ok": {"url": "http://127.0.0.1:1/mcp"}}}, tmp_path
    )
    assert "ok" in manager.clients
    assert manager.status["bad"]["connected"] is False


# --------------------------------------------------------------------------- LSP


def test_lsp_uri_to_path_and_utf16_columns(tmp_path: Path) -> None:
    from rbit.lsp import LSPClient, LSPServerDef, utf16_to_index, utf16_units

    client = LSPClient(LSPServerDef(name="x", command=["x"], extensions=[".py"]), tmp_path)
    target = tmp_path / "my dir" / "a b.py"
    target.parent.mkdir()
    target.write_text("x\n", encoding="utf-8")
    assert client._uri_to_path(target.as_uri()) == target.resolve()
    assert utf16_units("a\U0001F600b", 2) == 3
    assert utf16_to_index("a\U0001F600b", 3) == 2


@pytest.mark.asyncio
async def test_lsp_handle_message_accepts_string_ids(tmp_path: Path) -> None:
    from rbit.lsp import LSPClient, LSPServerDef

    client = LSPClient(LSPServerDef(name="x", command=["x"], extensions=[".py"]), tmp_path)
    sent: list[dict[str, Any]] = []

    async def fake_send(payload: dict[str, Any]) -> None:
        sent.append(payload)

    client._send = fake_send  # type: ignore[method-assign]
    await client._handle_message({"jsonrpc": "2.0", "id": "srv-1", "method": "workspace/configuration", "params": {"items": [1]}})
    assert sent and sent[0]["id"] == "srv-1"
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    client._pending[7] = fut
    await client._handle_message({"jsonrpc": "2.0", "id": "7", "result": 5})
    assert fut.result() == 5


@pytest.mark.asyncio
async def test_lsp_failed_start_is_negative_cached(tmp_path: Path) -> None:
    from rbit.lsp import LSPClient, LSPServerDef

    client = LSPClient(LSPServerDef(name="x", command=["definitely-not-a-binary-xyz"], extensions=[".py"]), tmp_path)
    assert await client.start() is False
    first = client.failed
    client.definition.command[0] = sys.executable  # would succeed if retried
    assert await client.start() is False
    assert client.failed == first


# ------------------------------------------------- launchers / CI / desktop / iOS

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_windows_launchers_isolate_cwd_and_propagate_exit_code() -> None:
    bat = _read("rbit.bat")
    ps1 = _read("rbit.ps1")
    for text in (bat, ps1):
        # `-P` and PYTHONSAFEPATH exist only on Python 3.11+ (3.10 is supported), and the env
        # var would leak into every Python process the agent starts: neither may be used in
        # code (the comments explaining this may of course mention them).
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().lower().startswith(("rem ", "#"))
        )
        assert " -P " not in code and "PYTHONSAFEPATH" not in code
        assert "sys.path[:] = [p for p in sys.path if p not in ('', '.')]" in code
        assert "runpy.run_module('rbit', run_name='__main__', alter_sys=True)" in text
    assert "exit /b %ERRORLEVEL%" in bat
    assert 'if not exist "%RBIT_PY%"' in bat  # falls back to PATH python without .venv
    assert "@args" in ps1 and "exit $LASTEXITCODE" in ps1
    assert "UTF8" in ps1


def _hijacked_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "rbit").mkdir(parents=True)
    (project / "rbit" / "__init__.py").write_text(
        'print("HIJACKED")\nraise SystemExit(99)\n', encoding="utf-8"
    )
    (project / "json.py").write_text('print("HIJACKED json")\n', encoding="utf-8")
    return project


def _launcher_env() -> dict[str, str]:
    import os

    env = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    env["PYTHONPATH"] = str(REPO_ROOT)  # makes rbit importable without an installed copy
    return env


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launchers")
def test_plain_python_dash_m_is_hijackable_from_the_project_dir(tmp_path: Path) -> None:
    """Documents the threat the launchers close (and proves the fixture is effective)."""
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "rbit", "--version"], cwd=_hijacked_project(tmp_path),
        env=_launcher_env(), capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert "HIJACKED" in proc.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launchers")
@pytest.mark.parametrize("launcher", ["bat", "ps1"])
def test_launchers_ignore_a_package_planted_in_the_project_dir(tmp_path: Path, launcher: str) -> None:
    import subprocess

    command = (
        [str(REPO_ROOT / "rbit.bat")]
        if launcher == "bat"
        else ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO_ROOT / "rbit.ps1")]
    )
    project = _hijacked_project(tmp_path)

    ok = subprocess.run(
        [*command, "--version"], cwd=project, env=_launcher_env(),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert "HIJACKED" not in ok.stdout + ok.stderr
    assert ok.stdout.strip().startswith("rbit v") and ok.returncode == 0

    # Arguments reach rbit and its exit code comes back out of the launcher.
    bad = subprocess.run(
        [*command, "--generate-completion", "tcsh"], cwd=project, env=_launcher_env(),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert bad.returncode == 2 and "Unsupported shell" in bad.stderr


def test_action_installs_from_action_path_and_is_safe_to_call() -> None:
    action = _read("action.yml")
    assert "github.action_path" in action
    assert "actions/checkout" not in action
    assert "if: always()" in action
    assert "-- \"$RBIT_TASK\"" in action
    assert "set -o pipefail" in action
    assert 'color: "blue"' in action  # GitHub accepts only a fixed palette


def test_release_workflow_scopes_permissions_and_gates_publish() -> None:
    workflow = _read(".github/workflows/release.yml")
    top, _, jobs = workflow.partition("\njobs:")
    assert "contents: write" not in top  # write only where a job needs it
    assert "continue-on-error" not in workflow
    assert "needs: test" in workflow
    assert "does not match pyproject version" in workflow


@pytest.mark.parametrize("name", ["ci.yml", "ios.yml", "desktop.yml"])
def test_workflows_default_to_read_only_token(name: str) -> None:
    text = _read(f".github/workflows/{name}")
    top = text.partition("\njobs:")[0]
    assert "permissions:\n  contents: read" in top


def test_pyproject_declares_crypto_and_all_extras() -> None:
    import tomllib

    data = tomllib.loads(_read("pyproject.toml"))
    extras = data["project"]["optional-dependencies"]
    assert any(dep.startswith("cryptography") for dep in extras["crypto"])
    assert "rbit[tokens,mobile,keyring,pdf,browser,crypto]" in extras["all"]


def test_dockerfile_has_healthcheck_and_dockerignore_excludes_secrets() -> None:
    assert "HEALTHCHECK" in _read("Dockerfile")
    ignore = _read(".dockerignore").splitlines()
    assert ".env" in ignore and ".git" in ignore and ".rbit" in ignore


def test_ios_no_arbitrary_loads_and_no_hardcoded_server() -> None:
    plist = _read("ios/Rbit/Info.plist")
    assert "NSAllowsArbitraryLoads" not in plist
    assert "NSAllowsLocalNetworking" in plist
    assert "31.77.11.16" not in _read("ios/Rbit/State/AppSettings.swift")
    assert "31.77.11.16" not in _read("ios/Rbit/Views/AddServerView.swift")


def test_ios_keychain_token_update_path() -> None:
    keychain = _read("ios/Rbit/Networking/KeychainStore.swift")
    assert "SecItemUpdate" in keychain
    assert "errSecItemNotFound" in keychain


def test_desktop_window_is_sandboxed_and_single_instance() -> None:
    main = _read("desktop/main.js")
    assert "sandbox: true" in main
    assert "requestSingleInstanceLock" in main
    assert 'process.platform !== "darwin"' in main
    assert '"/v1/status"' in main  # backend identity is checked with our token


def test_desktop_csp_and_renderer_guards() -> None:
    html = _read("desktop/renderer/index.html")
    assert "Content-Security-Policy" in html and "script-src 'self'" in html
    assert "unsafe-inline" not in html
    app_js = _read("desktop/renderer/app.js")
    assert 'ev.type === "session"' in app_js
    assert "if (busy) return" in app_js


# --- desktop: the bearer token travels over IPC, not in the page URL ---------


def test_desktop_token_is_not_in_the_window_url() -> None:
    root = Path(__file__).resolve().parent.parent / "desktop"
    main = (root / "main.js").read_text(encoding="utf-8")
    preload = (root / "preload.js").read_text(encoding="utf-8")

    assert "query:" not in main  # loadFile no longer carries port/workspace/token
    assert "token: TOKEN" in main and 'ipcMain.on("rbit:config"' in main
    assert 'ipcRenderer.sendSync("rbit:config")' in preload
    assert "location.search" not in preload and "URLSearchParams" not in preload


def test_desktop_config_ipc_only_answers_our_own_renderer() -> None:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    repo = Path(__file__).resolve().parent.parent
    probe = repo / "tests" / "js" / "desktop_ipc_probe.js"
    proc = subprocess.run(
        [node, str(probe), str(repo)], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["registered"] is True
    assert result["oursHasToken"] is True and result["oursHasPort"] is True
    assert result["oursWithQueryAllowed"] is True
    assert result["foreignHttps"] is None  # a foreign page never gets the token
    assert result["siblingDir"] is None  # nor a look-alike folder next to renderer/
    assert result["noFrame"] is None


def test_action_declares_only_inputs_it_uses_and_documents_secrets_correctly() -> None:
    import re

    action = _read("action.yml")
    block = action.split("\ninputs:", 1)[1].split("\noutputs:", 1)[0]
    declared = set(re.findall(r"^  ([A-Za-z0-9_-]+):\s*$", block, flags=re.MULTILINE))
    assert declared == {"task", "model"}  # the unused `github-token` input is gone

    for name in declared:  # every declared input is actually read by a step
        assert f"inputs.{name}" in action

    comments = "\n".join(line for line in action.splitlines() if line.lstrip().startswith("#"))
    assert "RBIT_API_KEY: ${{ secrets.RBIT_API_KEY }}" in comments
    assert "only exists for reusable workflows" in comments  # not advice to use it here


def test_ci_recipe_uses_pipefail_and_the_real_action_path() -> None:
    recipes = (Path(__file__).resolve().parent.parent / "docs" / "site" / "recipes.md").read_text(
        encoding="utf-8"
    )
    assert "set -o pipefail" in recipes
    assert '-- "исправь предупреждения ruff' in recipes
    assert "`.github/action.yml`" not in recipes  # the action lives at the repo root


# --- CLI audit: markup-looking names, unknown credential, unsafe agent names ---------

MARKUP_NAME = "[/x]"  # a closing tag without an opening one: Rich raises MarkupError when it is printed as markup


def test_mcp_remove_and_logout_print_unknown_names_literally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rbit.cli import main as cli_main

    assert cli_main(["mcp", "remove", MARKUP_NAME, "-d", str(tmp_path)]) == 1
    assert MARKUP_NAME in capsys.readouterr().out
    assert cli_main(["mcp", "logout", MARKUP_NAME, "-d", str(tmp_path)]) == 1
    assert MARKUP_NAME in capsys.readouterr().out


def test_auth_export_of_an_unknown_credential_is_an_error_not_a_keyerror(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rbit.cli import main as cli_main

    assert cli_main(["auth", "export", MARKUP_NAME, "-d", str(tmp_path)]) == 1
    assert "Unknown credential" in capsys.readouterr().out


def test_run_prints_a_markup_like_model_id_in_its_footer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rbit.cli import main as cli_main

    # The model is unknown, so the built-in agent answers; the footer line shows the model id.
    cli_main(["run", "-d", str(tmp_path), "-m", MARKUP_NAME, "--", "hello"])
    assert MARKUP_NAME in capsys.readouterr().out


@pytest.mark.parametrize("name", ["../evil", "a/b", MARKUP_NAME, ".hidden", "..", "a..b"])
def test_agent_create_rejects_names_that_could_escape_the_skills_dir(tmp_path: Path, name: str) -> None:
    from rbit.cli import main as cli_main

    assert cli_main(["agent", "create", name, "-d", str(tmp_path)]) == 2
    assert list(tmp_path.rglob("*.md")) == []  # nothing was written anywhere, least of all outside


def test_agent_create_accepts_a_normal_name(tmp_path: Path) -> None:
    from rbit.cli import main as cli_main

    assert cli_main(["agent", "create", "reviewer-2", "-d", str(tmp_path)]) == 0
    assert (tmp_path / ".rbit" / "skills" / "reviewer-2.md").is_file()


# --- desktop: the backend must not import code from the user's workspace ------------

DESKTOP_BOOT = (
    "import sys, runpy; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
    "runpy.run_module('rbit.cli', run_name='__main__', alter_sys=True)"
)


def test_desktop_main_starts_the_backend_with_the_sys_path_bootstrap() -> None:
    main = (REPO_ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    spawn_call = main[main.index("backend = spawn(") : main.index("backend.stderr.on")]
    assert '"-m"' not in spawn_call  # `python -m` puts the workspace first on sys.path
    assert '"-c", boot, "serve"' in spawn_call
    assert "sys.path[:] = [p for p in sys.path if p not in ('', '.')]" in main
    assert "runpy.run_module('rbit.cli', run_name='__main__', alter_sys=True)" in main


def test_the_desktop_bootstrap_ignores_a_package_planted_in_the_workspace(tmp_path: Path) -> None:
    import subprocess

    project = tmp_path / "proj"
    (project / "rbit").mkdir(parents=True)
    (project / "rbit" / "__init__.py").write_text('print("HIJACKED")\nraise SystemExit(99)\n', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    env["PYTHONPATH"] = str(REPO_ROOT)

    plain = subprocess.run(
        [sys.executable, "-m", "rbit.cli", "--help"], cwd=project, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert "HIJACKED" in plain.stdout  # what the desktop app used to do: the planted package wins

    safe = subprocess.run(
        [sys.executable, "-c", DESKTOP_BOOT, "--help"], cwd=project, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert "HIJACKED" not in safe.stdout + safe.stderr
    assert safe.returncode == 0 and "usage" in safe.stdout.lower()
