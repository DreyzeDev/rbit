"""Persistent terminal tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from rbit.models import Session
from rbit.tools import ToolExecutor


@pytest.mark.asyncio
async def test_terminal_runs_command_and_persists(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=dict)
    first = await executor.execute("terminal", {"command": "echo hello-rbit"})
    assert first.ok is True
    assert "hello-rbit" in first.output
    second = await executor.execute("terminal", {"action": "run", "command": "echo second"})
    assert second.ok is True
    assert "second" in second.output
    closed = await executor.execute("terminal", {"action": "close"})
    assert closed.ok is True
