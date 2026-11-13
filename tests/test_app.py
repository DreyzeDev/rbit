"""Automated headless TUI & CLI tests for rbit."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from textual.widgets import Input, Label, OptionList, Static

from rbit.agent import HTTP_LIMITS, AgentEngine, MAX_PARALLEL_TOOLS, is_reasoning_model
from rbit.app import RbitApp
from rbit.cli import main as cli_main
from rbit.modals import (
    CommandPaletteModal,
    EffortSelectorModal,
    HelpModal,
    ModelSelectorModal,
    SearchResultsModal,
)
from rbit.models import ChatMessage, ModelInfo, Session, ToolCallRecord, search_session, short_id
from rbit.tools import (
    PathOutsideWorkspace,
    ToolExecutor,
    ToolResult,
    is_secret_file,
    redact_secrets,
)
from rbit.widgets import (
    AgentModeBadge,
    AssistantMessageCard,
    AutocompletePopup,
    ChatScroll,
    HomeLogo,
    HomeTipBanner,
    SessionHeaderBar,
    SidebarPanel,
    ToolCallCard as ToolCardWidget,
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


@pytest.mark.asyncio
async def test_auto_checkpoint_before_first_edit_per_turn(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("v1 = 1\n", encoding="utf-8")

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session)
    cps = executor.storage.list_checkpoints()
    assert cps == []

    executor.begin_turn()
    first_write = await executor.execute("write", {"filePath": "b.py", "content": "b = 2\n"})
    assert first_write.ok is True

    cps = executor.storage.list_checkpoints()
    assert len(cps) == 1
    assert cps[0].label == "auto: before first edit of turn"
    assert "b.py" not in str(executor.storage.load_checkpoint_files(cps[0].id)[1])
    first_auto_cp_id = cps[0].id

    # Second mutation in the same turn must NOT create another checkpoint
    second_write = await executor.execute("write", {"filePath": "c.py", "content": "c = 3\n"})
    assert second_write.ok is True
    assert len(executor.storage.list_checkpoints()) == 1

    # New turn -> fresh auto checkpoint captured BEFORE the edit (still holds pre-edit a.py)
    executor.begin_turn()
    edited = await executor.execute("edit", {"filePath": "a.py", "oldString": "v1 = 1", "newString": "v1 = 999"})
    assert edited.ok is True
    assert target.read_text(encoding="utf-8") == "v1 = 999\n"

    cps = executor.storage.list_checkpoints()
    assert len(cps) == 2
    newest = cps[0] if cps[0].id != first_auto_cp_id else cps[1]
    assert newest.label == "auto: before first edit of turn"
    _, newest_files = executor.storage.load_checkpoint_files(newest.id)
    assert newest_files["a.py"] == "v1 = 1\n"

    # /rewind to the FIRST auto checkpoint restores v1 and drops files created after it
    restored = executor.restore_checkpoint(first_auto_cp_id)
    assert "Rewound workspace" in restored
    assert target.read_text(encoding="utf-8") == "v1 = 1\n"
    assert not (tmp_path / "b.py").exists()
    assert not (tmp_path / "c.py").exists()


@pytest.mark.asyncio
async def test_virtual_attachments_and_custom_slash_commands(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def run() -> int:\n    return 42\n", encoding="utf-8")
    cmds_dir = tmp_path / ".rbit" / "commands"
    cmds_dir.mkdir(parents=True)
    (cmds_dir / "review.md").write_text(
        "# Review the current change\nReview this change carefully and suggest fixes.\n\nTarget: {args}\n",
        encoding="utf-8",
    )

    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "rbit/smart-local"
    async with app.run_test(size=(130, 40)) as pilot:
        inp = app.query_one("#prompt-input")

        # @diff / @tree are accepted as virtual attachments even though those files don't exist
        inp.focus()
        inp.value = "@diff @tree объясни изменения"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        user_msg = app.session.messages[0]
        assert user_msg.attachments == ["diff", "tree"]
        assert len(app.session.messages) == 2
        reply = app.session.messages[1].content
        assert "git-diff" in reply
        assert "workspace-tree" in reply

        # Custom project command expands its Markdown template with {args}
        inp.focus()
        inp.value = "/review src/main.py"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        last_user = [m for m in app.session.messages if m.role == "user"][-1]
        assert last_user.content == "Review this change carefully and suggest fixes.\n\nTarget: src/main.py"
        assert app.storage.load_custom_commands()["review"]["description"] == "Review the current change"


@pytest.mark.asyncio
async def test_subagent_explore_and_virtual_attachment_expansion(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def run() -> int:\n    return 42\n", encoding="utf-8")

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session)
    captured: list[dict[str, Any]] = []
    main_step = 0
    sub_step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal main_step, sub_step
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        system = body["messages"][0]["content"]
        if "explore subagent" in system:
            sub_step += 1
            if sub_step == 1:
                tcs = [
                    {
                        "id": "s1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"filePath":"main.py"}'},
                    }
                ]
                return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": tcs}}]})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "main.py:1 defines `run() -> int`, returns 42."}}]},
            )

        main_step += 1
        if main_step == 1:
            tcs = [
                {
                    "id": "sub1",
                    "type": "function",
                    "function": {"name": "subagent", "arguments": '{"goal":"where is run defined?"}'},
                }
            ]
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": tcs}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Функция определена в main.py."}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {"api_key": "test-key", "api_base": "https://mock.local/v1"},
            http_client=mock_client,
        )
        user_msg = ChatMessage(
            id=short_id(), role="user", content="где определена run?", attachments=["tree"]
        )
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

    assert reply.content == "Функция определена в main.py."
    assert reply.tool_calls and reply.tool_calls[0].name == "subagent"
    assert "explore" in reply.tool_calls[0].summary

    main_requests = [b for b in captured if "explore subagent" not in b["messages"][0]["content"]]
    first_user_content = main_requests[0]["messages"][1]["content"]
    assert "<workspace-tree" in first_user_content
    assert "main.py" in first_user_content

    second_main_tool_msg = [m for m in main_requests[1]["messages"] if m["role"] == "tool"][0]
    assert "Subagent explore result" in second_main_tool_msg["content"]
    assert "main.py:1 defines" in second_main_tool_msg["content"]

    sub_requests = [b for b in captured if "explore subagent" in b["messages"][0]["content"]]
    assert sub_requests
    sub_tools = [t["function"]["name"] for t in sub_requests[0]["tools"]]
    assert "read" in sub_tools
    assert "write" not in sub_tools
    assert "bash" not in sub_tools


@pytest.mark.asyncio
async def test_smart_compaction_uses_llm_summary(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "openai/gpt-4.1"
    app.config_data["api_key"] = "test-key"
    app.session.messages = [
        ChatMessage(id=short_id(), role="user", content=f"шаг {i}") for i in range(5)
    ]

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "SUMMARY: prior work on calc.py"}}]},
        )

    async with app.run_test(size=(130, 40)) as pilot:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
            app.engine.http_client = mock_client
            inp = app.query_one("#prompt-input")
            inp.focus()
            inp.value = "/compact"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

    assert len(app.session.messages) == 3
    summary = app.session.messages[0]
    assert summary.content.startswith("**[Compacted]**")
    assert "SUMMARY: prior work on calc.py" in summary.content
    assert captured and "transcript" not in captured[0]["messages"][1]["content"].lower()
    assert "USER: шаг 0" in captured[0]["messages"][1]["content"]
    assert "tools" not in captured[0]


@pytest.mark.asyncio
async def test_retry_and_fork_session(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "rbit/smart-local"
    async with app.run_test(size=(130, 40)) as pilot:
        inp = app.query_one("#prompt-input")
        inp.focus()
        inp.value = "создай файл calc.py с калькулятором"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert len(app.session.messages) == 2
        first_reply_id = app.session.messages[1].id
        old_session_id = app.session.id

        inp.focus()
        inp.value = "/retry"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert len(app.session.messages) == 2
        assert app.session.messages[0].role == "user"
        assert app.session.messages[1].role == "assistant"
        assert app.session.messages[1].id != first_reply_id

        inp.focus()
        inp.value = "/fork 1"
        await pilot.press("enter")
        await pilot.pause()

    assert app.session.id != old_session_id
    assert len(app.session.messages) == 1
    assert app.session.title.endswith("(fork)")
    assert app.storage.find_session(old_session_id) is not None


def test_cli_map_json_and_human(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "readme.md").write_text("# hello\n", encoding="utf-8")

    assert cli_main(["map", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["file_count"] >= 2
    assert payload["total_bytes"] > 0
    assert payload["est_tokens"] == payload["total_bytes"] // 4
    assert "Python" in payload["languages"]
    assert any(item["path"] == "a.py" for item in payload["files"])

    assert cli_main(["map", str(tmp_path)]) == 0
    human = capsys.readouterr().out
    assert "rbit map" in human
    assert "Python" in human

    assert cli_main(["map", str(tmp_path), "--max-tokens", "1", "--json"]) == 0
    trimmed = json.loads(capsys.readouterr().out)
    assert trimmed["truncated"] is True
    assert len(trimmed["files"]) < payload["file_count"]


@pytest.mark.asyncio
async def test_lint_tool_reports_issues(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session)

    bad = await executor.execute("lint", {"filePath": "bad.py"})
    combined = f"{bad.summary}\n{bad.output}".lower()
    assert bad.ok is False
    assert ("syntax" in combined) or ("e999" in combined) or ("invalid" in combined)

    good = await executor.execute("lint", {"filePath": "good.py"})
    assert good.ok is True
    assert "0 issues" in f"{good.summary}\n{good.output}"




@pytest.mark.asyncio
async def test_token_counting_uses_tiktoken_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import rbit.models as models

    class FakeEncoder:
        def encode(self, text: str) -> list[int]:
            return list(range(len(text)))

    monkeypatch.setattr(models, "tiktoken", object())
    monkeypatch.setattr(models, "_ENCODER", FakeEncoder())
    assert models.count_tokens("hello") == 5
    assert models.count_tokens("") == 0

    monkeypatch.setattr(models, "tiktoken", None)
    monkeypatch.setattr(models, "_ENCODER", None)
    assert models.count_tokens("abcdefgh") == 2
    assert models.count_tokens("abc") == 1

    session = Session(messages=[ChatMessage(id=short_id(), role="user", content="abcd")])
    assert session.total_tokens == 1


@pytest.mark.asyncio
async def test_retry_backoff_on_429_and_5xx(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: {})
    engine.retry_base_delay = 0.0
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "rate"})
        if attempts["n"] == 2:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok", "tool_calls": []}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
        ok, content, _ = await engine._non_stream_chat_step(
            client=client,
            url="https://example.test/chat/completions",
            headers={},
            model_id="test",
            messages=[],
        )

    assert ok is True
    assert content == "ok"
    assert attempts["n"] == 3
    assert engine.retry_attempts == 4

    def always_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(always_500)) as client:
        ok, content, _ = await engine._non_stream_chat_step(
            client=client,
            url="https://example.test/chat/completions",
            headers={},
            model_id="test",
            messages=[],
        )
    assert ok is False
    assert "500" in content


def test_ci_workflow_runs_pytest_and_ruff() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pytest -q" in workflow
    assert "ruff check" in workflow
    assert "push" in workflow


@pytest.mark.asyncio
async def test_commit_command_drafts_and_confirms(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True)

    assert git("init").returncode == 0
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "init")
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")

    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "rbit/smart-local"
    async with app.run_test(size=(130, 40)) as pilot:
        inp = app.query_one("#prompt-input")
        inp.focus()
        inp.value = "/commit"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._pending_commit
        draft = app._pending_commit

        inp.focus()
        inp.value = "/commit yes"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert app._pending_commit == ""
    subject = git("log", "-1", "--format=%s").stdout.strip()
    assert subject == draft.splitlines()[0]
    assert git("status", "--porcelain").stdout.strip() == ""

@pytest.mark.asyncio
async def test_gitignore_respected_by_glob_grep_and_listing(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\nsecret_dir/\n", encoding="utf-8")
    (tmp_path / "keep.py").write_text("needle = 1\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("needle in log\n", encoding="utf-8")
    secret = tmp_path / "secret_dir"
    secret.mkdir()
    (secret / "hidden.py").write_text("needle in secret\n", encoding="utf-8")

    executor = ToolExecutor(workspace=tmp_path, session=Session())
    listed = executor.list_workspace_files()
    assert "keep.py" in listed
    assert "debug.log" not in listed
    assert not any(name.startswith("secret_dir/") for name in listed)

    glob_res = await executor.execute("glob", {"pattern": "*.py"})
    assert "keep.py" in glob_res.output
    assert "hidden.py" not in glob_res.output

    grep_res = await executor.execute("grep", {"pattern": "needle"})
    assert "keep.py" in grep_res.output
    assert "debug.log" not in grep_res.output
    assert "hidden.py" not in grep_res.output


@pytest.mark.asyncio
async def test_grep_context_files_only_and_per_file_limit(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("l1\nhit two\nl3\nl4\nhit five\nl6\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("hit one\nx\ny\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session())

    ctx = await executor.execute("grep", {"pattern": r"\bhit\b", "context": 1})
    assert ctx.ok is True
    assert "a.py:2:" in ctx.output
    assert "a.py-1-" in ctx.output
    assert "--" in ctx.output

    only = await executor.execute("grep", {"pattern": r"\bhit\b", "filesOnly": True})
    names = set(only.output.split())
    assert {"a.py", "b.py"} <= names
    assert ":" not in only.output

    limited = await executor.execute("grep", {"pattern": r"\bhit\b", "maxPerFile": 1})
    assert sum(1 for line in limited.output.splitlines() if line.startswith("a.py:")) == 1


@pytest.mark.asyncio
async def test_edit_falls_back_to_fuzzy_whitespace_match(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session())

    res = await executor.execute(
        "edit",
        {"filePath": "f.py", "oldString": "def foo():\n  return 2", "newString": "def foo():\n    return 2"},
    )
    assert res.ok is False

    res = await executor.execute(
        "edit",
        {"filePath": "f.py", "oldString": "def foo():\n  return 1", "newString": "def foo():\n    return 3"},
    )
    assert res.ok is True
    assert "return 3" in (tmp_path / "f.py").read_text(encoding="utf-8")
    assert "fuzzy" in res.output.lower()


@pytest.mark.asyncio
async def test_dangerous_bash_commands_are_denied(tmp_path: Path) -> None:
    from rbit.tools import is_dangerous_command

    assert is_dangerous_command("rm -rf /")
    assert is_dangerous_command("git push --force origin main")
    assert is_dangerous_command("mkfs.ext4 /dev/sda1")
    assert is_dangerous_command("echo hello") == ""

    executor = ToolExecutor(workspace=tmp_path, session=Session())
    res = await executor.execute("bash", {"command": "rm -rf /"})
    assert res.ok is False
    assert "dangerous" in f"{res.summary} {res.output}".lower()


def test_cli_lint_exit_codes_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    assert cli_main(["lint", str(tmp_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["issue_count"] >= 1
    assert "syntax" in payload["output"].lower() or "error" in payload["output"].lower()

    (tmp_path / "bad.py").write_text("x = 1\n", encoding="utf-8")
    assert cli_main(["lint", str(tmp_path)]) == 0
    assert "rbit lint" in capsys.readouterr().out

@pytest.mark.asyncio
async def test_symbols_index_python_js_and_markdown(tmp_path: Path) -> None:
    (tmp_path / "lib.py").write_text(
        "class Greeter:\n"
        "    def hello(self, name: str) -> str:\n"
        "        return name\n\n"
        "def top(a, b=1):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "app.ts").write_text(
        "export function run(x: number) {\n}\n"
        "const go = async (y) => y;\n"
        "class Box extends Base {}\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Title\n## Section\n", encoding="utf-8")

    executor = ToolExecutor(workspace=tmp_path, session=Session())
    py_res = await executor.execute("symbols", {"filePath": "lib.py"})
    assert py_res.ok is True
    assert "class Greeter" in py_res.output
    assert "def hello(self, name: str)" in py_res.output
    assert "def top(a, b=1)" in py_res.output

    all_res = await executor.execute("symbols", {})
    assert "app.ts:" in all_res.output
    assert "function run(x: number)" in all_res.output
    assert "class Box extends Base" in all_res.output
    assert "# Title" in all_res.output


@pytest.mark.asyncio
async def test_git_tool_readonly_actions(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "init")
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    executor = ToolExecutor(workspace=tmp_path, session=Session())
    status = await executor.execute("git", {"action": "status"})
    assert status.ok is True
    assert "a.py" in status.output

    diff = await executor.execute("git", {"action": "diff", "path": "a.py"})
    assert diff.ok is True
    assert "+y = 2" in diff.output

    log = await executor.execute("git", {"action": "log", "limit": 1})
    assert log.ok is True
    assert "init" in log.output

    bad = await executor.execute("git", {"action": "push"})
    assert bad.ok is False

    outside = await executor.execute("git", {"action": "diff", "path": "../outside.txt"})
    assert outside.ok is False


def test_cli_init_creates_rbit_md_and_memory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    assert cli_main(["init", str(tmp_path)]) == 0
    rbit_md = (tmp_path / "RBIT.md").read_text(encoding="utf-8")
    assert tmp_path.name in rbit_md
    assert "Python (1)" in rbit_md
    memory = (tmp_path / ".rbit" / "memory.md").read_text(encoding="utf-8")
    assert "rbit Project Memory" in memory

    (tmp_path / "RBIT.md").write_text("SENTINEL", encoding="utf-8")
    assert cli_main(["init", str(tmp_path)]) == 0
    assert (tmp_path / "RBIT.md").read_text(encoding="utf-8") == "SENTINEL"
    assert cli_main(["init", str(tmp_path), "--force"]) == 0
    assert (tmp_path / "RBIT.md").read_text(encoding="utf-8") != "SENTINEL"
    capsys.readouterr()


def test_cli_sessions_prune_removes_old_entries(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rbit.storage import StorageManager

    storage = StorageManager(tmp_path)
    storage.ensure_dirs()
    old = Session(title="old")
    fresh = Session(title="fresh")
    storage.save_session(old)
    storage.save_session(fresh)
    old_file = storage.sessions_dir / f"{old.id}.json"
    fresh_file = storage.sessions_dir / f"{fresh.id}.json"
    old_ts = time.time() - 90 * 86400
    os.utime(old_file, (old_ts, old_ts))
    checkpoint = storage.checkpoints_dir / "old-cp.json"
    checkpoint.write_text("{}", encoding="utf-8")
    os.utime(checkpoint, (old_ts, old_ts))

    assert cli_main(["sessions", "--prune", "30d", str(tmp_path)]) == 0
    assert not old_file.exists()
    assert fresh_file.exists()
    assert not checkpoint.exists()
    assert "Pruned" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_copy_last_answer_and_slash_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = RbitApp(workspace=tmp_path)
    app.session.messages = [
        ChatMessage(id=short_id(), role="assistant", content="hello copy"),
    ]
    copied: list[str] = []
    monkeypatch.setattr("rbit.app._write_clipboard", lambda text: copied.append(text) or True)

    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert copied == ["hello copy"]

        inp = app.query_one("#prompt-input")
        inp.focus()
        inp.value = "/copy"
        await pilot.press("enter")
        await pilot.pause()

    assert copied[-1] == "hello copy"

@pytest.mark.asyncio
async def test_falls_back_to_smart_local_on_401(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "openai/gpt-4.1"
    app.config_data["api_key"] = "bad-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with app.run_test(size=(130, 40)) as pilot:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
            app.engine.http_client = mock_client
            inp = app.query_one("#prompt-input")
            inp.focus()
            inp.value = "создай файл calc.py с калькулятором"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

    reply = app.session.messages[1]
    assert reply.content.startswith("**[Fallback]**")
    assert "smart" in reply.content.lower()
    assert (tmp_path / "calc.py").exists()


def test_cli_run_reads_stdin_with_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeStdin(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", FakeStdin("проанализируй проект и создай план"))
    assert cli_main(["run", "--stdin", "-m", "rbit/smart-local", "-d", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Обзор проекта" in out or "план" in out.lower()


@pytest.mark.asyncio
async def test_atomic_write_and_ansi_sanitize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rbit.tools import atomic_write_text, strip_ansi

    assert strip_ansi("\x1b[31mred\x1b[0m") == "red"
    assert strip_ansi("plain") == "plain"

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr("rbit.tools.os.replace", spy)
    target = tmp_path / "atomic.txt"
    atomic_write_text(target, "payload")
    assert target.read_text(encoding="utf-8") == "payload"
    assert calls and Path(calls[0][1]) == target
    assert not list(tmp_path.glob("*.tmp"))

    executor = ToolExecutor(workspace=tmp_path, session=Session())
    res = await executor.execute("write", {"filePath": "via_tool.txt", "content": "hi\n"})
    assert res.ok is True
    assert (tmp_path / "via_tool.txt").read_text(encoding="utf-8") == "hi\n"
    assert len(calls) >= 2


def test_checkpoint_rotation_keeps_newest(tmp_path: Path) -> None:
    from rbit.storage import StorageManager

    storage = StorageManager(tmp_path)
    storage.ensure_dirs()
    for idx in range(25):
        storage.save_checkpoint(f"cp {idx}", {"a.txt": "x"})
        time.sleep(0.005)
    kept = storage.list_checkpoints()
    assert len(kept) == StorageManager.MAX_CHECKPOINTS
    labels = {item.label for item in kept}
    assert "cp 24" in labels
    assert "cp 0" not in labels


@pytest.mark.asyncio
async def test_read_tool_rejects_oversized_file(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("x" * 600_000, encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session())

    res = await executor.execute("read", {"filePath": "big.txt"})
    assert res.ok is False
    assert "too large" in res.summary.lower()

    sliced = await executor.execute("read", {"filePath": "big.txt", "offset": 1, "limit": 3})
    assert sliced.ok is True
    assert sliced.output.count("\n") <= 2

@pytest.mark.asyncio
async def test_compact_mode_hides_sidebar_and_high_contrast_theme(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    app.config_data["theme"] = "rbit-contrast"
    app.session.messages = [ChatMessage(id=short_id(), role="assistant", content="hello")]
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        assert app._is_compact is True
        assert app.query_one(SidebarPanel).display is False
        theme_name = getattr(app.theme, "name", app.theme)
        assert theme_name == "rbit-contrast"

    wide = RbitApp(workspace=tmp_path)
    wide.config_data["sidebar_visible"] = True
    wide.session.messages = [ChatMessage(id=short_id(), role="assistant", content="hello")]
    async with wide.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        assert wide._is_compact is False
        assert wide.query_one(SidebarPanel).display is True


def test_cli_commit_draft_json_and_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import subprocess

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "init")
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")

    assert cli_main(["commit", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["applied"] is False
    assert payload["message"].strip()

    assert cli_main(["commit", str(tmp_path), "--apply", "-m", "chore: bump a.txt"]) == 0
    assert "Committed" in capsys.readouterr().out
    assert git("log", "-1", "--format=%s").stdout.strip() == "chore: bump a.txt"
    assert git("status", "--porcelain").stdout.strip() == ""


def test_cli_run_jsonl_event_stream(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(
        [
            "run",
            "-m",
            "rbit/smart-local",
            "--output-format",
            "jsonl",
            "-d",
            str(tmp_path),
            "проанализируй проект",
        ]
    )
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [item["event"] for item in lines]
    assert "tool_start" in events
    assert "tool_end" in events
    assert "message" in events
    assert events[-1] == "done"
    assert lines[-1]["ok"] is True


def test_generate_completion_scripts(capsys: pytest.CaptureFixture[str]) -> None:
    for shell in ("bash", "zsh", "powershell"):
        assert cli_main(["--generate-completion", shell]) == 0
        out = capsys.readouterr().out
        assert "rbit" in out
        assert len(out) > 20
    assert cli_main(["--generate-completion", "tcsh"]) == 2


@pytest.mark.asyncio
async def test_f1_and_slash_help_open_modal(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, HelpModal)
        help_text = str(app.screen.query_one("#help-content").source)
        assert "Keybindings" in help_text

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpModal)

        inp = app.query_one("#prompt-input")
        inp.focus()
        inp.value = "/help"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HelpModal)


@pytest.mark.asyncio
async def test_sidebar_ollama_indicator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from textual.widgets import Static

    async def fake_discover(models: list[ModelInfo] | None = None, **kwargs: Any) -> list[ModelInfo]:
        return models or []

    monkeypatch.setattr(
        "rbit.agent.discover_ollama_models",
        lambda: fake_discover(
            [
                ModelInfo(
                    id="ollama/llama3",
                    name="llama3 (Local)",
                    provider="Ollama Local",
                    context_window=8192,
                    cost_per_1k=0.0,
                )
            ]
        ),
    )
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = str(app.query_one("#sb-ollama", Static).render())
        assert "Ollama" in text
        assert "online" in text
        assert "1 model" in text

    monkeypatch.setattr("rbit.agent.discover_ollama_models", lambda: fake_discover([]))
    offline = RbitApp(workspace=tmp_path)
    offline.config_data["sidebar_visible"] = True
    offline.session.messages = [ChatMessage(id=short_id(), role="assistant", content="hi")]
    async with offline.run_test(size=(130, 40)) as pilot:
        await offline.workers.wait_for_complete()
        await pilot.pause()
        text = str(offline.query_one("#sb-ollama", Static).render())
        assert "offline" in text


def test_custom_extra_headers_are_merged(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    engine = AgentEngine(
        workspace=tmp_path,
        executor=executor,
        get_config=lambda: {"extra_headers": {"X-Org": "acme", "X-Title": "override"}},
    )
    headers = engine._build_headers("secret")
    assert headers["Authorization"] == "Bearer secret"
    assert headers["X-Org"] == "acme"
    assert headers["X-Title"] == "override"
    assert headers["Content-Type"] == "application/json"


def test_cli_logging_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert cli_main(["lint", str(tmp_path), "--quiet"]) == 0
    assert logging.getLogger().level == logging.WARNING
    capsys.readouterr()

    assert cli_main(["lint", str(tmp_path), "--trace"]) == 0
    assert logging.getLogger().level == logging.DEBUG


def test_pre_commit_config_present() -> None:
    root = Path(__file__).resolve().parents[1]
    config = (root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "ruff" in config
    assert "pytest" in config
    assert "mixed-line-ending" in config
    assert "end-of-file-fixer" in config


@pytest.mark.asyncio
async def test_bash_timeout_and_background(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())

    slow = await executor.execute(
        "bash",
        {"command": 'python -c "import time; time.sleep(5)"', "timeout": 1},
    )
    assert slow.ok is False
    assert "timed out" in slow.output.lower()
    assert "background=true" in slow.output

    bg = await executor.execute(
        "bash",
        {"command": 'python -c "import time; time.sleep(0.2)"', "background": True},
    )
    assert bg.ok is True
    assert "background" in bg.summary
    await asyncio.sleep(0.6)
    await executor.terminate_background()


def test_webfetch_converts_html_to_markdown(tmp_path: Path) -> None:
    from rbit.tools import html_to_markdown

    html = (
        "<html><body><nav>menu</nav><h1>Title</h1>"
        "<p>Hello <a href='/x'>link</a></p>"
        "<ul><li>One</li><li>Two</li></ul>"
        "<script>alert(1)</script><h2>Sub</h2></body></html>"
    )
    md = html_to_markdown(html)
    assert "# Title" in md
    assert "## Sub" in md
    assert "[link](/x)" in md
    assert "- One" in md and "- Two" in md
    assert "menu" not in md
    assert "alert(1)" not in md


def test_grep_uses_ripgrep_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.py").write_text("hit\n", encoding="utf-8")
    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = "./a.py:3:hit\n"

    def fake_run(cmd: list[str], **kwargs: Any) -> FakeProc:
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr("rbit.tools.shutil.which", lambda name: "rg" if name == "rg" else None)
    monkeypatch.setattr("rbit.tools.subprocess.run", fake_run)
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    res = executor._tool_grep({"pattern": "hit", "include": "*.py"})
    assert res.ok is True
    assert "a.py:3:hit" in res.output
    assert "(rg)" in res.summary
    assert calls and "--glob" in calls[0] and "*.py" in calls[0]


def test_cli_profile_overlay(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rbit.storage import StorageManager

    storage = StorageManager(tmp_path)
    storage.ensure_dirs()
    storage.save_config(
        {
            "model": "rbit/smart-local",
            "theme": "rbit-dark",
            "profiles": {"work": {"model": "openai/gpt-4.1", "theme": "rbit-light"}},
        }
    )
    assert cli_main(["--profile", "work", "map", str(tmp_path), "--json"]) == 0
    cfg = storage.load_config()
    assert cfg["model"] == "openai/gpt-4.1"
    assert cfg["theme"] == "rbit-light"
    capsys.readouterr()

    assert cli_main(["--profile"]) == 2


def test_workspace_fixture_and_env_isolation(workspace: Path) -> None:
    assert workspace.is_dir()
    assert (workspace / ".rbit").is_dir()
    assert os.environ.get("OPENAI_API_KEY") is None
    assert os.environ.get("RBIT_API_KEY") is None


def test_workspace_file_listing_is_cached_and_invalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    calls = {"n": 0}
    real_walk = os.walk

    def counting_walk(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real_walk(*args, **kwargs)

    monkeypatch.setattr("rbit.tools.os.walk", counting_walk)
    first = executor.list_workspace_files()
    assert "a.py" in first
    assert calls["n"] == 1

    executor.list_workspace_files()
    assert calls["n"] == 1

    executor._tool_write({"filePath": "b.py", "content": "y = 2\n"})
    refreshed = executor.list_workspace_files()
    assert calls["n"] == 2
    assert "b.py" in refreshed


@pytest.mark.asyncio
async def test_validate_api_key_via_models_endpoint(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: {})
    assert HTTP_LIMITS.max_keepalive_connections >= 1

    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://mock.local/v1/models"
        assert request.headers["authorization"] == "Bearer good-key"
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as client:
        engine.http_client = client
        ok, message, models = await engine.fetch_provider_models(
            "https://mock.local/v1/chat/completions", "good-key"
        )
    assert ok is True
    assert "2 models" in message
    assert [m.id for m in models] == ["a", "b"]
    assert all(m.provider == "mock.local" for m in models)

    def deny_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(deny_handler)) as client:
        engine.http_client = client
        ok, message = await engine.validate_api_key("https://mock.local/v1", "bad-key")
    assert ok is False
    assert "Invalid API key" in message


@pytest.mark.asyncio
async def test_connect_adds_all_provider_models_under_category(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://openrouter.ai/api/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "openai/gpt-4.1-mini"},
                    {"id": "anthropic/claude-haiku-4"},
                    {"id": "deepseek/deepseek-r1"},
                ]
            },
        )

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        app.engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.config_data["api_base"] = "https://openrouter.ai/api/v1"
        app.config_data["api_key"] = "test-key"
        added = app.engine.add_provider_models(
            (await app.engine.fetch_provider_models(app.config_data["api_base"], "test-key"))[2]
        )
        assert added == 3
        provider_models = [m for m in app.engine.extra_models if m.provider == "OpenRouter"]
        assert {m.id for m in provider_models} >= {
            "openai/gpt-4.1-mini",
            "anthropic/claude-haiku-4",
            "deepseek/deepseek-r1",
        }

        await app.push_screen(ModelSelectorModal("rbit/smart-local", app.engine.extra_models))
        await pilot.pause()
        olist = app.screen.query_one("#model-list", OptionList)
        ids = [olist.get_option_at_index(i).id for i in range(olist.option_count)]
        assert "openai/gpt-4.1-mini" in ids
        openrouter_idx = ids.index("openai/gpt-4.1-mini")
        assert olist.get_option_at_index(openrouter_idx - 1).disabled
        assert "openrouter" in str(olist.get_option_at_index(openrouter_idx - 1).prompt).lower()
        await pilot.press("escape")
        await app.engine.http_client.aclose()


@pytest.mark.asyncio
async def test_auto_discovery_on_startup_and_modal_refresh(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": "groq/llama-4-scout"}, {"id": "groq/qwen-3-32b"}]},
            )
        return httpx.Response(404)

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        app.engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cfg = {"api_base": "https://api.groq.com/openai/v1", "api_key": "k"}
        provider_count, _ = await app.engine.auto_discover_models(cfg, force=True)
        assert provider_count == 2
        assert {m.provider for m in app.engine.extra_models} >= {"Groq"}
        assert all(
            m["provider"] != "Ollama Local" for m in app.engine.persistable_provider_models()
        )

        await app.push_screen(ModelSelectorModal("rbit/smart-local", []))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ModelSelectorModal)
        assert not any(
            (modal.query_one("#model-list", OptionList).get_option_at_index(i).id or "")
            .startswith("groq/")
            for i in range(modal.query_one("#model-list", OptionList).option_count)
        )
        modal.refresh_models(app.engine.extra_models)
        await pilot.pause()
        olist = modal.query_one("#model-list", OptionList)
        ids = [olist.get_option_at_index(i).id for i in range(olist.option_count)]
        assert "groq/llama-4-scout" in ids
        assert "groq/qwen-3-32b" in ids
        await pilot.press("escape")
        await app.engine.http_client.aclose()


@pytest.mark.asyncio
async def test_palette_shows_recents_first(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await app.push_screen(CommandPaletteModal(recents=["diff", "todo"]))
        await pilot.pause()
        olist = app.screen.query_one("#palette-list", OptionList)
        ids = [olist.get_option_at_index(i).id for i in range(olist.option_count)]
        assert ids[1] == "diff"
        assert ids[2] == "todo"
        foot = app.screen.query_one("#palette-foot", Label)
        assert "last used" in str(foot.render()).lower()

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, CommandPaletteModal)

        app._record_palette_use("diff")
        app._record_palette_use("todo")
        assert app.config_data["palette_recents"][:2] == ["todo", "diff"]
        saved = json.loads((tmp_path / ".rbit" / "config.json").read_text(encoding="utf-8"))
        assert saved["palette_recents"][:2] == ["todo", "diff"]


def test_community_health_files_present() -> None:
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CHANGELOG.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
    ):
        assert (root / rel).is_file(), rel
    assert "pytest" in (root / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "vulnerability" in (root / "SECURITY.md").read_text(encoding="utf-8").lower()


def test_reasoning_effort_resolution(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())

    cfg: dict[str, Any] = {"effort": "auto"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)
    assert is_reasoning_model("openai/o3-mini") is True
    assert is_reasoning_model("deepseek/deepseek-r1") is True
    assert is_reasoning_model("openai/gpt-4.1") is False
    assert engine._reasoning_effort("openai/o3-mini") == "medium"
    assert engine._reasoning_effort("openai/gpt-4.1") is None

    cfg["effort"] = "high"
    assert engine._reasoning_effort("deepseek/deepseek-r1") == "high"
    assert engine._reasoning_effort("openai/gpt-4.1") is None

    cfg["effort"] = "bogus"
    assert engine._reasoning_effort("openai/o3-mini") is None


def test_effort_is_sent_in_request_payload(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok", "tool_calls": []}}]},
        )

    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"effort": "low"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await engine._non_stream_chat_step(
                client=client,
                url="https://mock.local/v1/chat/completions",
                headers={},
                model_id="openai/o3-mini",
                messages=[{"role": "user", "content": "hi"}],
            )

    asyncio.run(run())
    assert seen and seen[0]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_effort_selector_and_command(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        app._set_effort("high")
        assert app.config_data["effort"] == "high"
        saved = json.loads((tmp_path / ".rbit" / "config.json").read_text(encoding="utf-8"))
        assert saved["effort"] == "high"

        app.config_data["effort"] = "auto"
        await app.push_screen(EffortSelectorModal("auto", "openai/o3-mini"))
        await pilot.pause()
        olist = app.screen.query_one("#effort-list", OptionList)
        ids = [olist.get_option_at_index(i).id for i in range(olist.option_count)]
        assert ids == ["auto", "low", "medium", "high"]
        await pilot.press("escape")
        await pilot.pause()

        inp = app.query_one("#prompt-input")
        inp.focus()
        inp.value = "/effort low"
        await pilot.press("enter")
        await pilot.pause()
        assert app.config_data["effort"] == "low"

        inp.value = "/effort nonsense"
        await pilot.press("enter")
        await pilot.pause()
        assert app.config_data["effort"] == "low"


@pytest.mark.asyncio
async def test_apply_patch_multihunk_create_and_validation(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session())

    patch = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 a = 1
-b = 2
+b = 20
@@ -3,2 +3,2 @@
 c = 3
-d = 4
+d = 40
"""
    res = await executor.execute("apply_patch", {"patch": patch})
    assert res.ok is True
    assert "2 hunks" in res.summary
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\nd = 40\n"
    assert "calc.py" in executor.session.modified_files

    created = """--- /dev/null
+++ b/new_mod.py
@@ -0,0 +1,2 @@
+def hi():
+    return 1
"""
    made = await executor.execute("apply_patch", {"patch": created})
    assert made.ok is True
    assert (tmp_path / "new_mod.py").read_text(encoding="utf-8").startswith("def hi():")

    broken = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 a = 1
-missing line
+nope
"""
    bad = await executor.execute("apply_patch", {"patch": broken})
    assert bad.ok is False
    assert "context mismatch" in bad.summary

    # Undo restores the pre-patch content (snapshot stack, newest first)
    executor.undo_last()
    assert not (tmp_path / "new_mod.py").exists()
    executor.undo_last()
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\nd = 4\n"

    blocked = await executor.execute("apply_patch", {"patch": patch}, agent_mode="plan")
    assert blocked.ok is False
    assert "Plan mode" in blocked.output


def _flatten_render(widget: Any) -> str:
    from rich.console import Group
    from rich.text import Text

    def parts(node: Any) -> str:
        if isinstance(node, Group):
            return "\n".join(parts(child) for child in node.renderables)
        if isinstance(node, Text):
            return node.plain
        return getattr(node, "plain", str(node))

    return parts(widget.render())


@pytest.mark.asyncio
async def test_background_pid_stop_controls(tmp_path: Path) -> None:
    from rbit.widgets import ToolCallCard

    executor = ToolExecutor(workspace=tmp_path, session=Session())
    bg = await executor.execute(
        "bash",
        {"command": 'python -c "import time; time.sleep(30)"', "background": True},
    )
    assert bg.ok is True
    assert bg.pid > 0
    assert bg.pid in executor.background_pids()

    card = ToolCallCard(
        ToolCallRecord(id="bg1", name="bash", arguments={"command": "sleep"}, pid=bg.pid, status="success")
    )
    assert card.stop_state == "running"
    header = _flatten_render(card)
    assert "stop (pid" in header
    card.mark_stopped()
    assert card.stop_state == "stopped"
    assert "stopped (pid" in _flatten_render(card)

    assert await executor.terminate_background_pid(bg.pid) is True
    assert executor.background_pids() == []
    assert await executor.terminate_background_pid(bg.pid) is False


@pytest.mark.asyncio
async def test_session_search_messages_and_tool_output(tmp_path: Path) -> None:
    session = Session()
    session.messages.append(
        ChatMessage(id="m1", role="user", content="Please fix the LoginWidget bug")
    )
    session.messages.append(
        ChatMessage(
            id="m2",
            role="assistant",
            content="Done.",
            tool_calls=[
                ToolCallRecord(
                    id="t1",
                    name="grep",
                    arguments={"pattern": "login"},
                    summary="2 matches",
                    output="src/auth.py:42: loginwidget",
                ),
                ToolCallRecord(
                    id="t2",
                    name="read",
                    arguments={"filePath": "unrelated.py"},
                    summary="1 lines",
                    output="nothing interesting",
                ),
            ],
        )
    )
    hits = search_session(session, "login")
    assert len(hits) == 2
    assert hits[0][0] == "m1"
    assert hits[1][1] == "Tool grep"
    assert search_session(session, "") == []
    assert search_session(session, "zzz-none") == []

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        app.session = session
        app.executor.session = session
        await app.push_screen(SearchResultsModal(session, "login"))
        await pilot.pause()
        olist = app.screen.query_one("#search-session-list", OptionList)
        assert olist.option_count == 2
        foot = str(app.screen.query_one("#search-session-foot", Label).render())
        assert "2 matches" in foot
        await pilot.press("escape")
        await pilot.pause()

        inp = app.query_one("#prompt-input")
        inp.focus()
        inp.value = "/search login"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SearchResultsModal)
        await pilot.press("escape")
        await pilot.pause()


def test_cli_update_check_and_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from rbit import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_fetch_latest_pypi_version", lambda *a, **k: "99.0.0")
    assert cli_main(["update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "99.0.0" in out

    assert cli_main(["update", "--check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["up_to_date"] is False
    assert payload["latest"] == "99.0.0"

    monkeypatch.setattr(cli_mod, "_fetch_latest_pypi_version", lambda *a, **k: "1.0.0")
    assert cli_main(["update", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "up-to-date"

    monkeypatch.setattr(cli_mod, "_fetch_latest_pypi_version", lambda *a, **k: None)
    assert cli_main(["update"]) == 1
    assert "PyPI" in capsys.readouterr().out

    installed: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd: list[str], **kwargs: Any) -> FakeProc:
        installed.append(cmd)
        return FakeProc()

    monkeypatch.setattr(cli_mod, "_fetch_latest_pypi_version", lambda *a, **k: "99.1.0")
    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    assert cli_main(["update"]) == 0
    assert installed and "rbit" in installed[0] and "--upgrade" in installed[0]
    assert "99.1.0" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_agent_todowrite_syncs_sidebar_without_modal(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        res = await app.executor.execute(
            "todowrite",
            {"todos": [{"content": "ship batch 9", "status": "in_progress", "priority": "high"}]},
        )
        assert res.ok is True
        await pilot.pause()
        assert app.session.todos and app.session.todos[0].content == "ship batch 9"
        assert app.query_one(SidebarPanel).display is True
        todo_text = str(app.query_one("#sb-todo", Static).render())
        assert "ship batch 9" in todo_text


@pytest.mark.asyncio
async def test_paths_outside_workspace_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "rbit-outside-target.txt"
    executor = ToolExecutor(workspace=tmp_path / "ws", session=Session())
    (tmp_path / "ws").mkdir()

    with pytest.raises(PathOutsideWorkspace):
        executor.resolve_path("../rbit-outside-target.txt")

    blocked = await executor.execute("write", {"filePath": "../rbit-outside-target.txt", "content": "x"})
    assert blocked.ok is False
    assert "outside workspace" in blocked.summary
    assert not outside.exists()

    absolute = await executor.execute("read", {"filePath": str(outside)})
    assert absolute.ok is False
    assert "outside workspace" in absolute.summary

    inside = await executor.execute("write", {"filePath": "ok.txt", "content": "hi\n"})
    assert inside.ok is True

    executor.allow_outside_paths = True
    allowed = await executor.execute("write", {"filePath": "../rbit-outside-target.txt", "content": "yes\n"})
    assert allowed.ok is True, allowed
    assert outside.exists(), (str(outside), allowed.summary)
    assert outside.read_text(encoding="utf-8") == "yes\n"
    outside.unlink()

    cfg_executor = ToolExecutor(
        workspace=tmp_path / "ws",
        session=Session(),
        get_config=lambda: {"allow_outside_paths": True},
    )
    assert cfg_executor.resolve_path("../rbit-outside-target.txt").name == "rbit-outside-target.txt"


@pytest.mark.asyncio
async def test_escape_saves_partial_answer_as_interrupted(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        app.is_busy = True
        partial = ChatMessage(id="partial-1", role="assistant", content="Half an answer", agent="build")
        app._live_card = AssistantMessageCard(partial, [])
        app.query_one("#chat-scroll", ChatScroll).mount(app._live_card)
        await pilot.pause()

        app.action_interrupt_or_close()
        await pilot.pause()

        saved = next((m for m in app.session.messages if m.id == "partial-1"), None)
        assert saved is not None
        assert saved.interrupted is True
        assert saved.content == "Half an answer"
        assert app.is_busy is False
        assert app._live_card is None


@pytest.mark.asyncio
async def test_enter_expands_card_and_ctrl_e_collapses(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        rec = ToolCallRecord(
            id="c1", name="read", arguments={"filePath": "a.py"}, status="success", summary="1 lines", output="x"
        )
        card = ToolCardWidget(rec)
        app.query_one("#chat-scroll", ChatScroll).mount(card)
        await pilot.pause()
        assert card.expanded is False

        inp = app.query_one("#prompt-input", Input)
        inp.focus()
        inp.value = ""
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert card.expanded is True

        app.action_collapse_all_cards()
        await pilot.pause()
        assert card.expanded is False


@pytest.mark.asyncio
async def test_sticky_scroll_stops_following_when_scrolled_up(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        app.session.messages = [ChatMessage(id=f"m{i}", role="user", content=f"line {i}") for i in range(40)]
        app.executor.session = app.session
        app._rebuild_chat_view()
        app._sync_ui()
        await pilot.pause()
        await pilot.pause()

        scroll = app.query_one("#chat-scroll", ChatScroll)
        assert scroll.max_scroll_y > 0
        assert scroll.stuck_to_bottom is True
        scroll.scroll_home(animate=False)
        await pilot.pause()
        assert scroll.stuck_to_bottom is False

        before = scroll.scroll_y
        app._smart_scroll_end()
        await pilot.pause()
        assert scroll.scroll_y == before


@pytest.mark.asyncio
async def test_session_save_is_debounced(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        app.session.title = "debounced session"
        app._schedule_save_session(delay=0.05)
        assert app._save_timer is not None
        await asyncio.sleep(0.25)
        await pilot.pause()
        assert app._save_timer is None
        stored = [s for s in app.storage.list_sessions() if s.id == app.session.id]
        assert stored and stored[0].title == "debounced session"


def test_global_allow_outside_flag_before_subcommand(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rbit import cli as cli_mod

    captured: dict[str, Any] = {}

    async def fake_headless(args: Any) -> int:
        captured["allow_outside"] = getattr(args, "allow_outside", False)
        captured["prompt"] = args.prompt
        return 0

    monkeypatch.setattr(cli_mod, "_run_headless", fake_headless)
    assert cli_main(["--allow-outside", "run", "hello"]) == 0
    assert captured["allow_outside"] is True
    assert captured["prompt"] == ["hello"]

    assert cli_main(["run", "hi"]) == 0
    assert captured["allow_outside"] is False


@pytest.mark.asyncio
async def test_interrupted_card_meta_and_finalize_result(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        assert app._finalize_interrupted() is False

        msg = ChatMessage(id="p2", role="assistant", content="partial answer", agent="build")
        card = AssistantMessageCard(msg, [])
        app.query_one("#chat-scroll", ChatScroll).mount(card)
        app._live_card = card
        await pilot.pause()

        assert app._finalize_interrupted() is True
        assert msg.interrupted is True
        assert app._live_card is None
        meta = str(card.query_one(".assistant-meta", Static).render())
        assert "interrupted" in meta


@pytest.mark.asyncio
async def test_max_steps_budget_stops_the_loop(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"max_steps": 2, "api_key": "test"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)
    volley = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        volley["count"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def invoke_tool(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "ok", "ok")

    async def on_status(_s: str) -> None:
        pass

    text = await engine._run_openai_compatible(
        session=Session(),
        user_msg=ChatMessage(id="u1", role="user", content="hi"),
        agent_mode="build",
        model_id="openai/gpt-4o-mini",
        api_base="https://mock.local/v1",
        api_key="test",
        invoke_tool=invoke_tool,
        on_status=on_status,
        on_stream_chunk=None,
    )
    await engine.http_client.aclose()
    engine.http_client = None
    assert "лимит шагов вызова инструментов (2)" in text
    assert volley["count"] == 2


@pytest.mark.asyncio
async def test_thinking_hook_toggles_around_each_model_request(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "done"}}]}
        )

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events: list[bool] = []

    async def invoke_tool(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "ok", "ok")

    async def on_status(_s: str) -> None:
        pass

    async def on_thinking(active: bool) -> None:
        events.append(active)

    text = await engine._run_openai_compatible(
        session=Session(),
        user_msg=ChatMessage(id="u1", role="user", content="hi"),
        agent_mode="build",
        model_id="openai/gpt-4o-mini",
        api_base="https://mock.local/v1",
        api_key="test",
        invoke_tool=invoke_tool,
        on_status=on_status,
        on_stream_chunk=None,
        on_thinking=on_thinking,
    )
    await engine.http_client.aclose()
    engine.http_client = None
    assert text == "done"
    assert events == [True, False]


@pytest.mark.asyncio
async def test_max_cost_budget_stops_the_loop(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"max_cost_usd": 0.000001, "api_key": "test", "max_steps": 9}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def invoke_tool(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "ok", "ok")

    async def on_status(_s: str) -> None:
        pass

    text = await engine._run_openai_compatible(
        session=Session(),
        user_msg=ChatMessage(id="u1", role="user", content="hi"),
        agent_mode="build",
        model_id="openai/gpt-4o-mini",
        api_base="https://mock.local/v1",
        api_key="test",
        invoke_tool=invoke_tool,
        on_status=on_status,
        on_stream_chunk=None,
    )
    await engine.http_client.aclose()
    engine.http_client = None
    assert "лимит стоимости" in text


def test_session_finished_flag_roundtrip(tmp_path: Path) -> None:
    from rbit.storage import StorageManager

    storage = StorageManager(tmp_path)
    sess = Session()
    sess.finished = False
    storage.save_session(sess)
    loaded = storage.list_sessions()[0]
    assert loaded.finished is False
    sess.finished = True
    storage.save_session(sess)
    assert storage.list_sessions()[0].finished is True


@pytest.mark.asyncio
async def test_crash_recovery_loads_unfinished_session(tmp_path: Path) -> None:
    from rbit.storage import StorageManager

    storage = StorageManager(tmp_path)
    crash = Session()
    crash.title = "interrupted by crash"
    crash.messages.append(ChatMessage(id="u1", role="user", content="hello"))
    crash.finished = False
    storage.save_session(crash)

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        assert app.session.id == crash.id
        assert app.session.title == "interrupted by crash"
        assert app.session.messages[0].content == "hello"


@pytest.mark.asyncio
async def test_lazy_history_renders_windows_of_30(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        app.session.messages = [ChatMessage(id=f"m{i}", role="user", content=f"line {i}") for i in range(40)]
        app._rebuild_chat_view()
        await pilot.pause()

        scroll = app.query_one("#chat-scroll", ChatScroll)
        assert len(scroll.children) == 31
        assert app.query_one("#load-older", Static) is not None

        app._load_older()
        await pilot.pause()
        assert len(scroll.children) == 40
        assert not scroll.query("#load-older")


def test_card_copy_target() -> None:
    card = ToolCardWidget(ToolCallRecord(id="b", name="bash", arguments={"command": "pytest -q"}))
    assert card.copy_target() == "pytest -q"
    card = ToolCardWidget(ToolCallRecord(id="r", name="read", arguments={"filePath": "src/a.py"}))
    assert card.copy_target() == "src/a.py"
    card = ToolCardWidget(ToolCallRecord(id="g", name="grep", arguments={"pattern": "TODO"}))
    assert card.copy_target() == "TODO"
    card = ToolCardWidget(ToolCallRecord(id="w", name="webfetch", arguments={"url": "https://x"}))
    assert card.copy_target() == "https://x"
    card = ToolCardWidget(ToolCallRecord(id="u", name="todowrite", arguments={}))
    assert card.copy_target() == ""


@pytest.mark.asyncio
async def test_retry_binding_and_last_error_hint(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        assert callable(app.action_retry_last)
        assert app._last_error == ""
        app._last_error = "boom"
        assert app._last_error == "boom"


def test_diff_and_tool_card_styling() -> None:
    from rich.text import Text

    from rbit.widgets import _render_unified_diff

    diff = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,3 @@\n"
        " a = 1\n"
        "-b = 2\n"
        "+b = 3\n"
        "+c = 4\n"
    )
    rendered = _render_unified_diff(diff)
    assert isinstance(rendered, Text)
    styles = " ".join(str(span.style) for span in rendered.spans)
    assert "#7fd88f" in styles
    assert "#e06c75" in styles
    assert "+ b = 3" in rendered.plain
    assert "- b = 2" in rendered.plain

    write_card = ToolCardWidget(
        ToolCallRecord(
            id="w1",
            name="write",
            arguments={"filePath": "calc.py"},
            summary="(+2 -1)",
            status="success",
        )
    )
    write_plain = _flatten_render(write_card)
    assert "Write calc.py" in write_plain
    assert "+2" in write_plain and "-1" in write_plain

    bash_card = ToolCardWidget(
        ToolCallRecord(
            id="b1",
            name="bash",
            arguments={"command": "pytest -q"},
            summary="1 passed",
            output="ok\n",
            status="success",
        )
    )
    bash_card.expanded = True
    bash_plain = _flatten_render(bash_card)
    assert "$ pytest -q" in bash_plain
    assert "\u2502 ok" in bash_plain

    git_card = ToolCardWidget(
        ToolCallRecord(id="g1", name="git", arguments={}, summary="2 files", status="success")
    )
    assert "Git" in _flatten_render(git_card)

@pytest.mark.asyncio
async def test_previous_tool_results_are_replayed_in_history(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "done"}}]},
        )

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    session = Session()
    session.messages = [
        ChatMessage(id="u1", role="user", content="read a.py"),
        ChatMessage(
            id="a1",
            role="assistant",
            content="",
            model="openai/gpt-4o-mini",
            tool_calls=[
                ToolCallRecord(
                    id="t1",
                    name="read",
                    arguments={"filePath": "a.py"},
                    status="success",
                    output="HELLO_FROM_FILE",
                )
            ],
        ),
        ChatMessage(id="u2", role="user", content="what was in the file?"),
    ]

    async def invoke_tool(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "ok", "ok")

    async def on_status(_s: str) -> None:
        pass

    await engine._run_openai_compatible(
        session=session,
        user_msg=session.messages[-1],
        agent_mode="build",
        model_id="openai/gpt-4o-mini",
        api_base="https://mock.local/v1",
        api_key="test",
        invoke_tool=invoke_tool,
        on_status=on_status,
    )

    assert captured
    messages = captured[0]["messages"]
    tool_msg = next(m for m in messages if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_t1"
    assert tool_msg["name"] == "read"
    assert "HELLO_FROM_FILE" in tool_msg["content"]
    assistant_with_calls = next(m for m in messages if m.get("tool_calls"))
    call = assistant_with_calls["tool_calls"][0]
    assert call["id"] == "call_t1"
    assert call["function"]["name"] == "read"
    assert "a.py" in call["function"]["arguments"]
    assert [m["role"] for m in messages][-1] == "user"

@pytest.mark.asyncio
async def test_thinking_indicator_appears_and_clears(tmp_path: Path) -> None:
    from rbit.widgets import ThinkingIndicator

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app._show_thinking()
        await pilot.pause()
        assert app._thinking is not None
        scroll = app.query_one("#chat-scroll")
        assert scroll.children[-1] is app._thinking
        app._thinking.tick(3)
        await pilot.pause()
        assert "Thinking..." in app._thinking.render().plain
        app._hide_thinking()
        await pilot.pause()
        assert app._thinking is None
        assert len(list(app.query(ThinkingIndicator))) == 0


def test_thinking_indicator_click_reveals_reasoning() -> None:
    from rbit.widgets import ThinkingIndicator

    ind = ThinkingIndicator()
    assert "Thinking..." in ind.render().plain
    ind.set_reasoning("secret chain of thought")
    assert ind.has_reasoning is True
    ind.toggle()
    assert "secret chain of thought" in ind.render().plain
    ind.finish()
    collapsed = ind.render().plain
    assert "Thought for" in collapsed
    assert "secret chain of thought" not in collapsed
    ind.toggle()
    assert "secret chain of thought" in ind.render().plain
    ind.set_reasoning("x" * 2000)
    assert len(ind.render().plain) < 1800


@pytest.mark.asyncio
async def test_stream_updates_before_card_is_mounted_are_safe(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        draft = ChatMessage(id="a1", role="assistant", content="")
        card = AssistantMessageCard(draft, live=True)
        await card.update_stream("partial")
        assert draft.content == "partial"
        await card.finalize(ChatMessage(id="a2", role="assistant", content="final"))
        assert card.msg.content == "final"


@pytest.mark.asyncio
async def test_reasoning_only_reply_is_used_as_content(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "", "reasoning": "internal chain of thought"}}
                ]
            },
        )

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def invoke_tool(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "ok", "ok")

    async def on_status(_s: str) -> None:
        pass

    text = await engine._run_openai_compatible(
        session=Session(),
        user_msg=ChatMessage(id="u1", role="user", content="hi"),
        agent_mode="build",
        model_id="openai/gpt-4o-mini",
        api_base="https://mock.local/v1",
        api_key="test",
        invoke_tool=invoke_tool,
        on_status=on_status,
        on_stream_chunk=None,
    )
    await     engine.http_client.aclose()
    engine.http_client = None
    assert text == "internal chain of thought"


@pytest.mark.asyncio
async def test_reasoning_stream_reaches_hook(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)

    def handler(_request: httpx.Request) -> httpx.Response:
        body = "\n".join(
            [
                'data: {"choices":[{"delta":{"reasoning":"Alpha"}}]}',
                'data: {"choices":[{"delta":{"reasoning":"Beta"}}]}',
                'data: {"choices":[{"delta":{"content":"Answer"}}]}',
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pieces: list[str] = []

    async def invoke_tool(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "ok", "ok")

    async def on_status(_s: str) -> None:
        pass

    async def on_reasoning_chunk(piece: str) -> None:
        pieces.append(piece)

    async def on_stream_chunk(_partial_md: str) -> None:
        pass

    text = await engine._run_openai_compatible(
        session=Session(),
        user_msg=ChatMessage(id="u1", role="user", content="hi"),
        agent_mode="build",
        model_id="openai/gpt-4o-mini",
        api_base="https://mock.local/v1",
        api_key="test",
        invoke_tool=invoke_tool,
        on_status=on_status,
        on_stream_chunk=on_stream_chunk,
        on_reasoning_chunk=on_reasoning_chunk,
    )
    await engine.http_client.aclose()
    engine.http_client = None
    assert text == "Answer"
    assert pieces == ["Alpha", "Beta"]


@pytest.mark.asyncio
async def test_failing_stream_hook_does_not_abort_the_turn(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)

    def handler(_request: httpx.Request) -> httpx.Response:
        body = "\n".join(
            [
                'data: {"choices":[{"delta":{"content":"Hello"}}]}',
                'data: {"choices":[{"delta":{"content":" world"}}]}',
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def invoke_tool(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "ok", "ok")

    async def on_status(_s: str) -> None:
        pass

    async def on_stream_chunk(_partial: str) -> None:
        raise RuntimeError("No nodes match 'Markdown'")

    text = await engine._run_openai_compatible(
        session=Session(),
        user_msg=ChatMessage(id="u1", role="user", content="hi"),
        agent_mode="build",
        model_id="openai/gpt-4o-mini",
        api_base="https://mock.local/v1",
        api_key="test",
        invoke_tool=invoke_tool,
        on_status=on_status,
        on_stream_chunk=on_stream_chunk,
    )
    await engine.http_client.aclose()
    engine.http_client = None
    assert text == "Hello world"


@pytest.mark.asyncio
async def test_full_agent_turn_thinking_reasoning_stream_and_click(tmp_path: Path) -> None:
    """Full _execute_agent_turn flow: two thinking rounds with reasoning, streaming,
    settled 'Thought for Ns' lines kept in the feed, click expands them."""
    from rbit.widgets import ThinkingIndicator

    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "rbit/smart-local"

    async def fake_run_turn(**kwargs):
        on_thinking = kwargs["on_thinking"]
        on_reasoning_chunk = kwargs["on_reasoning_chunk"]
        on_stream_chunk = kwargs["on_stream_chunk"]

        await on_thinking(True)
        await on_reasoning_chunk("сначала думаю о структуре. ")
        await on_reasoning_chunk("теперь о формулировке.")
        await on_thinking(False)
        await on_stream_chunk("# Ответ\n\nПервая часть.")
        await on_thinking(True)
        await on_reasoning_chunk("уточняю вторую часть.")
        await on_thinking(False)
        await on_stream_chunk("# Ответ\n\nПервая часть. Вторая часть.")
        return ChatMessage(
            id="a1", role="assistant", content="# Ответ\n\nПервая часть. Вторая часть.",
            agent=kwargs["agent_mode"], model=kwargs["model_id"], duration_s=1.0,
        )

    app.engine.run_turn = fake_run_turn

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        user = ChatMessage(id="u1", role="user", content="тест")
        app.session.messages.append(user)
        app._execute_agent_turn(user)
        for _ in range(200):
            await pilot.pause()
            if not app.is_busy:
                break
        await pilot.pause()
        await pilot.pause()

        assert app.is_busy is False
        lines = list(app.query(ThinkingIndicator))
        cards = list(app.query(AssistantMessageCard))
        assert len(cards) == 1, f"expected final assistant card, got {len(cards)}"
        assert len(lines) == 2, f"expected both settled thinking lines kept, got {len(lines)}"
        assert "▸" in lines[0].render().plain
        assert "▸" in lines[1].render().plain
        lines[0].toggle()
        await pilot.pause()
        assert "▾" in lines[0].render().plain
        assert "сначала думаю о структуре." in lines[0].render().plain
        assert "Первая часть" in cards[0].msg.content


def test_atomic_write_preserves_dominant_line_ending(tmp_path: Path) -> None:
    from rbit.tools import atomic_write_text

    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(b"a\r\nb\r\nc\r\n")
    atomic_write_text(crlf, "a\nB\nc\n")
    assert crlf.read_bytes() == b"a\r\nB\r\nc\r\n"

    lf = tmp_path / "lf.txt"
    lf.write_bytes(b"a\nb\n")
    atomic_write_text(lf, "a\nB\n")
    assert lf.read_bytes() == b"a\nB\n"

    fresh = tmp_path / "new.txt"
    atomic_write_text(fresh, "x\n")
    assert fresh.read_bytes() == b"x\n"


@pytest.mark.asyncio
async def test_fuzzy_edit_keeps_trailing_newline(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def  foo():\n    return 1\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    res = await executor.execute(
        "edit", {"filePath": "m.py", "oldString": "def foo():", "newString": "def bar():"}
    )
    assert res.ok is True, res
    text = (tmp_path / "m.py").read_text(encoding="utf-8")
    assert text == "def bar():\n    return 1\n"


@pytest.mark.asyncio
async def test_checkpoint_restore_keeps_binary_files(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("v1\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    meta = executor.create_checkpoint("before")

    (tmp_path / "keep.txt").write_text("v2\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nbinary")
    (tmp_path / "later.txt").write_text("new file\n", encoding="utf-8")

    msg = executor.restore_checkpoint(meta.id)
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "v1\n"
    assert (tmp_path / "logo.png").read_bytes().startswith(b"\x89PNG")
    assert not (tmp_path / "later.txt").exists()
    assert "restored" in msg


@pytest.mark.asyncio
async def test_grep_utf8_output_and_invalid_regex(tmp_path: Path) -> None:
    (tmp_path / "u.txt").write_text("\u0101\u0101\u0101 needle\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session())

    res = await executor.execute("grep", {"pattern": "needle"})
    assert res.ok is True
    assert "needle" in res.output

    bad = await executor.execute("grep", {"pattern": "[", "filesOnly": True})
    assert bad.ok is False


@pytest.mark.asyncio
async def test_stream_retry_resets_tool_call_accumulators(tmp_path: Path) -> None:
    """A stream that breaks mid-tool-call must not glue its partial deltas onto
    the retried response (the 'readread' / merged-arguments bug)."""
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test"}

    attempts = {"n": 0}

    async def broken_stream() -> Any:
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"rea"}}]}}]}\n\n'
        raise httpx.ReadError("connection reset mid-stream")

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(200, content=broken_stream())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c2",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"filePath": "a.py"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    cfg["api_base"] = "https://mock.local/v1"
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)
    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _noop_rec(_rec: ToolCallRecord) -> None:
        return None

    async def _noop_status(_s: str) -> None:
        return None

    reply = await engine.run_turn(
        session=Session(),
        user_msg=ChatMessage(id="u1", role="user", content="hi"),
        agent_mode="build",
        model_id="openai/gpt-4.1",
        on_tool_start=_noop_rec,
        on_tool_end=_noop_rec,
        on_status=_noop_status,
    )
    await engine.http_client.aclose()
    engine.http_client = None
    tool = reply.tool_calls[0]
    assert tool.name == "read"
    assert "rea" + "read" not in tool.name
    assert tool.name != "rea"
    assert '"filePath": "a.py"' in tool.arguments or tool.arguments == {"filePath": "a.py"}


@pytest.mark.asyncio
async def test_tool_crash_does_not_kill_turn(tmp_path: Path) -> None:
    """An exception inside a tool must become a failed tool result, not kill the turn."""
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "все хорошо"}}]},
        )

    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)
    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def exploding_tool(name: str, args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("boom inside tool")

    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    tool_calls = [
        {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    ]
    messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
    await engine._dispatch_tool_calls(
        tool_calls=tool_calls,
        api_messages=messages,
        invoke_tool=exploding_tool,
        on_status=None,
    )
    tool_msg = messages[-1]
    assert tool_msg["role"] == "tool"
    assert "internal error" in tool_msg["content"]
    assert "boom inside tool" in tool_msg["content"]


@pytest.mark.asyncio
async def test_non_stream_garbage_body_is_error(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test"}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway junk</html>", headers={"content-type": "text/html"})

    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)
    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok, text, tool_calls = await engine._non_stream_chat_step(
        client=engine.http_client,
        url="https://mock.local/v1/chat/completions",
        headers={},
        model_id="openai/gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
    )
    await engine.http_client.aclose()
    engine.http_client = None
    assert ok is False
    assert "не-JSON" in text
    assert tool_calls == []


def test_readonly_bash_statement_analysis() -> None:
    from rbit.tools import is_readonly_bash_command

    assert is_readonly_bash_command("echo hi") is True
    assert is_readonly_bash_command("cat a.txt | grep needle") is True
    assert is_readonly_bash_command("git status") is True
    assert is_readonly_bash_command("echo x; rm -rf y") is False
    assert is_readonly_bash_command("ls > out.txt") is False
    assert is_readonly_bash_command("pytest && curl evil.example") is False


def test_apply_patch_accepts_stripped_empty_context(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    (tmp_path / "f.txt").write_text("alpha\n\nomega\n", encoding="utf-8")
    patch = (
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "\n"
        "-omega\n"
        "+OMEGA\n"
    )
    res = executor._tool_apply_patch({"filePath": "f.txt", "patch": patch})
    assert res.ok is True, res
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "alpha\n\nOMEGA\n"


@pytest.mark.asyncio
async def test_completion_inserts_path_without_control_chars(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", Input)
        inp.value = "прочитай @zet"
        await pilot.pause()
        app._apply_completion("file", "sub\zeta.txt")
        assert inp.value == "прочитай @sub/zeta.txt "
        assert "\t" not in inp.value and "\n" not in inp.value and "\r" not in inp.value


def test_load_corrupted_checkpoint_returns_none(tmp_path: Path) -> None:
    from rbit.storage import StorageManager

    storage = StorageManager(workspace=tmp_path)
    storage.ensure_dirs()
    (storage.checkpoints_dir / "badcp.json").write_text("{ truncated", encoding="utf-8")
    assert storage.load_checkpoint_files("badcp") is None


@pytest.mark.asyncio
async def test_prompt_paste_keeps_multiline_and_large_text(tmp_path: Path) -> None:
    from textual import events

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", Input)

        inp.post_message(events.Paste("строка один\nстрока два\nстрока три"))
        await pilot.pause()
        assert inp.value == "строка один строка два строка три"

        inp.value = ""
        big = "x" * 20000
        inp.post_message(events.Paste(big))
        await pilot.pause()
        assert inp.value == big

        inp.value = "начало "
        inp.cursor_position = len(inp.value)
        inp.post_message(events.Paste("a\nb\nc\nдлинный хвост"))
        await pilot.pause()
        assert inp.value == "начало a b c длинный хвост"


@pytest.mark.asyncio
async def test_prompt_queue_runs_after_current_turn(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "rbit/smart-local"
    turn = {"n": 0}

    release = asyncio.Event()

    async def fake_run_turn(**kwargs):
        turn["n"] += 1
        if turn["n"] == 1:
            await release.wait()  # hold the first turn open until the queue is set
        return ChatMessage(
            id=f"a{turn['n']}", role="assistant", content=f"ответ {turn['n']}",
            agent="build", model="m", duration_s=0.1,
        )

    app.engine.run_turn = fake_run_turn
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app._submit_prompt("первый")
        for _ in range(100):
            await pilot.pause()
            if app.is_busy:
                break
        assert app.is_busy, "first turn never started"
        app._submit_prompt("второй")
        assert app._queued_prompt == "второй"
        assert "в очереди" in app._busy_status_display
        release.set()
        for _ in range(600):
            await pilot.pause()
            if len(app.session.messages) >= 4 and not app.is_busy:
                break
        await pilot.pause()
        assert app._queued_prompt is None
    roles = [m.role for m in app.session.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert app.session.messages[-1].content == "ответ 2"


@pytest.mark.asyncio
async def test_sidebar_file_opens_diff_modal(tmp_path: Path) -> None:
    (tmp_path / "t.txt").write_text("v1\n", encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        res = await app.executor.execute(
            "edit", {"filePath": "t.txt", "oldString": "v1", "newString": "v2"}
        )
        assert res.ok is True
        app._sync_ui()
        await pilot.pause()
        app.action_open_file_diff("t.txt")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert type(app.screen).__name__ == "DiffViewerModal"
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_tool_args_streaming_status_hint(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test"}

    def handler(_request: httpx.Request) -> httpx.Response:
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "write"}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "x" * 600}}
            ]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)
    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    statuses: list[str] = []

    async def on_status(s: str) -> None:
        statuses.append(s)

    async def on_stream_chunk(_p: str) -> None:
        return None

    ok, text, tool_calls = await engine._stream_chat_step(
        client=engine.http_client,
        url="https://mock.local/v1/chat/completions",
        headers={},
        model_id="openai/gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
        on_stream_chunk=on_stream_chunk,
        on_status=on_status,
    )
    await engine.http_client.aclose()
    engine.http_client = None
    assert ok is True
    assert any("стрим аргументов: write" in s for s in statuses)
    assert statuses[-1] == ""
    assert tool_calls and len(tool_calls[0]["function"]["arguments"]) == 600


@pytest.mark.asyncio
async def test_prompt_profile_filters_tools_and_prompt(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test", "prompt_profile": "reviewer"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        tool_names = [t["function"]["name"] for t in body["tools"]]
        assert "write" not in tool_names and "bash" not in tool_names
        assert "PROFILE: reviewer" in body["messages"][0]["content"]
        assert "1-3 line plan" in body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок, план готов"}}]})

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _noop_status(_s: str) -> None:
        return None

    async def _noop_tool(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "ok", "ok")

    reply = await engine._run_openai_compatible(
        session=Session(),
        user_msg=ChatMessage(id="u1", role="user", content="review this"),
        agent_mode="build",
        model_id="openai/gpt-4.1",
        api_base="https://mock.local/v1",
        api_key="test",
        invoke_tool=_noop_tool,
        on_status=_noop_status,
        on_stream_chunk=None,
    )
    await engine.http_client.aclose()
    engine.http_client = None
    assert "ок, план готов" in reply
    assert engine._active_profile_tools is None  # reset after the run


@pytest.mark.asyncio
async def test_profile_readonly_blocks_mutating_tool(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test", "prompt_profile": "reviewer"}
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)
    engine._active_profile_readonly = True
    messages: list[dict[str, Any]] = []
    tool_calls = [
        {"id": "c1", "type": "function", "function": {"name": "write", "arguments": "{}"}}
    ]

    async def must_not_run(name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("mutating tool must not run under reviewer profile")

    await engine._dispatch_tool_calls(
        tool_calls=tool_calls, api_messages=messages, invoke_tool=must_not_run, on_status=None
    )
    assert messages[-1]["role"] == "tool"
    assert "read-only" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_prompt_caching_marks_system_for_openrouter(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session())
    cfg: dict[str, Any] = {"api_key": "test", "prompt_caching": "auto"}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        system = body["messages"][0]
        assert isinstance(system["content"], list)
        assert system["content"][0]["cache_control"] == {"type": "ephemeral"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)
    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok, text, _ = await engine._non_stream_chat_step(
        client=engine.http_client,
        url="https://openrouter.ai/api/v1/chat/completions",
        headers={},
        model_id="openai/gpt-4.1",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
    )
    await engine.http_client.aclose()
    engine.http_client = None
    assert ok is True


@pytest.mark.asyncio
async def test_self_check_runs_one_fix_round(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "rbit/smart-local"
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
    calls = {"n": 0}

    async def fake_run_turn(**kwargs):
        calls["n"] += 1
        user_content = kwargs["user_msg"].content
        if calls["n"] == 1:
            assert "[auto]" not in user_content
            return ChatMessage(
                id="a1", role="assistant", content="правлю",
                agent="build", model="m", duration_s=0.1,
                tool_calls=[ToolCallRecord(
                    id="t1", name="write", arguments={"filePath": "broken.py"},
                    status="success", summary="(+1 -1)", output="ok", diff="",
                    duration_ms=1,
                )],
            )
        assert "[auto]" in user_content and "broken.py" in user_content
        return ChatMessage(id="a2", role="assistant", content="исправлено", agent="build", model="m", duration_s=0.1)

    app.engine.run_turn = fake_run_turn
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        user = ChatMessage(id="u1", role="user", content="почини файл")
        app.session.messages.append(user)
        app._execute_agent_turn(user)
        for _ in range(300):
            await pilot.pause()
            if not app.is_busy:
                break
        await pilot.pause()
    assert calls["n"] == 2
    assert any("[auto]" in m.content for m in app.session.messages if m.role == "user")
