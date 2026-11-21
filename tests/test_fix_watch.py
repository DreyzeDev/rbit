"""`rbit watch --run`: one reaction per burst of changes, no feedback loop, nothing lost.

Time is faked: `time.sleep` inside the watch loop runs a script of "external events" at
chosen polls and raises KeyboardInterrupt when the script is over, and the agent turn is
replaced by a fake that records its prompt and writes a file (its own edit).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

import rbit.cli as cli
from rbit.models import ChatMessage


class _Agent:
    """Stands in for AgentEngine.run_turn."""

    def __init__(self, workspace: Path, fail_first: bool = False) -> None:
        self.workspace = workspace
        self.prompts: list[str] = []
        self.fail_first = fail_first

    async def __call__(self, session: Any, user_msg: Any, agent_mode: str, model_id: str,
                       on_tool_start: Any, on_tool_end: Any, on_status: Any, **_kw: Any) -> ChatMessage:
        self.prompts.append(user_msg.content)
        # The agent edits the workspace itself: that must NOT count as a new change.
        (self.workspace / "agent_out.txt").write_text(f"turn {len(self.prompts)}\n", encoding="utf-8")
        if self.fail_first and len(self.prompts) == 1:
            raise RuntimeError("provider exploded")
        return ChatMessage(id="r1", role="assistant", content="done", agent="build", model=model_id)


def _watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: dict[int, Callable[[], None]],
    polls: int = 12,
    run: str | None = "check the change",
    fail_first: bool = False,
) -> _Agent:
    agent = _Agent(tmp_path, fail_first=fail_first)
    monkeypatch.setattr("rbit.agent.AgentEngine.run_turn", agent)
    count = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        index = count["n"]
        count["n"] += 1
        if index in events:
            events[index]()
        if index >= polls:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    assert cli._run_watch(tmp_path, run, 0.5) == 0
    return agent


def _write(tmp_path: Path, rel: str, text: str = "x\n") -> Callable[[], None]:
    def action() -> None:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    return action


def test_a_new_file_triggers_exactly_one_turn_and_the_agents_own_edit_does_not_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _watch(tmp_path, monkeypatch, {0: _write(tmp_path, "ext.py")}, polls=12)
    assert len(agent.prompts) == 1  # 12 polls, but the agent's agent_out.txt is not a change


def test_the_agent_is_told_which_files_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _watch(tmp_path, monkeypatch, {0: _write(tmp_path, "src/a.py")})
    assert agent.prompts[0].startswith("check the change")
    assert "src/a.py" in agent.prompts[0].replace("\\", "/")


def test_a_burst_of_saves_is_one_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # call 0 = poll, call 1 = the debounce wait: a second file saved during it joins the same turn
    agent = _watch(
        tmp_path, monkeypatch, {0: _write(tmp_path, "a.py"), 1: _write(tmp_path, "b.py")}, polls=8
    )
    assert len(agent.prompts) == 1
    assert "a.py" in agent.prompts[0] and "b.py" in agent.prompts[0]


def test_modifying_an_existing_file_triggers_but_heavy_dirs_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "keep.py").write_text("old\n", encoding="utf-8")
    for heavy in ("node_modules/pkg/index.js", ".git/HEAD", ".rbit/notes.txt", "__pycache__/x.pyc"):
        _write(tmp_path, heavy)()
    import os

    def touch_everything() -> None:
        for heavy in ("node_modules/pkg/index.js", ".git/HEAD", ".rbit/notes.txt", "__pycache__/x.pyc"):
            path = tmp_path / heavy
            os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 50))

    agent = _watch(tmp_path, monkeypatch, {0: touch_everything}, polls=6)
    assert agent.prompts == []  # nothing outside the ignored dirs changed

    (tmp_path / "keep.py").write_text("new, longer content\n", encoding="utf-8")
    os.utime(tmp_path / "keep.py", (1, 2_000_000_000))  # make the mtime differ for sure
    agent = _watch(tmp_path, monkeypatch, {0: touch_everything}, polls=6)
    assert agent.prompts == []  # still: the baseline is taken at start, so no change is seen

    agent = _watch(
        tmp_path, monkeypatch,
        {0: lambda: os.utime(tmp_path / "keep.py", (1, 2_000_000_100))}, polls=6,
    )
    assert len(agent.prompts) == 1 and "keep.py" in agent.prompts[0]


def test_a_failed_turn_does_not_stop_watching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = _watch(
        tmp_path, monkeypatch,
        {0: _write(tmp_path, "one.py"), 5: _write(tmp_path, "two.py")},
        polls=12, fail_first=True,
    )
    assert len(agent.prompts) == 2  # the second change was still handled
    assert "watch turn failed" in capsys.readouterr().out


def test_without_run_it_only_reports_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = _watch(tmp_path, monkeypatch, {0: _write(tmp_path, "ext.py")}, polls=5, run=None)
    assert agent.prompts == []
    assert "changed:" in capsys.readouterr().out


def test_a_watch_turn_is_saved_as_a_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _watch(tmp_path, monkeypatch, {0: _write(tmp_path, "ext.py")}, polls=6)
    files = list((tmp_path / ".rbit" / "sessions").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["title"].startswith("watch ")
