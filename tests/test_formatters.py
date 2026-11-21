"""Automatic formatters: opt-in config, custom commands, ordering, failures, apply_patch."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from rbit.formatters import format_file
from rbit.models import Session
from rbit.tools import ToolExecutor


def _upper_script(tmp_path: Path) -> Path:
    script = tmp_path / "upper.py"
    script.write_text(
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "path.write_text(path.read_text(encoding='utf-8').upper(), encoding='utf-8')\n",
        encoding="utf-8",
    )
    return script


def _fail_script(tmp_path: Path) -> Path:
    script = tmp_path / "fail.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    return script


def _executor(tmp_path: Path, config: dict[str, Any]) -> ToolExecutor:
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: config)
    # A custom formatter command comes from the project config and runs arbitrary
    # code, so it only runs with the user's standing shell approval. These tests
    # model a user who has granted it; see test_fix_tools.py for the denied case.
    executor.always_allow_bash = True
    return executor


def _custom(script: Path, suffix: str = ".foo", **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": [sys.executable, str(script), "$FILE"], "extensions": [suffix]}
    entry.update(extra)
    return entry


@pytest.mark.asyncio
async def test_custom_formatter_runs_after_write(tmp_path: Path) -> None:
    config = {"formatter": {"upper": _custom(_upper_script(tmp_path))}}
    executor = _executor(tmp_path, config)
    result = await executor.execute("write", {"filePath": "note.foo", "content": "hello\n"})
    assert result.ok is True
    assert (tmp_path / "note.foo").read_text(encoding="utf-8") == "HELLO\n"
    assert "upper: note.foo (formatted)" in result.output


@pytest.mark.asyncio
async def test_formatter_disabled_by_default(tmp_path: Path) -> None:
    executor = _executor(tmp_path, {})
    result = await executor.execute("write", {"filePath": "note.foo", "content": "hello\n"})
    assert result.ok is True
    assert (tmp_path / "note.foo").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_individual_formatter_can_be_disabled(tmp_path: Path) -> None:
    config = {"formatter": {"upper": _custom(_upper_script(tmp_path), disabled=True)}}
    executor = _executor(tmp_path, config)
    await executor.execute("write", {"filePath": "note.foo", "content": "hello\n"})
    assert (tmp_path / "note.foo").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_failed_formatter_keeps_tool_success(tmp_path: Path) -> None:
    config = {"formatter": {"broken": _custom(_fail_script(tmp_path))}}
    executor = _executor(tmp_path, config)
    result = await executor.execute("write", {"filePath": "note.foo", "content": "hello\n"})
    assert result.ok is True
    assert (tmp_path / "note.foo").read_text(encoding="utf-8") == "hello\n"
    assert "broken: note.foo failed" in result.output


@pytest.mark.asyncio
async def test_first_successful_formatter_wins(tmp_path: Path) -> None:
    script = _upper_script(tmp_path)
    config = {
        "formatter": {
            "broken": _custom(_fail_script(tmp_path)),
            "upper": _custom(script),
        }
    }
    executor = _executor(tmp_path, config)
    await executor.execute("write", {"filePath": "note.foo", "content": "hello\n"})
    assert (tmp_path / "note.foo").read_text(encoding="utf-8") == "HELLO\n"


@pytest.mark.asyncio
async def test_formatter_environment_is_passed(tmp_path: Path) -> None:
    script = tmp_path / "env.py"
    script.write_text(
        "import os, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "path.write_text(os.environ['STYLE'], encoding='utf-8')\n",
        encoding="utf-8",
    )
    config = {"formatter": {"env": _custom(script, environment={"STYLE": "styled"})}}
    executor = _executor(tmp_path, config)
    await executor.execute("write", {"filePath": "note.foo", "content": "hello\n"})
    assert (tmp_path / "note.foo").read_text(encoding="utf-8") == "styled"


@pytest.mark.asyncio
async def test_apply_patch_formats_touched_file(tmp_path: Path) -> None:
    (tmp_path / "a.foo").write_text("one\n", encoding="utf-8")
    patch = "--- a/a.foo\n+++ b/a.foo\n@@ -1 +1 @@\n-one\n+aaaa\n"
    config = {"formatter": {"upper": _custom(_upper_script(tmp_path))}}
    executor = _executor(tmp_path, config)
    result = await executor.execute("apply_patch", {"patch": patch})
    assert result.ok is True
    assert (tmp_path / "a.foo").read_text(encoding="utf-8") == "AAAA\n"


@pytest.mark.asyncio
async def test_builtin_skipped_without_executable_or_dependency(tmp_path: Path) -> None:
    (tmp_path / "page.html").write_text("<p>hi</p>\n", encoding="utf-8")
    result = format_file(tmp_path / "page.html", {"formatter": True}, tmp_path)
    assert result.ran is False
