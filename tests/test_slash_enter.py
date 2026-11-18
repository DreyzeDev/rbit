"""Enter on a slash command must act immediately, not just refill the input.

Reported symptom: typing `/effort` and pressing Enter only rewrote the input to
`/effort ` and did nothing; a second Enter was needed. The completion handler
treated a slash command like a file path — a fragment to be spliced in — even
though a slash command is a complete action.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.screen import ModalScreen

from rbit.app import RbitApp
from rbit.widgets import AutocompletePopup


async def _type(pilot, text: str) -> None:
    for ch in text:
        await pilot.press("space" if ch == " " else ch)
        await pilot.pause(0.01)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typed,expected_modal",
    [
        ("/eff", "EffortSelectorModal"),
        ("/effort", "EffortSelectorModal"),
        ("/themes", "ThemeSelectorModal"),
        ("/todo", "TodoManagerModal"),
    ],
)
async def test_single_enter_opens_command_modal(
    workspace: Path, typed: str, expected_modal: str
) -> None:
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.15)
        inp = app.query_one("#prompt-input")

        await _type(pilot, typed)
        # The menu really is open at this point — that is what made the old
        # behaviour surprising: Enter appeared to be swallowed by it.
        assert app.query_one(AutocompletePopup).display is True

        await pilot.press("enter")
        await pilot.pause(0.4)

        screen = app.screen
        assert isinstance(screen, ModalScreen), f"{typed} did not act on the first Enter"
        assert type(screen).__name__ == expected_modal
        assert inp.value == ""


@pytest.mark.asyncio
async def test_second_enter_is_not_required(workspace: Path) -> None:
    """Explicitly pin the old two-Enter flow: one Enter leaves nothing pending."""
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.15)
        inp = app.query_one("#prompt-input")

        await _type(pilot, "/eff")
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert isinstance(app.screen, ModalScreen)
        # The input must not be left holding a half-accepted command.
        assert inp.value == ""
        assert app.query_one(AutocompletePopup).display is False


@pytest.mark.asyncio
async def test_arguments_survive_the_completion(workspace: Path) -> None:
    """`/permissions auto` keeps its argument when the menu resolves the command."""
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.15)
        await _type(pilot, "/permissions auto")
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert app.config_data.get("permission_mode") == "auto"


@pytest.mark.asyncio
async def test_bare_slash_does_not_fire_first_match(workspace: Path) -> None:
    """A lone `/` is ambiguous: Enter must not launch whatever sorts first."""
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.15)
        await _type(pilot, "/")
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert not isinstance(app.screen, ModalScreen)


@pytest.mark.asyncio
async def test_file_completion_still_fills_in_without_submitting(workspace: Path) -> None:
    """A path is only part of a prompt, so Enter there must keep editing."""
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.15)
        inp = app.query_one("#prompt-input")

        # Set the value directly: `pilot.press` cannot type Cyrillic, and the
        # @-completion path is debounced, so give the timer its window.
        inp.value = "look at @mai"
        await pilot.pause(0.4)
        assert app.query_one(AutocompletePopup).display is True

        await pilot.press("enter")
        await pilot.pause(0.3)

        # Still in the input, now with the full path, and nothing was sent.
        assert not isinstance(app.screen, ModalScreen)
        assert "@main.py" in inp.value
        assert app.session.messages == []
