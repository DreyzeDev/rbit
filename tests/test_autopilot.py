"""End-to-end tests for Autopilot persistence/resume and concurrent subagent file-conflict locking."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from rbit.agent import AgentEngine
from rbit.models import ChatMessage, Session
from rbit.storage import StorageManager
from rbit.tools import ToolExecutor


@pytest.mark.asyncio
async def test_autopilot_end_to_end_multi_file_test_fix_verify(tmp_path: Path) -> None:
    """Real multi-file project: autopilot analyzes, plans, edits, runs pytest, fixes, and verifies."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "math_ops.py").write_text("def mul(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("from math_ops import mul\n\ndef area(w, h):\n    return mul(w, h)\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "from app import area\n\ndef test_area():\n    assert area(3, 4) == 12\n",
        encoding="utf-8",
    )

    storage = StorageManager(workspace=tmp_path)
    storage.ensure_dirs()
    session = Session(id="ap_e2e", title="autopilot e2e", messages=[])
    user = ChatMessage(id="u1", role="user", content="Fix area calculation and verify tests pass")
    session.messages.append(user)

    ex = ToolExecutor(workspace=tmp_path, session=session, storage=storage, get_config=lambda: {})
    ex.always_allow_bash = True
    engine = AgentEngine(workspace=tmp_path, executor=ex, get_config=lambda: {"api_key": "k"}, storage=storage)

    step = {"n": 0}

    async def fake_step(**kwargs: Any) -> tuple[bool, str, list[dict[str, Any]]]:
        step["n"] += 1
        if step["n"] == 1:
            return (
                True,
                "1. Inspect math_ops.py and create plan",
                [
                    {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"filePath": "math_ops.py"}'}},
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {
                            "name": "todowrite",
                            "arguments": json.dumps(
                                {
                                    "todos": [
                                        {"id": "t1", "content": "Run baseline tests", "status": "in_progress", "priority": "high"},
                                        {"id": "t2", "content": "Fix math_ops.py", "status": "pending", "priority": "high"},
                                    ]
                                }
                            ),
                        },
                    },
                ],
            )
        if step["n"] == 2:
            return (
                True,
                "Run pytest to reproduce failure",
                [{"id": "c3", "type": "function", "function": {"name": "bash", "arguments": '{"command": "pytest -q"}'}}],
            )
        if step["n"] == 3:
            return (
                True,
                "Fix mul() in math_ops.py",
                [
                    {
                        "id": "c4",
                        "type": "function",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {"filePath": "math_ops.py", "oldString": "return a + b", "newString": "return a * b"}
                            ),
                        },
                    }
                ],
            )
        if step["n"] == 4:
            return (
                True,
                "Re-run pytest to verify fix",
                [
                    {"id": "c5", "type": "function", "function": {"name": "bash", "arguments": '{"command": "pytest -q"}'}},
                    {
                        "id": "c6",
                        "type": "function",
                        "function": {
                            "name": "todowrite",
                            "arguments": json.dumps(
                                {
                                    "todos": [
                                        {"id": "t1", "content": "Run baseline tests", "status": "completed", "priority": "high"},
                                        {"id": "t2", "content": "Fix math_ops.py", "status": "completed", "priority": "high"},
                                    ]
                                }
                            ),
                        },
                    },
                ],
            )
        return True, "Fixed mul() in math_ops.py and verified pytest passes.", []

    engine._non_stream_chat_step = fake_step  # type: ignore[method-assign]

    async def noop(*_a: Any, **_kw: Any) -> None:
        return None

    reply = await engine.run_turn(
        session=session,
        user_msg=user,
        agent_mode="autopilot",
        model_id="openai/gpt-4o",
        on_tool_start=noop,
        on_tool_end=noop,
        on_status=noop,
        on_stream_chunk=None,
    )
    assert step["n"] == 5
    # Step 2 pytest failed, step 4 pytest succeeded
    bash_calls = [tc for tc in reply.tool_calls if tc.name == "bash"]
    assert len(bash_calls) == 2
    assert bash_calls[0].status == "error"
    assert bash_calls[1].status == "success"
    state = storage.load_autopilot_state("ap_e2e")
    assert state is not None
    assert state["status"] == "completed"
    assert "math_ops.py" in state["modified_files"]


@pytest.mark.asyncio
async def test_autopilot_persists_paused_state_and_resumes_after_restart(tmp_path: Path) -> None:
    """Simulates a crash mid-autopilot and verifies resume injects saved progress."""
    storage1 = StorageManager(workspace=tmp_path)
    storage1.ensure_dirs()
    session = Session(
        id="ap_resume",
        title="resume",
        messages=[ChatMessage(id="u0", role="user", content="Build feature X")],
    )
    session.todos = [{"id": "t1", "content": "Implement X", "status": "in_progress", "priority": "high"}]  # type: ignore[assignment]
    ex1 = ToolExecutor(workspace=tmp_path, session=session, storage=storage1, get_config=lambda: {})
    eng1 = AgentEngine(workspace=tmp_path, executor=ex1, get_config=lambda: {"api_key": "k"}, storage=storage1)

    async def crash_step(**_kwargs: Any) -> tuple[bool, str, list[dict[str, Any]]]:
        raise RuntimeError("network drop")

    eng1._non_stream_chat_step = crash_step  # type: ignore[method-assign]

    async def noop(*_a: Any, **_kw: Any) -> None:
        return None

    with pytest.raises(RuntimeError, match="network drop"):
        await eng1.run_turn(
            session=session,
            user_msg=session.messages[0],
            agent_mode="autopilot",
            model_id="openai/gpt-4o",
            on_tool_start=noop,
            on_tool_end=noop,
            on_status=noop,
            on_stream_chunk=None,
        )

    paused = storage1.load_autopilot_state("ap_resume")
    assert paused is not None
    assert paused["status"] == "paused"
    assert "network drop" in paused["last_error"]

    # Simulate process restart with fresh StorageManager + AgentEngine
    storage2 = StorageManager(workspace=tmp_path)
    ex2 = ToolExecutor(workspace=tmp_path, session=session, storage=storage2, get_config=lambda: {})
    eng2 = AgentEngine(workspace=tmp_path, executor=ex2, get_config=lambda: {"api_key": "k"}, storage=storage2)

    captured_system: list[str] = []

    async def resume_step(**kwargs: Any) -> tuple[bool, str, list[dict[str, Any]]]:
        captured_system.append(str(kwargs["messages"][0]["content"]))
        return True, "Resumed and completed feature X.", []

    eng2._non_stream_chat_step = resume_step  # type: ignore[method-assign]
    u2 = ChatMessage(id="u2", role="user", content="continue")
    session.messages.append(u2)
    reply = await eng2.run_turn(
        session=session,
        user_msg=u2,
        agent_mode="autopilot",
        model_id="openai/gpt-4o",
        on_tool_start=noop,
        on_tool_end=noop,
        on_status=noop,
        on_stream_chunk=None,
    )
    assert "Autopilot Resume Context" in captured_system[0]
    assert "Resumed and completed" in reply.content
    completed = storage2.load_autopilot_state("ap_resume")
    assert completed is not None and completed["status"] == "completed"


@pytest.mark.asyncio
async def test_concurrent_parallel_subagents_atomic_file_conflict_lock(tmp_path: Path) -> None:
    """Two coder subagents launched via asyncio.gather cannot both edit the same file."""
    (tmp_path / "target.py").write_text("VAL = 0\n", encoding="utf-8")
    storage = StorageManager(workspace=tmp_path)
    session = Session(id="s_par_conf", title="par conf", messages=[])
    ex = ToolExecutor(workspace=tmp_path, session=session, storage=storage, get_config=lambda: {})
    engine = AgentEngine(workspace=tmp_path, executor=ex, get_config=lambda: {"api_key": "k"}, storage=storage)
    engine._turn_subagent_files = {}
    engine._subagent_budget_lock = asyncio.Lock()
    engine._turn_subagent_spent = 0.0
    engine._turn_subagent_reserved = 0.0
    engine._turn_subagent_count = 0

    tool_outputs: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        msgs = body.get("messages") or []
        for m in msgs:
            if m.get("role") == "tool":
                tool_outputs.append(str(m.get("content") or ""))
        if not any(m.get("role") == "tool" for m in msgs):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "e1",
                                        "type": "function",
                                        "function": {
                                            "name": "edit",
                                            "arguments": json.dumps(
                                                {"filePath": "target.py", "oldString": "VAL = 0", "newString": "VAL = 1"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "finished"}}]})

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        r1, r2 = await asyncio.gather(
            engine._run_subagent(
                {"goal": "edit 1", "agent": "coder"}, model_id="openai/gpt-4o", api_base="https://api.example/v1", api_key="k"
            ),
            engine._run_subagent(
                {"goal": "edit 2", "agent": "coder"}, model_id="openai/gpt-4o", api_base="https://api.example/v1", api_key="k"
            ),
        )
    finally:
        await engine.http_client.aclose()
    assert r1.ok and r2.ok
    assert any("Conflict:" in out and "target.py" in out for out in tool_outputs)
