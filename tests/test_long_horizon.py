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
                        "function": {"name": "read", "arguments": json.dumps({"filePath": "app.py"})},
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
    """Runs multiple subagents concurrently and verifies shared budget + concurrency limits."""
    storage = StorageManager(workspace=tmp_path)
    storage.ensure_dirs()
    session = Session(id="s_par", title="par", messages=[])
    executor = ToolExecutor(workspace=tmp_path, session=session, storage=storage, get_config=lambda: {})
    cfg = {
        "max_subagents_per_turn": 4,
        "subagent_turn_budget_usd": 0.10,
    }
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg, storage=storage)
    engine._turn_subagent_spent = 0.0
    engine._turn_subagent_count = 0

    async def fake_explore(goal: str, model_id: str, api_base: str, api_key: str, subagent: Any = None) -> str:
        await asyncio.sleep(0.01)
        engine._turn_subagent_spent += 0.04
        return f"result for {goal}"

    engine._explore_with_llm = fake_explore  # type: ignore[method-assign]

    results = await asyncio.gather(
        engine._run_subagent({"goal": "a"}, model_id="m", api_base="https://api.example/v1", api_key="k"),
        engine._run_subagent({"goal": "b"}, model_id="m", api_base="https://api.example/v1", api_key="k"),
        engine._run_subagent({"goal": "c"}, model_id="m", api_base="https://api.example/v1", api_key="k"),
    )
    assert all(r.ok for r in results)
    assert engine._turn_subagent_count == 3
    assert engine._turn_subagent_spent == pytest.approx(0.12)

    # Next subagent must be blocked because shared budget (0.12 >= 0.10) is exhausted
    blocked = await engine._run_subagent(
        {"goal": "d"}, model_id="m", api_base="https://api.example/v1", api_key="k"
    )
    assert blocked.ok is False
    assert "budget" in blocked.summary
