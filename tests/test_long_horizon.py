"""Integration tests for long-horizon autonomous tasks, mid-turn compaction, and parallel subagents."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from rbit.agent import AgentEngine
from rbit.models import ChatMessage, ModelInfo, Session
from rbit.storage import StorageManager
from rbit.tools import ToolExecutor


@pytest.mark.asyncio
async def test_long_horizon_multi_step_with_midturn_compaction(tmp_path: Path) -> None:
    """Simulates a 14-step autonomous loop that triggers mid-turn predictive compaction."""
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    storage = StorageManager(workspace=tmp_path)
    storage.ensure_dirs()
    session = Session(id="s_long", title="long", messages=[])
    user = ChatMessage(id="u1", role="user", content="inspect and update app.py in multiple steps")
    session.messages.append(user)

    executor = ToolExecutor(workspace=tmp_path, session=session, storage=storage, get_config=lambda: {})
    cfg = {
        "api_key": "test-key",
        "auto_compact": True,
        "auto_compact_at": 60,
        "max_steps": 20,
    }
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg, storage=storage)
    engine.extra_models = [ModelInfo(id="small/ctx", name="Small", provider="test", context_window=6000)]

    step_counter = {"n": 0}
    saw_compacted = {"flag": False}

    async def fake_non_stream_step(**kwargs: Any) -> tuple[bool, str, list[dict[str, Any]]]:
        step_counter["n"] += 1
        msgs = kwargs["messages"]
        if any("**[Compacted mid-turn]**" in str(m.get("content") or "") for m in msgs):
            saw_compacted["flag"] = True
        # Ensure no orphaned tool message at index 3 right after compacted summary
        for idx, m in enumerate(msgs):
            if m.get("role") == "tool":
                assert idx > 0 and msgs[idx - 1].get("role") in ("assistant", "tool")

        if step_counter["n"] <= 12:
            return (
                True,
                f"step {step_counter['n']} " + ("context filler " * 45),
                [
                    {
                        "id": f"call_{step_counter['n']}",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"filePath": "app.py", "offset": step_counter["n"]}),
                        },
                    }
                ],
            )
        if step_counter["n"] == 13:
            return (
                True,
                "updating app.py",
                [
                    {
                        "id": "call_edit",
                        "type": "function",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {"filePath": "app.py", "oldString": "VALUE = 1", "newString": "VALUE = 2"}
                            ),
                        },
                    }
                ],
            )
        return True, "Completed 14-step autonomous task.", []

    engine._non_stream_chat_step = fake_non_stream_step  # type: ignore[method-assign]
    statuses: list[str] = []

    async def noop(*_a: Any, **_kw: Any) -> None:
        return None

    async def on_status(txt: str) -> None:
        statuses.append(txt)

    reply = await engine.run_turn(
        session=session,
        user_msg=user,
        agent_mode="build",
        model_id="small/ctx",
        on_tool_start=noop,
        on_tool_end=noop,
        on_status=on_status,
        on_stream_chunk=None,
    )
    assert step_counter["n"] == 14
    assert saw_compacted["flag"] is True
    assert any("Предиктивная компакция шага" in s for s in statuses)
    assert "VALUE = 2" in (tmp_path / "app.py").read_text(encoding="utf-8")
    assert "Completed 14-step autonomous task." in reply.content


@pytest.mark.asyncio
async def test_parallel_subagents_respect_shared_turn_budget(tmp_path: Path) -> None:
    """Runs multiple subagents concurrently and verifies atomic reservation never overspends."""
    storage = StorageManager(workspace=tmp_path)
    storage.ensure_dirs()
    session = Session(id="s_par", title="par", messages=[])
    executor = ToolExecutor(workspace=tmp_path, session=session, storage=storage, get_config=lambda: {})
    cfg = {
        "max_subagents_per_turn": 4,
        "subagent_turn_budget_usd": 0.10,
        "subagent_max_cost_usd": 0.05,
    }
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg, storage=storage)
    engine._turn_subagent_spent = 0.0
    engine._turn_subagent_reserved = 0.0
    engine._turn_subagent_count = 0
    engine._subagent_budget_lock = asyncio.Lock()

    async def fake_explore(
        goal: str,
        model_id: str,
        api_base: str,
        api_key: str,
        subagent: Any = None,
        call_budget_usd: float | None = None,
    ) -> str:
        await asyncio.sleep(0.02)
        spend = min(0.04, call_budget_usd) if call_budget_usd is not None else 0.04
        engine._turn_subagent_spent += spend
        return f"result for {goal}"

    engine._explore_with_llm = fake_explore  # type: ignore[method-assign]

    # With $0.10 shared cap and $0.05 per-call reserve, at most 2 concurrent subagents can reserve at once;
    # the 3rd concurrent call is blocked atomically before launch, so spend never exceeds $0.10!
    results = await asyncio.gather(
        engine._run_subagent({"goal": "a"}, model_id="m", api_base="https://api.example/v1", api_key="k"),
        engine._run_subagent({"goal": "b"}, model_id="m", api_base="https://api.example/v1", api_key="k"),
        engine._run_subagent({"goal": "c"}, model_id="m", api_base="https://api.example/v1", api_key="k"),
    )
    succeeded = [r for r in results if r.ok]
    rejected = [r for r in results if not r.ok]
    assert len(succeeded) == 2
    assert len(rejected) == 1
    assert "budget" in rejected[0].summary
    assert engine._turn_subagent_spent <= 0.10 + 1e-9
    assert engine._turn_subagent_reserved == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_autonomous_turn_recovers_from_tool_failure(tmp_path: Path) -> None:
    """Verifies an autonomous loop recovers when a tool call fails and completes the task."""
    (tmp_path / "target.py").write_text("X = 10\n", encoding="utf-8")
    storage = StorageManager(workspace=tmp_path)
    storage.ensure_dirs()
    session = Session(id="s_err", title="err", messages=[])
    user = ChatMessage(id="u1", role="user", content="update target.py")
    session.messages.append(user)
    executor = ToolExecutor(workspace=tmp_path, session=session, storage=storage, get_config=lambda: {})
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: {"api_key": "k"}, storage=storage)

    step = {"n": 0}

    async def fake_step(**kwargs: Any) -> tuple[bool, str, list[dict[str, Any]]]:
        step["n"] += 1
        if step["n"] == 1:
            return (
                True,
                "trying missing file",
                [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"filePath": "missing.py"}'}}],
            )
        if step["n"] == 2:
            return (
                True,
                "reading target.py instead",
                [{"id": "c2", "type": "function", "function": {"name": "edit", "arguments": '{"filePath": "target.py", "oldString": "X = 10", "newString": "X = 20"}'}}],
            )
        return True, "Recovered and updated target.py.", []

    engine._non_stream_chat_step = fake_step  # type: ignore[method-assign]

    async def noop(*_a: Any, **_kw: Any) -> None:
        return None

    reply = await engine.run_turn(
        session=session,
        user_msg=user,
        agent_mode="build",
        model_id="openai/gpt-4o",
        on_tool_start=noop,
        on_tool_end=noop,
        on_status=noop,
        on_stream_chunk=None,
    )
    assert step["n"] == 3
    assert reply.tool_calls[0].status == "error"
    assert reply.tool_calls[1].status == "success"
    assert "X = 20" in (tmp_path / "target.py").read_text(encoding="utf-8")
    assert "Recovered" in reply.content


@pytest.mark.asyncio
async def test_repeat_loop_guard_breaks_identical_tool_cycle(tmp_path: Path) -> None:
    """Prevents infinite loops when the model repeats the exact same tool call."""
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    storage = StorageManager(workspace=tmp_path)
    session = Session(id="s_loop", title="loop", messages=[])
    user = ChatMessage(id="u1", role="user", content="loop test")
    session.messages.append(user)
    executor = ToolExecutor(workspace=tmp_path, session=session, storage=storage, get_config=lambda: {})
    engine = AgentEngine(
        workspace=tmp_path, executor=executor, get_config=lambda: {"api_key": "k", "max_steps": 20}, storage=storage
    )

    calls = {"n": 0}

    async def stuck_step(**kwargs: Any) -> tuple[bool, str, list[dict[str, Any]]]:
        calls["n"] += 1
        return (
            True,
            "re-reading",
            [{"id": f"c_{calls['n']}", "type": "function", "function": {"name": "read", "arguments": '{"filePath": "a.py"}'}}],
        )

    engine._non_stream_chat_step = stuck_step  # type: ignore[method-assign]

    async def noop(*_a: Any, **_kw: Any) -> None:
        return None

    reply = await engine.run_turn(
        session=session,
        user_msg=user,
        agent_mode="build",
        model_id="openai/gpt-4o",
        on_tool_start=noop,
        on_tool_end=noop,
        on_status=noop,
        on_stream_chunk=None,
    )
    assert calls["n"] == 4
    assert "Loop guard" in reply.content


@pytest.mark.asyncio
async def test_ask_and_review_modes_block_file_mutations(tmp_path: Path) -> None:
    storage = StorageManager(workspace=tmp_path)
    ex = ToolExecutor(workspace=tmp_path, session=Session(), storage=storage, get_config=lambda: {})
    for mode in ("ask", "review"):
        res = await ex.execute("write", {"filePath": "x.py", "content": "1"}, agent_mode=mode)
        assert res.ok is False
        assert not (tmp_path / "x.py").exists()
