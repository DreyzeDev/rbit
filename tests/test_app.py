"""Automated headless TUI & CLI tests for rbit."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from rbit.agent import AgentEngine, MAX_PARALLEL_TOOLS
from rbit.app import RbitApp
from rbit.cli import main as cli_main
from rbit.models import ChatMessage, Session, ToolCallRecord, short_id
from rbit.tools import ToolExecutor, ToolResult, is_secret_file, redact_secrets
from rbit.widgets import (
    AgentModeBadge,
    AutocompletePopup,
    HomeLogo,
    HomeTipBanner,
    SessionHeaderBar,
    SidebarPanel,
)


@pytest.mark.asyncio
async def test_rbit_home_screen_and_agent_toggle(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        assert app.query_one(HomeLogo).display is True
        assert app.query_one(HomeTipBanner).display is True
        assert app.query_one(SessionHeaderBar).display is False
        assert app.query_one(SidebarPanel).display is False

        badge = app.query_one(AgentModeBadge)
        assert app.agent_mode == "build"
        assert "Build" in str(badge.render())

        await pilot.press("tab")
        await pilot.pause()
        assert app.agent_mode == "plan"

        await pilot.press("tab")
        await pilot.pause()
        assert app.agent_mode == "build"


@pytest.mark.asyncio
async def test_session_transition_todo_diff_undo_and_export(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "rbit/smart-local"
    async with app.run_test(size=(130, 40)) as pilot:
        inp = app.query_one("#prompt-input")
        inp.focus()
        inp.value = "/todo Проверить интерфейс rbit"
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.session.todos) == 1
        assert app.query_one(SidebarPanel).display is True

        inp.focus()
        inp.value = "создай файл calc.py с калькулятором"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.query_one(HomeLogo).display is False
        assert app.query_one(SessionHeaderBar).display is True
        assert app.query_one(SidebarPanel).display is True

        created = tmp_path / "calc.py"
        assert created.exists()
        assert "calc.py" in app.session.modified_files
        assert len(app.session.todos) >= 3

        # /undo removes the created file
        inp.focus()
        inp.value = "/undo"
        await pilot.press("enter")
        await pilot.pause()
        assert not created.exists()

        # /redo restores the file
        inp.focus()
        inp.value = "/redo"
        await pilot.press("enter")
        await pilot.pause()
        assert created.exists()

        # /export writes Markdown transcript
        inp.focus()
        inp.value = "/export session_report.md"
        await pilot.press("enter")
        await pilot.pause()
        assert (tmp_path / "session_report.md").exists()

        # /new returns cleanly back to centered Home View
        inp.focus()
        inp.value = "/new"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(HomeLogo).display is True
        assert app.query_one(SessionHeaderBar).display is False


@pytest.mark.asyncio
async def test_bang_shell_checkpoints_and_memory(tmp_path: Path) -> None:
    sample = tmp_path / "notes.txt"
    sample.write_text("v1\n", encoding="utf-8")

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        inp = app.query_one("#prompt-input")

        # 1. Save checkpoint v1
        inp.focus()
        inp.value = "/checkpoint before-edit"
        await pilot.press("enter")
        await pilot.pause()
        cps = app.storage.list_checkpoints()
        assert len(cps) == 1
        assert cps[0].label == "before-edit"

        # Mutate workspace + create extra file
        sample.write_text("v2-mutated\n", encoding="utf-8")
        extra = tmp_path / "temp_extra.py"
        extra.write_text("x = 42\n", encoding="utf-8")

        # Rewind via executor
        app.executor.restore_checkpoint(cps[0].id)
        assert sample.read_text(encoding="utf-8") == "v1\n"
        assert not extra.exists()

        # 2. Persistent project memory (/remember)
        inp.focus()
        inp.value = "/remember Always use pytest and type hints"
        await pilot.press("enter")
        await pilot.pause()
        assert "Always use pytest" in app.storage.load_project_memory()

        # 3. Direct !command bang shell (zero LLM tokens)
        inp.focus()
        inp.value = "!echo rbit-bang-ok"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(HomeLogo).display is False
        assert len(app.session.messages) == 0  # Zero LLM messages/tokens used!


@pytest.mark.asyncio
async def test_autocomplete_and_modals(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        inp = app.query_one("#prompt-input")
        popup = app.query_one(AutocompletePopup)

        inp.value = "/ch"
        await pilot.pause()
        assert popup.display is True

        inp.value = "прочитай @ma"
        await pilot.pause()
        assert popup.display is True
        popup.display = False

        sidebar = app.query_one(SidebarPanel)
        assert sidebar.display is False
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.display is True

        for shortcut in ("ctrl+p", "ctrl+m", "ctrl+t", "ctrl+o"):
            await pilot.press(shortcut)
            await pilot.pause()
            assert len(app.screen_stack) == 2
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1


def test_cli_subcommands_headless_run_doctor_stats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 1. rbit run --json (explicitly using deterministic built-in rbit/smart-local)
    rc = cli_main(["run", "--json", "-m", "rbit/smart-local", "-d", str(tmp_path), "создай файл hello_cli.py"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert (tmp_path / "hello_cli.py").exists()
    assert payload["agent"] == "build"

    # 2. rbit doctor
    assert cli_main(["doctor", str(tmp_path)]) == 0
    capsys.readouterr()

    # 3. rbit stats & sessions & export
    assert cli_main(["stats", str(tmp_path)]) == 0
    assert cli_main(["sessions", str(tmp_path)]) == 0
    out_md = tmp_path / "cli_export.md"
    assert cli_main(["export", "-d", str(tmp_path), "-o", str(out_md)]) == 0
    assert out_md.exists()


@pytest.mark.asyncio
async def test_parallel_readonly_tool_calls_and_semaphore_limit(tmp_path: Path) -> None:
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text(f"payload-{i}\n", encoding="utf-8")

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session)

    active = 0
    peak_active = 0
    orig_execute = executor.execute

    async def slow_execute(name: str, args: dict[str, Any], agent_mode: str = "build") -> ToolResult:
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        try:
            await asyncio.sleep(0.10)
            return await orig_execute(name, args, agent_mode=agent_mode)
        finally:
            active -= 1

    executor.execute = slow_execute  # type: ignore[method-assign]

    captured_requests: list[dict[str, Any]] = []
    step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal step
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append(body)
        step += 1
        if step == 1:
            tcs = [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "read", "arguments": json.dumps({"filePath": f"f{i}.txt"})},
                }
                for i in range(6)
            ]
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": tcs}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Все 6 файлов прочитаны."}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {"api_key": "test-key", "api_base": "https://mock.local/v1"},
            http_client=mock_client,
        )
        user_msg = ChatMessage(id=short_id(), role="user", content="прочитай 6 файлов")
        session.messages.append(user_msg)

        async def _noop_rec(_rec: ToolCallRecord) -> None:
            return

        async def _noop_status(_s: str) -> None:
            return

        t0 = time.perf_counter()
        reply = await engine.run_turn(
            session=session,
            user_msg=user_msg,
            agent_mode="build",
            model_id="openai/gpt-4.1",
            on_tool_start=_noop_rec,
            on_tool_end=_noop_rec,
            on_status=_noop_status,
        )
        elapsed = time.perf_counter() - t0

    assert reply.content == "Все 6 файлов прочитаны."
    assert 2 <= peak_active <= MAX_PARALLEL_TOOLS
    # Sequential 6 * (0.05 + 0.10)s would take >= 0.90s; bounded parallel (4 + 2) takes ~0.35s
    assert elapsed < 0.75

    second_req_msgs = captured_requests[1]["messages"]
    tool_msgs = [m for m in second_req_msgs if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == [f"call_{i}" for i in range(6)]
    for i, m in enumerate(tool_msgs):
        assert f"payload-{i}" in m["content"]


@pytest.mark.asyncio
async def test_mixed_tool_calls_preserve_write_barrier(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("v1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b-init\n", encoding="utf-8")

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session)
    captured_requests: list[dict[str, Any]] = []
    step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal step
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append(body)
        step += 1
        if step == 1:
            tcs = [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"filePath":"a.txt"}'}},
                {"id": "c2", "type": "function", "function": {"name": "read", "arguments": '{"filePath":"b.txt"}'}},
                {
                    "id": "c3",
                    "type": "function",
                    "function": {"name": "write", "arguments": '{"filePath":"a.txt","content":"v2\\n"}'},
                },
                {"id": "c4", "type": "function", "function": {"name": "read", "arguments": '{"filePath":"a.txt"}'}},
            ]
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": tcs}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Барьер соблюдён."}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {"api_key": "test-key", "api_base": "https://mock.local/v1"},
            http_client=mock_client,
        )
        user_msg = ChatMessage(id=short_id(), role="user", content="обнови a.txt")
        session.messages.append(user_msg)

        async def _noop_rec(_rec: ToolCallRecord) -> None:
            return

        async def _noop_status(_s: str) -> None:
            return

        reply = await engine.run_turn(
            session=session,
            user_msg=user_msg,
            agent_mode="build",
            model_id="openai/gpt-4.1",
            on_tool_start=_noop_rec,
            on_tool_end=_noop_rec,
            on_status=_noop_status,
        )

    assert reply.content == "Барьер соблюдён."
    tool_msgs = {m["tool_call_id"]: m["content"] for m in captured_requests[1]["messages"] if m["role"] == "tool"}
    assert "1: v1" in tool_msgs["c1"]
    assert "1: b-init" in tool_msgs["c2"]
    assert "Wrote a.txt" in tool_msgs["c3"]
    assert "1: v2" in tool_msgs["c4"]


@pytest.mark.asyncio
async def test_auto_diagnostics_on_write_edit_and_self_heal_turn(tmp_path: Path) -> None:
    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session)

    # 1. Broken Python triggers auto-diagnostics in write, edit fixes it cleanly
    bad_py = await executor.execute("write", {"filePath": "broken.py", "content": "def calc(x, y)\n    return x + y\n"})
    assert bad_py.ok is True
    assert "1 syntax issue" in bad_py.summary
    assert "Diagnostics" in bad_py.output
    assert "SyntaxError" in bad_py.output

    fixed_py = await executor.execute(
        "edit",
        {"filePath": "broken.py", "oldString": "def calc(x, y)", "newString": "def calc(x, y):"},
    )
    assert fixed_py.ok is True
    assert "syntax issue" not in fixed_py.summary
    assert "Diagnostics" not in fixed_py.output

    # 2. Broken JSON & TOML & explicit `diagnostics` tool on workspace
    bad_json = await executor.execute("write", {"filePath": "cfg.json", "content": '{"port": }\n'})
    assert "JSONDecodeError" in bad_json.output

    bad_toml = await executor.execute("write", {"filePath": "cfg.toml", "content": "[server\nport = 80\n"})
    assert "TOMLDecodeError" in bad_toml.output

    ws_diag = await executor.execute("diagnostics", {"filePath": "."})
    assert ws_diag.ok is True
    assert "2 issues" in ws_diag.summary
    assert "cfg.json" in ws_diag.output
    assert "cfg.toml" in ws_diag.output

    # 3. Full agent self-heal loop: step 1 writes broken Python -> receives Diagnostics -> step 2 edits & fixes
    await executor.execute("write", {"filePath": "cfg.json", "content": '{"port": 8080}\n'})
    await executor.execute("write", {"filePath": "cfg.toml", "content": "[server]\nport = 80\n"})

    captured_requests: list[dict[str, Any]] = []
    step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal step
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append(body)
        step += 1
        if step == 1:
            tcs = [
                {
                    "id": "w1",
                    "type": "function",
                    "function": {
                        "name": "write",
                        "arguments": json.dumps({"filePath": "app.py", "content": "def greet(\n    return 'hi'\n"}),
                    },
                }
            ]
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": tcs}}]})
        if step == 2:
            tcs = [
                {
                    "id": "e1",
                    "type": "function",
                    "function": {
                        "name": "edit",
                        "arguments": json.dumps(
                            {"filePath": "app.py", "oldString": "def greet(", "newString": "def greet():"}
                        ),
                    },
                }
            ]
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": tcs}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Исправил синтаксис в том же ходе."}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {"api_key": "test-key", "api_base": "https://mock.local/v1"},
            http_client=mock_client,
        )
        user_msg = ChatMessage(id=short_id(), role="user", content="напиши app.py")
        session.messages.append(user_msg)

        async def _noop_rec(_rec: ToolCallRecord) -> None:
            return

        async def _noop_status(_s: str) -> None:
            return

        reply = await engine.run_turn(
            session=session,
            user_msg=user_msg,
            agent_mode="build",
            model_id="openai/gpt-4.1",
            on_tool_start=_noop_rec,
            on_tool_end=_noop_rec,
            on_status=_noop_status,
        )

    assert reply.content == "Исправил синтаксис в том же ходе."
    step2_tool_msg = [m for m in captured_requests[1]["messages"] if m["role"] == "tool"][0]
    assert "Diagnostics" in step2_tool_msg["content"]
    assert "SyntaxError" in step2_tool_msg["content"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def greet():\n    return 'hi'\n"


@pytest.mark.asyncio
async def test_secret_guard_blocks_env_and_redacts_tokens(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-proj-1234567890abcdefghijklmno\n", encoding="utf-8")
    env_example = tmp_path / ".env.example"
    env_example.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    key_file = tmp_path / "server.pem"
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nabc123xyz\n-----END RSA PRIVATE KEY-----\n", encoding="utf-8")

    assert is_secret_file(env_file) is True
    assert is_secret_file(key_file) is True
    assert is_secret_file(env_example) is False

    cfg = {"allow_secret_reads": False}
    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session, get_config=lambda: cfg)

    # 1. Reading .env and .pem is blocked, while .env.example is allowed
    blocked_env = await executor.execute("read", {"filePath": ".env"})
    assert blocked_env.ok is False
    assert "Secret Guard" in blocked_env.output
    assert "1234567890abcdefghijklmno" not in blocked_env.output

    blocked_pem = await executor.execute("read", {"filePath": "server.pem"})
    assert blocked_pem.ok is False

    allowed_example = await executor.execute("read", {"filePath": ".env.example"})
    assert allowed_example.ok is True

    # 2. Grep skips secret files
    grep_res = await executor.execute("grep", {"pattern": "1234567890"})
    assert grep_res.ok is True
    assert "1234567890" not in grep_res.output

    # 3. Allow override via allow_secret_reads=True, STILL redacting token values in tool output!
    cfg["allow_secret_reads"] = True
    override_res = await executor.execute("read", {"filePath": ".env"})
    assert override_res.ok is True
    assert "sk-[REDACTED]" in override_res.output
    assert "1234567890abcdefghijklmno" not in override_res.output

    # 4. Diff and session markdown export redact GitHub/Slack/OpenAI tokens
    cfg["allow_secret_reads"] = False
    leaked_py = await executor.execute(
        "write",
        {
            "filePath": "client.py",
            "content": 'TOKEN = "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"\nSLACK = "xoxb-123456789012-abcdefghijkl"\n',
        },
    )
    assert "gh_[REDACTED]" in leaked_py.diff
    assert "xox-[REDACTED]" in leaked_py.diff
    assert "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234" not in leaked_py.diff

    session.messages.append(
        ChatMessage(
            id=short_id(),
            role="user",
            content="мой ключ sk-or-v1-abcdefghijklmnopqrstuvwxyz123456",
        )
    )
    exported = executor.storage.export_session_markdown(session, tmp_path / "export.md")
    md_text = exported.read_text(encoding="utf-8")
    assert "sk-[REDACTED]" in md_text
    assert "abcdefghijklmnopqrstuvwxyz123456" not in md_text
    assert redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9") == "Authorization: Bearer [REDACTED]"



