"""The daily budget must bind every client of the engine, not only the TUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rbit.agent import AgentEngine
from rbit.models import ChatMessage, Session, short_id
from rbit.tools import ToolExecutor
from rbit.usage import UsageLedger


def _engine(tmp_path: Path, cfg: dict[str, Any]) -> AgentEngine:
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: cfg)
    return AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg)


async def _turn(engine: AgentEngine, session: Session) -> ChatMessage:
    user = ChatMessage(id=short_id(), role="user", content="hello")
    session.messages.append(user)

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    return await engine.run_turn(session, user, "build", "rbit/smart-local", _noop, _noop, _noop)


def test_a_bare_engine_books_and_reads_the_workspace_ledger(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {})
    assert isinstance(engine.usage, UsageLedger) and engine.usage.workspace == tmp_path


@pytest.mark.asyncio
async def test_strict_daily_budget_blocks_a_headless_turn_before_any_paid_call(tmp_path: Path) -> None:
    cfg = {"daily_budget_usd": 0.01, "budget_mode": "strict"}
    engine = _engine(tmp_path, cfg)
    engine.usage.record("acme/model", cost_usd=0.02, input_tokens=10, output_tokens=10)  # already over the cap

    reply = await _turn(engine, Session())

    assert "Budget guard" in reply.content and "daily_budget_usd" in reply.content


@pytest.mark.asyncio
async def test_warn_mode_and_no_limit_do_not_block(tmp_path: Path) -> None:
    from rbit.usage import BudgetGuard

    engine = _engine(tmp_path, {"daily_budget_usd": 0.01, "budget_mode": "warn"})
    engine.usage.record("acme/model", cost_usd=0.5, input_tokens=1, output_tokens=1)
    assert BudgetGuard(engine.usage, engine.get_config()).check("acme/model")[0] is True
    unlimited = _engine(tmp_path, {})
    assert BudgetGuard(unlimited.usage, {}).check("acme/model")[0] is True
