"""Scheduled prompts: parsing, persistence, tool, and CLI helpers."""

from __future__ import annotations

import time
from argparse import Namespace
from pathlib import Path

import pytest

from rbit.cli import _run_schedule
from rbit.models import Session
from rbit.scheduler import Scheduler
from rbit.tools import ToolExecutor


def test_schedule_next_run_specs() -> None:
    base = 1_700_000_000.0
    assert Scheduler.next_run("every 30m", after=base) == base + 1800
    daily = Scheduler.next_run("daily 09:30", after=base)
    assert daily is not None and daily > base
    assert Scheduler.next_run("2030-01-01T10:00:00") is not None
    assert Scheduler.next_run("nonsense") is None


def test_schedule_crud_due(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    entry, error = scheduler.add("check CI", "every 1h", name="ci")
    assert entry is not None and not error
    assert scheduler.due(now=time.time() + 7200)[0]["id"] == entry["id"]
    assert scheduler.remove("ci") is True
    assert scheduler.load() == []


@pytest.mark.asyncio
async def test_schedule_tool(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=dict)
    created = await executor.execute("schedule", {"action": "create", "prompt": "nightly", "schedule": "every 1d"})
    assert created.ok is True
    listed = await executor.execute("schedule", {"action": "list"})
    assert "nightly" in listed.output
    removed = await executor.execute("schedule", {"action": "remove", "name": "nightly"})
    assert removed.ok is True


def test_schedule_cli_add_list_remove(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_schedule(Namespace(dir=str(tmp_path), schedule_cmd="add", prompt="p", schedule="every 2h", name="n", model="", agent="build")) == 0
    assert _run_schedule(Namespace(dir=str(tmp_path), schedule_cmd="list")) == 0
    assert "n" in capsys.readouterr().out
    assert _run_schedule(Namespace(dir=str(tmp_path), schedule_cmd="remove", target="n")) == 0

def test_schedule_cron_parser() -> None:
    base = 1_700_000_000.0
    upcoming = Scheduler.next_run("* * * * *", after=base)
    assert upcoming is not None and upcoming > base
    assert upcoming - base <= 60
    invalid = Scheduler.next_run("not a cron", after=base)
    assert invalid is None
