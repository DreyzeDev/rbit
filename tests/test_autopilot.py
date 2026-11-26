"""End-to-end tests for Autopilot persistence/resume and concurrent subagent file-conflict locking."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
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
                [{"id": "c3", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": f'"{sys.executable}" -B -m pytest -q'})}}],
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
    step_n = {"n": 0}

    async def resume_step(**kwargs: Any) -> tuple[bool, str, list[dict[str, Any]]]:
        captured_system.append(str(kwargs["messages"][0]["content"]))
        step_n["n"] += 1
        if step_n["n"] == 1:
            return (
                True,
                "",
                [
                    {
                        "id": "call_td",
                        "type": "function",
                        "function": {
                            "name": "todowrite",
                            "arguments": json.dumps(
                                {"todos": [{"id": "t1", "content": "Implement X", "status": "completed", "priority": "high"}]}
                            ),
                        },
                    }
                ],
            )
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


@pytest.mark.asyncio
async def test_autopilot_24_step_multi_module_project_and_non_looping_test_gate(tmp_path: Path) -> None:
    """24-step autonomous run across multiple modules with mid-turn compaction and real pytest."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='multi'\n", encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "calc.py").write_text("def mul(a, b):\n    return a + b\n", encoding="utf-8")
    (pkg / "fmt.py").write_text("def label(name, val):\n    return f'{name}={val}'\n", encoding="utf-8")
    (tmp_path / "test_pkg.py").write_text(
        "from pkg.calc import mul\nfrom pkg.fmt import label\n\ndef test_all():\n    assert label('x', mul(3, 5)) == 'x=15'\n",
        encoding="utf-8",
    )

    storage = StorageManager(workspace=tmp_path)
    storage.ensure_dirs()
    session = Session(id="ap_24", title="24-step", messages=[])
    user = ChatMessage(id="u1", role="user", content="Audit pkg, fix bugs, and verify pytest")
    session.messages.append(user)

    ex = ToolExecutor(workspace=tmp_path, session=session, storage=storage, get_config=lambda: {})
    ex.always_allow_bash = True
    cfg = {"api_key": "k", "auto_compact": True, "auto_compact_at": 65, "max_steps": 30}
    engine = AgentEngine(workspace=tmp_path, executor=ex, get_config=lambda: cfg, storage=storage)

    step = {"n": 0}

    async def fake_step(**_kwargs: Any) -> tuple[bool, str, list[dict[str, Any]]]:
        step["n"] += 1
        if step["n"] <= 20:
            return (
                True,
                f"inspecting step {step['n']}",
                [
                    {
                        "id": f"r_{step['n']}",
                        "type": "function",
                        "function": {"name": "read", "arguments": json.dumps({"filePath": "pkg/calc.py", "offset": step["n"]})},
                    }
                ],
            )
        if step["n"] == 21:
            amp = "& " if sys.platform == "win32" else ""
            return (
                True,
                "reproducing failure",
                [{"id": "b1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": f'{amp}"{sys.executable}" -B -m pytest -q'})}}],
            )
        if step["n"] == 22:
            return (
                True,
                "fixing mul()",
                [
                    {
                        "id": "e1",
                        "type": "function",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps({"filePath": "pkg/calc.py", "oldString": "return a + b", "newString": "return a * b"}),
                        },
                    }
                ],
            )
        if step["n"] == 23:
            amp = "& " if sys.platform == "win32" else ""
            return (
                True,
                "verifying fix",
                [{"id": "b2", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": f'{amp}"{sys.executable}" -B -m pytest -q'})}}],
            )
        return True, "Completed 24-step autonomous multi-module fix.", []

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
    assert step["n"] == 24
    bash_calls = [tc for tc in reply.tool_calls if tc.name == "bash"]
    assert bash_calls[0].status == "error" and bash_calls[1].status == "success"
    assert storage.load_autopilot_state("ap_24")["status"] == "completed"  # type: ignore[index]


@pytest.mark.asyncio
async def test_autopilot_recovers_after_abrupt_process_termination(tmp_path: Path) -> None:
    """Simulates abrupt SIGKILL/power-loss (status=running on disk) and resumes cleanly."""
    storage1 = StorageManager(workspace=tmp_path)
    storage1.ensure_dirs()
    session = Session(
        id="ap_kill",
        title="abrupt kill",
        finished=False,
        messages=[ChatMessage(id="u1", role="user", content="Migrate module A and B")],
    )
    storage1.save_session(session)
    storage1.save_autopilot_state(
        "ap_kill",
        {
            "goal": "Migrate module A and B",
            "status": "running",
            "todos": [
                {"id": "t1", "content": "Migrate A", "status": "completed", "priority": "high"},
                {"id": "t2", "content": "Migrate B", "status": "in_progress", "priority": "high"},
            ],
            "modified_files": ["a.py"],
        },
    )

    # Fresh process after reboot
    storage2 = StorageManager(workspace=tmp_path)
    loaded_sess = storage2.find_session("ap_kill")
    assert loaded_sess is not None and loaded_sess.finished is False
    ex2 = ToolExecutor(workspace=tmp_path, session=loaded_sess, storage=storage2, get_config=lambda: {})
    eng2 = AgentEngine(workspace=tmp_path, executor=ex2, get_config=lambda: {"api_key": "k"}, storage=storage2)

    seen_sys: list[str] = []

    async def resume_step(**kwargs: Any) -> tuple[bool, str, list[dict[str, Any]]]:
        seen_sys.append(str(kwargs["messages"][0]["content"]))
        return True, "Finished remaining Migrate B step.", []

    eng2._non_stream_chat_step = resume_step  # type: ignore[method-assign]

    async def noop(*_a: Any, **_kw: Any) -> None:
        return None

    u2 = ChatMessage(id="u2", role="user", content="resume")
    loaded_sess.messages.append(u2)
    reply = await eng2.run_turn(
        session=loaded_sess,
        user_msg=u2,
        agent_mode="autopilot",
        model_id="openai/gpt-4o",
        on_tool_start=noop,
        on_tool_end=noop,
        on_status=noop,
        on_stream_chunk=None,
    )
    assert "[completed] Migrate A" in seen_sys[0]
    assert "[in_progress] Migrate B" in seen_sys[0]
    assert "Finished remaining" in reply.content
    assert storage2.load_autopilot_state("ap_kill")["status"] == "completed"  # type: ignore[index]


@pytest.mark.asyncio
async def test_parallel_subagents_disjoint_files_succeed_without_corruption(tmp_path: Path) -> None:
    """4 concurrent coder subagents edit disjoint files cleanly while shared file edits are blocked."""
    for name in ("f1.py", "f2.py", "f3.py", "shared.py"):
        (tmp_path / name).write_text("X = 0\n", encoding="utf-8")
    storage = StorageManager(workspace=tmp_path)
    session = Session(id="s_disjoint", title="disjoint", messages=[])
    ex = ToolExecutor(workspace=tmp_path, session=session, storage=storage, get_config=lambda: {})
    engine = AgentEngine(workspace=tmp_path, executor=ex, get_config=lambda: {"api_key": "k"}, storage=storage)
    engine._turn_subagent_files = {}
    engine._subagent_budget_lock = asyncio.Lock()
    engine._turn_subagent_spent = 0.0
    engine._turn_subagent_reserved = 0.0
    engine._turn_subagent_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        msgs = body.get("messages") or []
        if not any(m.get("role") == "tool" for m in msgs):
            goal_txt = str(msgs[-1].get("content") or "")
            fname = goal_txt.split()[-1]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": f"e_{fname}",
                                        "type": "function",
                                        "function": {
                                            "name": "edit",
                                            "arguments": json.dumps(
                                                {"filePath": fname, "oldString": "X = 0", "newString": f"X = '{fname}'"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = await asyncio.gather(
            engine._run_subagent({"goal": "edit f1.py", "agent": "coder"}, model_id="openai/gpt-4o", api_base="https://api.example/v1", api_key="k"),
            engine._run_subagent({"goal": "edit f2.py", "agent": "coder"}, model_id="openai/gpt-4o", api_base="https://api.example/v1", api_key="k"),
            engine._run_subagent({"goal": "edit f3.py", "agent": "coder"}, model_id="openai/gpt-4o", api_base="https://api.example/v1", api_key="k"),
            engine._run_subagent({"goal": "edit shared.py", "agent": "coder"}, model_id="openai/gpt-4o", api_base="https://api.example/v1", api_key="k"),
        )
    finally:
        await engine.http_client.aclose()
    assert all(r.ok for r in res)
    assert (tmp_path / "f1.py").read_text(encoding="utf-8") == "X = 'f1.py'\n"
    assert (tmp_path / "f2.py").read_text(encoding="utf-8") == "X = 'f2.py'\n"
    assert (tmp_path / "f3.py").read_text(encoding="utf-8") == "X = 'f3.py'\n"
    assert (tmp_path / "shared.py").read_text(encoding="utf-8") == "X = 'shared.py'\n"
