"""Automated headless TUI & CLI tests for rbit."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from rbit.app import RbitApp
from rbit.cli import main as cli_main
from rbit.widgets import (
    AgentModeBadge,
    AutocompletePopup,
    HomeLogo,
    HomeTipBanner,
    SessionHeaderBar,
    SidebarPanel,
)


@pytest.mark.asyncio
async def test_rbit_home_screen_and_agent_toggle(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        assert app.query_one(HomeLogo).display is True
        assert app.query_one(HomeTipBanner).display is True
        assert app.query_one(SessionHeaderBar).display is False
        assert app.query_one(SidebarPanel).display is False

        badge = app.query_one(AgentModeBadge)
        assert app.agent_mode == "build"
        assert "Build" in str(badge.render())

        await pilot.press("tab")
        await pilot.pause()
        assert app.agent_mode == "plan"

        await pilot.press("tab")
        await pilot.pause()
        assert app.agent_mode == "build"


@pytest.mark.asyncio
async def test_session_transition_todo_diff_undo_and_export(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    app.config_data["model"] = "rbit/smart-local"
    async with app.run_test(size=(130, 40)) as pilot:
        inp = app.query_one("#prompt-input")
        inp.focus()
        inp.value = "/todo Проверить интерфейс rbit"
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.session.todos) == 1
        assert app.query_one(SidebarPanel).display is True

        inp.focus()
        inp.value = "создай файл calc.py с калькулятором"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.query_one(HomeLogo).display is False
        assert app.query_one(SessionHeaderBar).display is True
        assert app.query_one(SidebarPanel).display is True

        created = tmp_path / "calc.py"
        assert created.exists()
        assert "calc.py" in app.session.modified_files
        assert len(app.session.todos) >= 3

        # /undo removes the created file
        inp.focus()
        inp.value = "/undo"
        await pilot.press("enter")
        await pilot.pause()
        assert not created.exists()

        # /redo restores the file
        inp.focus()
        inp.value = "/redo"
        await pilot.press("enter")
        await pilot.pause()
        assert created.exists()

        # /export writes Markdown transcript
        inp.focus()
        inp.value = "/export session_report.md"
        await pilot.press("enter")
        await pilot.pause()
        assert (tmp_path / "session_report.md").exists()

        # /new returns cleanly back to centered Home View
        inp.focus()
        inp.value = "/new"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(HomeLogo).display is True
        assert app.query_one(SessionHeaderBar).display is False


@pytest.mark.asyncio
async def test_bang_shell_checkpoints_and_memory(tmp_path: Path) -> None:
    sample = tmp_path / "notes.txt"
    sample.write_text("v1\n", encoding="utf-8")

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        inp = app.query_one("#prompt-input")

        # 1. Save checkpoint v1
        inp.focus()
        inp.value = "/checkpoint before-edit"
        await pilot.press("enter")
        await pilot.pause()
        cps = app.storage.list_checkpoints()
        assert len(cps) == 1
        assert cps[0].label == "before-edit"

        # Mutate workspace + create extra file
        sample.write_text("v2-mutated\n", encoding="utf-8")
        extra = tmp_path / "temp_extra.py"
        extra.write_text("x = 42\n", encoding="utf-8")

        # Rewind via executor
        app.executor.restore_checkpoint(cps[0].id)
        assert sample.read_text(encoding="utf-8") == "v1\n"
        assert not extra.exists()

        # 2. Persistent project memory (/remember)
        inp.focus()
        inp.value = "/remember Always use pytest and type hints"
        await pilot.press("enter")
        await pilot.pause()
        assert "Always use pytest" in app.storage.load_project_memory()

        # 3. Direct !command bang shell (zero LLM tokens)
        inp.focus()
        inp.value = "!echo rbit-bang-ok"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(HomeLogo).display is False
        assert len(app.session.messages) == 0  # Zero LLM messages/tokens used!


@pytest.mark.asyncio
async def test_autocomplete_and_modals(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        inp = app.query_one("#prompt-input")
        popup = app.query_one(AutocompletePopup)

        inp.value = "/ch"
        await pilot.pause()
        assert popup.display is True

        inp.value = "прочитай @ma"
        await pilot.pause()
        assert popup.display is True
        popup.display = False

        sidebar = app.query_one(SidebarPanel)
        assert sidebar.display is False
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.display is True

        for shortcut in ("ctrl+p", "ctrl+m", "ctrl+t", "ctrl+o"):
            await pilot.press(shortcut)
            await pilot.pause()
            assert len(app.screen_stack) == 2
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1


def test_cli_subcommands_headless_run_doctor_stats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 1. rbit run --json (explicitly using deterministic built-in rbit/smart-local)
    rc = cli_main(["run", "--json", "-m", "rbit/smart-local", "-d", str(tmp_path), "создай файл hello_cli.py"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert (tmp_path / "hello_cli.py").exists()
    assert payload["agent"] == "build"

    # 2. rbit doctor
    assert cli_main(["doctor", str(tmp_path)]) == 0
    capsys.readouterr()

    # 3. rbit stats & sessions & export
    assert cli_main(["stats", str(tmp_path)]) == 0
    assert cli_main(["sessions", str(tmp_path)]) == 0
    out_md = tmp_path / "cli_export.md"
    assert cli_main(["export", "-d", str(tmp_path), "-o", str(out_md)]) == 0
    assert out_md.exists()
