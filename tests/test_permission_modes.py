"""Permission mode tests: presets over the ordered allow/ask/deny rule engine.

A mode is only a shortcut for the *prompt* decision. It must never override an
explicit `deny` the user wrote, and it must never change the default outcome of
a request the engine already answers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rbit.models import Session
from rbit.permissions import (
    DEFAULT_MODE,
    MODE_IDS,
    mode_decision,
    mode_label,
    mode_short_label,
    mode_table,
    normalize_permission_mode,
    permission_mode_from_config,
)
from rbit.tools import ToolExecutor


def _executor(
    tmp_path: Path,
    cfg: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ToolExecutor:
    config = cfg if cfg is not None else {}
    return ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: config, **kwargs)


class _Prompter:
    """Records permission prompts and answers them."""

    def __init__(self, answer: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.answer = answer

    async def __call__(self, tool: str, detail: str) -> bool:
        self.calls.append((tool, detail))
        return self.answer


# --- pure mode logic ------------------------------------------------------


def test_mode_ids_and_labels_are_stable() -> None:
    assert MODE_IDS == ("ask", "commands", "edits", "auto")
    assert DEFAULT_MODE == "edits"
    assert mode_label("auto") == "Auto - allow everything"
    assert mode_short_label("ask") == "perms: ask"


def test_aliases_normalize_to_canonical_ids() -> None:
    assert normalize_permission_mode("yolo") == "auto"
    assert normalize_permission_mode("--dangerously-skip-permissions") == "auto"
    assert normalize_permission_mode("все") == "auto"
    assert normalize_permission_mode("commands-only") == "commands"
    assert normalize_permission_mode("strict") == "ask"
    # Garbage falls back to the caller's default, never to the requested mode.
    assert normalize_permission_mode("nonsense") == DEFAULT_MODE
    assert normalize_permission_mode(None) == DEFAULT_MODE
    assert normalize_permission_mode(42) == DEFAULT_MODE


def test_mode_decision_matrix() -> None:
    # ask: nothing automatic
    assert mode_decision("ask", "shell") == "ask"
    assert mode_decision("ask", "edit") == "ask"
    # commands: shell automatic, edits still prompt
    assert mode_decision("commands", "shell") == "allow"
    assert mode_decision("commands", "edit") == "ask"
    # edits: the documented default
    assert mode_decision("edits", "shell") == "ask"
    assert mode_decision("edits", "edit") == "allow"
    # auto: everything automatic
    assert mode_decision("auto", "shell") == "allow"
    assert mode_decision("auto", "edit") == "allow"
    assert mode_decision("auto", "external_directory") == "allow"
    # unrelated actions are never governed by a mode
    assert mode_decision("auto", "read") is None
    assert mode_decision("ask", "webfetch") is None


def test_non_auto_modes_do_not_invent_external_blocks() -> None:
    """Only `auto` has an opinion about paths outside the workspace."""
    for mode in ("ask", "commands", "edits"):
        assert mode_decision(mode, "external_directory") is None


def test_mode_table_matches_decisions() -> None:
    for mode_id, _label, _desc, cmd_auto, edit_auto in mode_table():
        assert cmd_auto is (mode_decision(mode_id, "shell") == "allow")
        assert edit_auto is (mode_decision(mode_id, "edit") == "allow")


def test_priority_of_config_sources() -> None:
    assert permission_mode_from_config({}, env={}) == DEFAULT_MODE
    assert permission_mode_from_config({"permission_mode": "ask"}, env={}) == "ask"
    # The legacy auto flag still means exactly "never prompt".
    assert permission_mode_from_config({"auto_approve": True}, env={}) == "auto"
    assert permission_mode_from_config({}, env={"RBIT_AUTO_APPROVE": "1"}) == "auto"
    # An explicit env mode is the strongest signal.
    assert permission_mode_from_config(
        {"auto_approve": True}, env={"RBIT_PERMISSION_MODE": "ask"}
    ) == "ask"
    assert permission_mode_from_config(
        {"permission_mode": "auto"}, env={"RBIT_PERMISSION_MODE": "commands"}
    ) == "commands"


# --- engine integration ---------------------------------------------------


@pytest.mark.asyncio
async def test_ask_mode_prompts_for_edits(tmp_path: Path) -> None:
    cfg = {"permission_mode": "ask"}
    prompter = _Prompter(answer=True)
    executor = _executor(tmp_path, cfg, permission_cb=prompter)

    result = await executor.execute("write", {"filePath": "a.txt", "content": "hi\n"})
    assert result.ok is True
    assert prompter.calls == [("edit", "a.txt")]


@pytest.mark.asyncio
async def test_default_mode_writes_without_prompt(tmp_path: Path) -> None:
    prompter = _Prompter()
    executor = _executor(tmp_path, {}, permission_cb=prompter)

    result = await executor.execute("write", {"filePath": "a.txt", "content": "hi\n"})
    assert result.ok is True
    assert prompter.calls == []
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hi\n"


@pytest.mark.asyncio
async def test_deny_rule_beats_auto_mode(tmp_path: Path) -> None:
    """`auto` silences prompts; it never unlocks an explicit deny."""
    cfg = {
        "permission_mode": "auto",
        "permissions": [{"action": "edit", "resource": "*.key", "effect": "deny"}],
    }
    executor = _executor(tmp_path, cfg)

    blocked = await executor.execute("write", {"filePath": "secret.key", "content": "k\n"})
    assert blocked.ok is False
    assert "Permission denied" in blocked.output
    assert not (tmp_path / "secret.key").exists()

    ok = await executor.execute("write", {"filePath": "notes.md", "content": "n\n"})
    assert ok.ok is True


@pytest.mark.asyncio
async def test_commands_mode_auto_approves_shell(tmp_path: Path) -> None:
    cfg = {"permission_mode": "commands"}
    prompter = _Prompter()
    executor = _executor(tmp_path, cfg, permission_cb=prompter)

    await executor.execute("bash", {"command": "echo hi"})
    assert prompter.calls == []


@pytest.mark.asyncio
async def test_commands_mode_still_prompts_for_edits(tmp_path: Path) -> None:
    cfg = {"permission_mode": "commands"}
    prompter = _Prompter(answer=False)
    executor = _executor(tmp_path, cfg, permission_cb=prompter)

    result = await executor.execute("write", {"filePath": "a.txt", "content": "x\n"})
    assert result.ok is False
    assert prompter.calls == [("edit", "a.txt")]
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_edits_mode_prompts_for_shell(tmp_path: Path) -> None:
    cfg = {"permission_mode": "edits"}
    prompter = _Prompter(answer=False)
    executor = _executor(tmp_path, cfg, permission_cb=prompter)

    await executor.execute("bash", {"command": "touch marker.txt"})
    assert prompter.calls == [("bash", "touch marker.txt")]
    assert not (tmp_path / "marker.txt").exists()


@pytest.mark.asyncio
async def test_auto_mode_never_prompts(tmp_path: Path) -> None:
    cfg = {"permission_mode": "auto"}
    prompter = _Prompter(answer=False)
    executor = _executor(tmp_path, cfg, permission_cb=prompter)

    written = await executor.execute("write", {"filePath": "a.txt", "content": "x\n"})
    assert written.ok is True
    await executor.execute("bash", {"command": "echo hi"})
    assert prompter.calls == []


@pytest.mark.asyncio
async def test_read_only_bash_never_prompts_in_any_mode(tmp_path: Path) -> None:
    """Read-only commands are already safe; modes must not add a prompt."""
    for mode in MODE_IDS:
        prompter = _Prompter()
        executor = _executor(tmp_path, {"permission_mode": mode}, permission_cb=prompter)
        await executor.execute("bash", {"command": "git status"})
        assert prompter.calls == [], f"{mode} prompted for a read-only command"


@pytest.mark.asyncio
async def test_explicit_ask_rule_survives_non_auto_modes(tmp_path: Path) -> None:
    """A rule that singles out a resource keeps prompting unless mode is `auto`."""
    for mode in ("ask", "commands", "edits"):
        cfg = {
            "permission_mode": mode,
            "permissions": [{"action": "edit", "resource": "*.sql", "effect": "ask"}],
        }
        prompter = _Prompter(answer=True)
        executor = _executor(tmp_path, cfg, permission_cb=prompter)

        result = await executor.execute("write", {"filePath": "seed.sql", "content": "select 1;\n"})
        assert result.ok is True
        assert prompter.calls == [("edit", "seed.sql")], f"{mode} ignored an explicit ask rule"


@pytest.mark.asyncio
async def test_auto_mode_silences_explicit_ask_rules(tmp_path: Path) -> None:
    """`auto` means literally never prompt; only `deny` survives it."""
    cfg = {
        "permission_mode": "auto",
        "permissions": [{"action": "edit", "resource": "*.sql", "effect": "ask"}],
    }
    prompter = _Prompter(answer=False)
    executor = _executor(tmp_path, cfg, permission_cb=prompter)

    result = await executor.execute("write", {"filePath": "seed.sql", "content": "select 1;\n"})
    assert result.ok is True
    assert prompter.calls == []
