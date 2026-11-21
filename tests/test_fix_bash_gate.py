"""`bash` must fail closed like `terminal` when nobody can approve the command."""

from __future__ import annotations

from pathlib import Path

import pytest

from rbit.models import Session
from rbit.tools import ToolExecutor


def _executor(tmp_path: Path, **kwargs) -> ToolExecutor:
    return ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: kwargs.pop("cfg", {}), **kwargs)


@pytest.mark.asyncio
async def test_bash_without_prompt_channel_refuses_mutating_commands(tmp_path: Path) -> None:
    result = await _executor(tmp_path).execute("bash", {"command": "echo hi > marker.txt"})

    assert result.ok is False and "no interactive client" in result.output
    assert not (tmp_path / "marker.txt").exists()


@pytest.mark.asyncio
async def test_bash_read_only_commands_still_run_without_prompt_channel(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    result = await _executor(tmp_path).execute("bash", {"command": "cat a.txt"})
    assert result.ok is True and "x" in result.output


@pytest.mark.asyncio
async def test_standing_approval_lets_unattended_runs_use_bash(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    ex.always_allow_bash = True  # what `rbit run`, ACP and `--auto` set
    result = await ex.execute("bash", {"command": "echo hi > marker.txt"})
    assert result.ok is True and (tmp_path / "marker.txt").exists()


@pytest.mark.asyncio
async def test_an_allow_rule_or_auto_mode_is_a_standing_approval(tmp_path: Path) -> None:
    rule = [{"action": "shell", "resource": "echo *", "effect": "allow"}]
    ex = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: {"permissions": rule})
    assert (await ex.execute("bash", {"command": "echo hi > a.txt"})).ok is True
    auto = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: {"permission_mode": "auto"})
    assert (await auto.execute("bash", {"command": "echo hi > b.txt"})).ok is True
