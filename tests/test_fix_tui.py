"""Regression tests for TUI bug fixes (app.py, modals.py, widgets.py, terminal.py)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input

from rbit.app import (
    RbitApp,
    format_at_ref,
    parse_at_refs,
    trailing_at_query,
)
from rbit.modals import (
    CommandPaletteModal,
    EffortSelectorModal,
    HelpModal,
    PromptHistoryModal,
    SearchResultsModal,
    SessionPickerModal,
    TodoManagerModal,
)
from rbit.models import ChatMessage, Session, TodoItem, ToolCallRecord
from rbit.terminal import PersistentShell, TerminalError
from rbit.tools import ToolResult
from rbit.widgets import AutocompletePopup, ChatScroll, HomeLogo, ToolCallCard

SIZE = (120, 40)


def _messages(n: int) -> list[ChatMessage]:
    return [ChatMessage(id=f"m{i}", role="user", content=f"line {i}") for i in range(n)]


def _reply(n: int) -> ChatMessage:
    return ChatMessage(
        id=f"a{n}", role="assistant", content=f"ответ {n}", agent="build", model="m", duration_s=0.1
    )


async def _wait_until(pilot: Any, predicate: Any, tries: int = 400) -> None:
    for _ in range(tries):
        await pilot.pause()
        if predicate():
            return
    raise AssertionError("condition never became true")


class _Notes:
    """Collects notifications so tests can assert on them."""

    def __init__(self, app: RbitApp) -> None:
        self.items: list[tuple[str, str]] = []
        original = app.notify

        def spy(message: str, *args: Any, **kwargs: Any) -> None:
            self.items.append((str(message), str(kwargs.get("severity", "information"))))
            original(message, *args, **kwargs)

        app.notify = spy  # type: ignore[method-assign]

    def text(self) -> str:
        return "\n".join(m for m, _ in self.items)


# 1. "Load older messages" must survive repeated rebuilds -------------------


@pytest.mark.asyncio
async def test_load_older_twice_does_not_raise_duplicate_ids(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.messages = _messages(130)
        app._rebuild_chat_view()
        app._load_older()  # still truncated: a second "load-older" notice is mounted
        await pilot.pause()
        app._load_older()
        await pilot.pause()
        scroll = app.query_one("#chat-scroll", ChatScroll)
        notices = [c for c in scroll.children if str(c.id or "").startswith("load-older")]
        assert len(notices) == 1


# 2. stale @-completion timer --------------------------------------------


@pytest.mark.asyncio
async def test_stale_file_completion_timer_does_not_resurrect_popup(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", Input)
        popup = app.query_one(AutocompletePopup)
        inp.focus()
        inp.value = "@ma"
        await pilot.pause()
        assert app._completion_timer is not None
        inp.value = ""
        await pilot.pause(0.4)
        assert popup.display is False
        assert app._completion_timer is None


@pytest.mark.asyncio
async def test_file_completion_rechecks_input_before_showing(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.executor.list_workspace_files = lambda limit=120: ["main.py"]  # type: ignore[assignment]
        app._completion_query = "ma"
        app._run_file_completion()  # the field is empty: nothing may pop up
        assert app.query_one(AutocompletePopup).display is False


@pytest.mark.asyncio
async def test_enter_on_empty_input_with_stale_popup_does_not_insert(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", Input)
        popup = app.query_one(AutocompletePopup)
        inp.focus()
        popup.show_files("ma", ["main.py"])
        assert popup.display is True
        await pilot.press("enter")
        await pilot.pause()
        assert inp.value == ""
        assert popup.display is False


# 3. prompt history modal -------------------------------------------------


@pytest.mark.asyncio
async def test_history_modal_returns_full_prompt_newest_first(tmp_path: Path) -> None:
    long_prompt = "z" * 300 + " [/bad] tail"
    history = ["oldest", "[bold]markup[/", long_prompt]
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        picked: list[str | None] = []
        modal = PromptHistoryModal(history)
        app.push_screen(modal, picked.append)
        await pilot.pause()
        olist = modal.query_one("#history-list")
        assert olist.option_count == 3
        assert str(olist.get_option_at_index(2).prompt) == "oldest"  # type: ignore[union-attr]
        olist.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert picked == [long_prompt]  # untruncated, and newest was first


@pytest.mark.asyncio
async def test_history_modal_ids_are_stable_and_unique(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        modal = PromptHistoryModal(["a", "b", "a", "c"])
        app.push_screen(modal)
        await pilot.pause()
        olist = modal.query_one("#history-list")
        ids = [olist.get_option_at_index(i).id for i in range(olist.option_count)]  # type: ignore[union-attr]
        assert len(ids) == len(set(ids)) == 3


# 4. slash popup: custom command shadows a built-in -------------------------


@pytest.mark.asyncio
async def test_custom_command_overriding_builtin_does_not_duplicate_ids(tmp_path: Path) -> None:
    cmds = tmp_path / ".rbit" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "help.md").write_text("# My help\nExplain {args}\n", encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", Input)
        inp.focus()
        inp.value = "/hel"
        await pilot.pause()
        popup = app.query_one(AutocompletePopup)
        assert popup.display is True
        assert popup.values.count("/help") == 1
        assert "My help" in str(popup.get_option("/help").prompt)


# 5. search modal: several hits in one message ------------------------------


@pytest.mark.asyncio
async def test_search_modal_handles_multiple_hits_in_one_message(tmp_path: Path) -> None:
    session = Session()
    msg = ChatMessage(id="msg1", role="assistant", content="needle in text", agent="build")
    msg.tool_calls = [
        ToolCallRecord(id="t1", name="grep", arguments={}, status="success", output="needle in tool"),
        ToolCallRecord(id="t2", name="grep", arguments={}, status="success", output="another needle"),
    ]
    session.messages = [msg]
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        picked: list[str | None] = []
        modal = SearchResultsModal(session, "needle")
        app.push_screen(modal, picked.append)
        await pilot.pause()
        olist = modal.query_one("#search-session-list")
        for _ in range(40):  # the list fills asynchronously; wait instead of racing it
            if olist.option_count == 3:
                break
            await pilot.pause(0.05)
        assert olist.option_count == 3
        olist.focus()
        olist.highlighted = 2
        await pilot.press("enter")
        await pilot.pause()
        assert picked == ["msg1"]  # the real message id, not the unique option id


# 6. todo modal: duplicate ids ---------------------------------------------


@pytest.mark.asyncio
async def test_todo_modal_tolerates_duplicate_ids(tmp_path: Path) -> None:
    todos = [
        TodoItem(id="same", content="one", status="pending", priority="medium"),
        TodoItem(id="same", content="two", status="pending", priority="high"),
    ]
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        modal = TodoManagerModal(todos)
        app.push_screen(modal)
        await pilot.pause()
        assert modal.query_one("#todo-modal-list").option_count == 2
        modal.query_one("#todo-modal-list").highlighted = 1
        modal.action_cycle_prio()
        assert modal.todos[1].priority == "low"
        assert modal.todos[0].priority == "medium"


# 7. arrow-key prompt history -----------------------------------------------


@pytest.mark.asyncio
async def test_arrow_keys_browse_prompt_history(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.prompt_history = ["first", "second", "third"]
        inp = app.query_one("#prompt-input", Input)
        inp.focus()
        seen: list[str] = []
        for key in ("up", "up", "up", "up", "down", "down", "down", "down"):
            await pilot.press(key)
            await pilot.pause()
            seen.append(inp.value)
        assert seen == ["third", "second", "first", "first", "second", "third", "", ""]


@pytest.mark.asyncio
async def test_arrow_up_does_not_clobber_text_being_typed(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.prompt_history = ["old"]
        inp = app.query_one("#prompt-input", Input)
        inp.focus()
        inp.value = "typing now"
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert inp.value == "typing now"


# 8. /export ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_rejects_paths_outside_workspace_and_nul(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    app = RbitApp(workspace=ws)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        notes = _Notes(app)
        app._cmd_export("../outside.md")
        app._cmd_export("bad\x00name.md")
        assert not (tmp_path / "outside.md").exists()
        assert notes.text().count("Export failed") == 2


@pytest.mark.asyncio
async def test_export_creates_parent_folders(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.messages = _messages(2)
        app._cmd_export("reports/deep/session.md")
        assert (tmp_path / "reports" / "deep" / "session.md").is_file()


# 9. worker exceptions must not kill the app --------------------------------


@pytest.mark.asyncio
async def test_macro_with_non_object_step_does_not_crash_app(tmp_path: Path) -> None:
    macros = tmp_path / ".rbit" / "macros"
    macros.mkdir(parents=True)
    (macros / "m.json").write_text('["just a string", {"tool": "todoread", "args": {}}]', encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app._handle_slash_command("/macro m") is True
        await _wait_until(pilot, lambda: not app.is_busy and app._busy_worker is None)
        assert app.is_running


@pytest.mark.asyncio
async def test_run_coro_turns_exceptions_into_notices(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()

        async def boom() -> None:
            raise RuntimeError("kaboom")

        app._run_coro(boom(), "/boom")
        await pilot.pause(0.2)
        assert app.is_running
        texts = [str(c.render()) for c in app.query_one("#chat-scroll").children]
        assert any("kaboom" in t for t in texts)


@pytest.mark.asyncio
async def test_get_editor_survives_unbalanced_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", '"unclosed editor --wait')
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        editor = app._get_editor()
        assert editor and isinstance(editor, list)


@pytest.mark.asyncio
async def test_get_editor_keeps_windows_paths_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific splitting")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", r'"C:\Program Files\Editor\ed.exe" --wait')
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app._get_editor() == [r"C:\Program Files\Editor\ed.exe", "--wait"]


# 10. effort modal: markup-looking model name --------------------------------


@pytest.mark.asyncio
async def test_effort_modal_accepts_markup_like_model_name(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.push_screen(EffortSelectorModal("auto", "vendor/model[/x]"))
        await pilot.pause()
        assert isinstance(app.screen, EffortSelectorModal)


# 11. Esc must not leave tool cards "running" ----------------------------------


@pytest.mark.asyncio
async def test_interrupt_marks_running_tool_cards(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        rec = ToolCallRecord(id="t1", name="read", arguments={"filePath": "a"}, status="running")
        done = ToolCallRecord(id="t2", name="read", arguments={"filePath": "b"}, status="success")
        card, done_card = ToolCallCard(rec), ToolCallCard(done)
        await app.query_one("#chat-scroll", ChatScroll).mount(card, done_card)
        app.is_busy = True
        app.action_interrupt_or_close()
        await pilot.pause()
        assert rec.status == "error" and rec.summary == "interrupted"
        assert done.status == "success"


# 12. queued prompt must be dispatched after !bang and /compact ------------------


def _install_fake_turn(app: RbitApp) -> list[str]:
    calls: list[str] = []

    async def fake_run_turn(**kwargs: Any) -> ChatMessage:
        calls.append(kwargs["user_msg"].content)
        return _reply(len(calls))

    app.engine.run_turn = fake_run_turn  # type: ignore[assignment]
    return calls


@pytest.mark.asyncio
async def test_queued_prompts_run_in_order_after_bang(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    calls = _install_fake_turn(app)
    release = asyncio.Event()

    async def slow_bang(cmd: str) -> ToolResult:
        await release.wait()
        return ToolResult(ok=True, summary="ok", output="out")

    app.executor.execute_bang_shell = slow_bang  # type: ignore[assignment]
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app._busy_worker = app._execute_bang_command("echo hi")
        await _wait_until(pilot, lambda: app.is_busy)
        app._submit_prompt("one")
        app._submit_prompt("two")
        assert app._queued_prompts == ["one", "two"]
        release.set()
        await _wait_until(pilot, lambda: len(calls) == 2 and not app.is_busy)
        assert calls == ["one", "two"]


@pytest.mark.asyncio
async def test_queued_prompt_runs_after_compact(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    calls = _install_fake_turn(app)
    release = asyncio.Event()

    async def slow_summary(msgs: Any, model: str) -> str:
        await release.wait()
        return "summary"

    app.engine.summarize_session = slow_summary  # type: ignore[assignment]
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.messages = _messages(6)
        app._cmd_compact()
        assert app.is_busy  # set synchronously
        app._submit_prompt("after compact")
        assert app._queued_prompts == ["after compact"]
        release.set()
        await _wait_until(pilot, lambda: calls == ["after compact"] and not app.is_busy)


# 13. /test, /fix, /macro are busy jobs ------------------------------------------


@pytest.mark.asyncio
async def test_test_command_is_busy_cancellable_and_exclusive(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    started = asyncio.Event()
    cancelled = {"flag": False}

    async def slow_execute(tool: str, args: dict[str, Any]) -> ToolResult:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise
        return ToolResult(ok=True, summary="", output="")

    app.executor.execute = slow_execute  # type: ignore[assignment]
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        notes = _Notes(app)
        assert app._handle_slash_command("/test echo hi") is True
        assert app.is_busy is True
        await asyncio.wait_for(started.wait(), 5)
        # No parallel start while the job runs.
        assert app._start_busy_job(lambda: asyncio.sleep(0), "second") is False
        assert "busy" in notes.text()
        await pilot.press("escape")
        await _wait_until(pilot, lambda: cancelled["flag"])
        assert app.is_busy is False


@pytest.mark.asyncio
async def test_failing_test_job_hands_output_to_agent_after_idle(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    calls = _install_fake_turn(app)

    async def failing(tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=False, summary="", output="FAILED test_x")

    app.executor.execute = failing  # type: ignore[assignment]
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app._handle_slash_command("/fix pytest")
        await _wait_until(pilot, lambda: len(calls) == 1 and not app.is_busy)
        assert "FAILED test_x" in calls[0]


# 14. deleting sessions -------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_session_cannot_be_deleted_while_busy(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.messages = _messages(2)
        app.storage.save_session(app.session)
        sid = app.session.id
        app.is_busy = True
        assert app._delete_session_from_picker(sid) is False
        assert app.storage.find_session(sid) is not None
        app.is_busy = False
        assert app._delete_session_from_picker(sid) is True
        assert app.storage.find_session(sid) is None


@pytest.mark.asyncio
async def test_ctrl_d_in_session_picker_needs_confirmation(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        sessions = [Session(title="Alpha"), Session(title="Beta")]
        result: list[Any] = []
        modal = SessionPickerModal(sessions, "other")
        app.push_screen(modal, result.append)
        await pilot.pause()
        modal.query_one("#session-list").highlighted = 1
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert result == [] and app.screen is modal  # first press only arms
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert result == [("delete", sessions[0].id)]


# 15. narrow terminal -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_screen_fits_narrow_terminal(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(60, 30)) as pilot:
        await pilot.pause()
        assert app.query_one(HomeLogo).region.width <= 60
        assert app.query_one("#prompt-outer").region.width <= 60


# 16. /explain -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_builds_a_real_attachment_reference(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        sent: list[str] = []
        app._submit_prompt = sent.append  # type: ignore[method-assign]
        app._cmd_explain("src/main.py")
        app._cmd_explain("my dir/a b.py")
        assert sent[0].endswith("@src/main.py") and "@ " not in sent[0]
        assert sent[1].endswith('@"my dir/a b.py"')


# 17. @-references with spaces ------------------------------------------------------------


def test_at_reference_helpers_support_spaces() -> None:
    assert parse_at_refs('see @"a b.txt" and @c.py#L3-5') == ["a b.txt", "c.py#L3-5"]
    assert format_at_ref("a b.txt") == '@"a b.txt"'
    assert format_at_ref("src\\x.py") == "@src/x.py"
    assert trailing_at_query('look at @"my f') == "my f"
    assert trailing_at_query("look at @src/ma") == "src/ma"
    assert trailing_at_query('look @"done.txt" ') is None


@pytest.mark.asyncio
async def test_file_with_spaces_is_completed_and_attached(tmp_path: Path) -> None:
    (tmp_path / "a b.txt").write_text("hi\n", encoding="utf-8")
    app = RbitApp(workspace=tmp_path)
    calls = _install_fake_turn(app)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", Input)
        inp.value = "read @a"
        app._apply_completion("file", "a b.txt")
        assert inp.value == 'read @"a b.txt" '
        app._submit_prompt(inp.value)
        await _wait_until(pilot, lambda: calls and not app.is_busy)
        user = next(m for m in app.session.messages if m.role == "user")
        assert user.attachments == ["a b.txt"]


# 18. palette over a modal ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_palette_does_not_open_over_a_modal(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")  # on the main screen it opens rbit's own palette
        await pilot.pause()
        assert isinstance(app.screen, CommandPaletteModal)
        await pilot.press("escape")
        await pilot.pause()
        app.push_screen(HelpModal())
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, HelpModal)
        assert len(app.screen_stack) == 2


# 19. persistent terminal ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_timeout_restarts_shell_and_drops_late_output(tmp_path: Path) -> None:
    shell = PersistentShell(tmp_path, timeout=1.0)
    slow = "ping -n 5 127.0.0.1 >nul & echo LATEOUTPUT" if os.name == "nt" else "sleep 4; echo LATEOUTPUT"
    try:
        with pytest.raises(TerminalError, match="timed out"):
            await shell.run(slow)
        shell.timeout = 20.0
        out = await shell.run("echo fresh-start")
        assert "fresh-start" in out
        assert "LATEOUTPUT" not in out
    finally:
        await shell.close()


@pytest.mark.asyncio
async def test_terminal_output_has_no_banner_prompt_or_duplicates(tmp_path: Path) -> None:
    shell = PersistentShell(tmp_path, timeout=20.0)
    # Output arrives in several reads: it used to be appended twice.
    cmd = "echo one & ping -n 2 127.0.0.1 >nul & echo two" if os.name == "nt" else "echo one; sleep 1; echo two"
    try:
        out = await shell.run(cmd)
        assert [ln.strip() for ln in out.splitlines()] == ["one", "two"]
    finally:
        await shell.close()


# 20. /rewind, /continue, /edit -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewind_and_continue_are_refused_while_busy(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        notes = _Notes(app)
        app.is_busy = True
        assert app._handle_slash_command("/rewind") is True
        await pilot.pause()
        assert len(app.screen_stack) == 1
        app._handle_slash_command("/continue abc")
        await pilot.pause()
        assert notes.text().count("busy") >= 2


@pytest.mark.asyncio
async def test_edit_does_not_precreate_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = RbitApp(workspace=tmp_path)
    target = tmp_path / "newdir" / "new.txt"
    seen: dict[str, bool] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> None:
        seen["existed_at_launch"] = target.exists()

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        monkeypatch.setattr("rbit.app.subprocess.run", fake_run)
        monkeypatch.setattr(app, "suspend", lambda: contextlib.nullcontext())
        await app._cmd_edit("newdir/new.txt")
        assert seen == {"existed_at_launch": False}
        assert not target.exists()


# 21. compaction keeps pinned messages ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_keeps_pinned_messages_and_resets_busy(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)

    async def summary(msgs: Any, model: str) -> str:
        return "summary"

    app.engine.summarize_session = summary  # type: ignore[assignment]
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        msgs = _messages(8)
        msgs[2].pinned = True
        app.session.messages = msgs
        app._cmd_compact()
        await _wait_until(pilot, lambda: not app.is_busy and len(app.session.messages) < 8)
        ids = [m.id for m in app.session.messages]
        assert "m2" in ids and "m7" in ids and "m6" in ids
        assert "m3" not in ids


@pytest.mark.asyncio
async def test_compact_failure_does_not_leave_app_busy(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)

    async def failing_summary(msgs: Any, model: str) -> str:
        raise RuntimeError("llm down")

    def broken_heuristic(msgs: Any) -> str:
        raise RuntimeError("heuristic broken too")

    app.engine.summarize_session = failing_summary  # type: ignore[assignment]
    app.engine._heuristic_summary = broken_heuristic  # type: ignore[assignment]
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.messages = _messages(6)
        app._cmd_compact()
        await _wait_until(pilot, lambda: not app.is_busy)
        assert app.is_busy is False


# 22. Esc must cancel any turn ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escape_cancels_plain_turn_after_retry(tmp_path: Path) -> None:
    app = RbitApp(workspace=tmp_path)
    cancelled = {"flag": False}
    block = {"on": False}

    async def fake_run_turn(**kwargs: Any) -> ChatMessage:
        if block["on"]:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled["flag"] = True
                raise
        return _reply(1)

    app.engine.run_turn = fake_run_turn  # type: ignore[assignment]
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app._submit_prompt("first")
        await _wait_until(pilot, lambda: not app.is_busy)
        app._cmd_retry()  # leaves a stale worker reference behind (before the fix)
        await _wait_until(pilot, lambda: not app.is_busy)
        block["on"] = True
        app._submit_prompt("second")
        await _wait_until(pilot, lambda: app.is_busy)
        assert app._busy_worker is not None
        await pilot.press("escape")
        await _wait_until(pilot, lambda: cancelled["flag"])
        assert app.is_busy is False
        assert app._busy_worker is None


@pytest.mark.asyncio
async def test_toasts_show_bracketed_text_literally(workspace: Path) -> None:
    """`Skill not found: [name]` used to show "Skill not found: ": Textual read `[name]` as markup."""
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=SIZE) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.query("PromptSubHints"):
                break
        app.notify("Skill not found: [name]  Bad: '[red]x'  Macro: C:\m\[/oops]", severity="warning", timeout=30)
        await pilot.pause(0.2)
        toast = next(n for n in app._notifications if n.message.startswith("Skill not found"))
        assert toast.markup is False
        assert toast.message == "Skill not found: [name]  Bad: '[red]x'  Macro: C:\m\[/oops]"


def test_diff_block_fence_outgrows_backticks_inside_the_diff() -> None:
    """A changed README with its own ``` lines used to close the viewer's code block early."""
    from rbit.modals import markdown_diff_block

    plain = markdown_diff_block("+x\n-y")
    assert plain.startswith("```diff\n") and plain.endswith("\n```")
    fenced = "```python\nprint(1)\n```"
    block = markdown_diff_block(f"+{fenced}")
    opening = block.split("\n", 1)[0]
    assert opening == "````diff"  # one backtick longer than the longest run inside
    assert block.endswith("\n````") and fenced in block
    assert markdown_diff_block("+````x").startswith("`````diff")


@pytest.mark.asyncio
async def test_retry_resends_the_users_prompt_not_the_auto_self_check_followup(workspace: Path) -> None:
    """After a self-check the last user message is the app's own "[auto] ..." text; /retry reran that."""
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=SIZE) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.query("PromptSubHints"):
                break
        sent: list[ChatMessage] = []
        app._execute_agent_turn = lambda msg: sent.append(msg)  # type: ignore[method-assign,assignment]
        app.session.messages = [
            ChatMessage(id="u1", role="user", content="add a --json flag"),
            ChatMessage(id="a1", role="assistant", content="done"),
            ChatMessage(id="u2", role="user", content="[auto] Self-check found problems in the files you just changed:\n\nx"),
            ChatMessage(id="a2", role="assistant", content="fixed"),
        ]
        app._cmd_retry()
        assert [m.id for m in sent] == ["u1"]
        assert [m.id for m in app.session.messages] == ["u1"]


def _executor(workspace: Path) -> Any:
    from rbit.tools import ToolExecutor

    return ToolExecutor(workspace, Session(title="t"))


def test_refused_undo_keeps_the_snapshot_so_it_works_after_the_conflict_is_gone(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    executor._push_snapshot("edit a", target, True, "old\n", "new\n", b"old\n")
    target.write_text("someone else\n", encoding="utf-8")  # the file changed after rbit's edit

    assert "Cannot undo" in executor.undo_last()
    assert len(executor.undo_stack) == 1  # it used to be thrown away here

    target.write_text("new\n", encoding="utf-8")  # conflict resolved
    assert "Reverted" in executor.undo_last()
    assert target.read_text(encoding="utf-8") == "old\n"


def test_undo_turn_skips_a_conflicting_file_and_still_restores_the_rest(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A1\n", encoding="utf-8")
    b.write_text("B1\n", encoding="utf-8")
    executor._push_snapshot("edit a", a, True, "A0\n", "A1\n", b"A0\n")
    executor._push_snapshot("edit b", b, True, "B0\n", "B1\n", b"B0\n")
    a.write_text("A-user\n", encoding="utf-8")

    assert executor.undo_to_count(0) == 1  # terminates, reverts b only
    assert b.read_text(encoding="utf-8") == "B0\n" and a.read_text(encoding="utf-8") == "A-user\n"
    assert executor.undo_stack == []


@pytest.mark.asyncio
async def test_new_session_forgets_the_previous_sessions_undo_history(workspace: Path) -> None:
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=SIZE) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.query("PromptSubHints"):
                break
        target = workspace / "x.txt"
        target.write_text("1\n", encoding="utf-8")
        app.executor._push_snapshot("edit x", target, True, "0\n", "1\n", b"0\n")
        app.action_new_session()
        await pilot.pause(0.1)
        assert app.executor.undo_stack == [] and app.executor.redo_stack == []
        app._cmd_undo()
        assert target.read_text(encoding="utf-8") == "1\n"  # the old session's edit stays


@pytest.mark.asyncio
async def test_clear_chat_is_saved(workspace: Path) -> None:
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=SIZE) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.query("PromptSubHints"):
                break
        app.session.messages = [ChatMessage(id="u1", role="user", content="hi")]
        app.storage.save_session(app.session)
        app.action_clear_chat()
        await pilot.pause(0.1)
        assert app.storage.find_session(app.session.id).messages == []


@pytest.mark.asyncio
async def test_bang_command_typed_while_busy_stays_in_the_input(workspace: Path) -> None:
    """`!pytest` pressed during a turn only said "busy" and the typed command was lost."""
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=SIZE) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.query("PromptSubHints"):
                break
        app.is_busy = True
        inp = app.query_one("#prompt-input", Input)
        inp.focus()
        inp.value = "!echo hi"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert inp.value == "!echo hi"
        app.is_busy = False


@pytest.mark.asyncio
async def test_cost_notice_has_no_literal_markdown_asterisks(workspace: Path) -> None:
    app = RbitApp(workspace=workspace)
    async with app.run_test(size=SIZE) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.query("PromptSubHints"):
                break
        app._cmd_info("/cost")
        await pilot.pause(0.1)
        notices = [str(w.render()) for w in app.query(".system-notice")]
        assert any("Estimated session cost: $" in n for n in notices)
        assert not any("**" in n for n in notices)


def test_every_palette_entry_has_a_dispatcher_and_the_new_ones_are_listed() -> None:
    """A palette row without a handler would be a dead button."""
    import re

    from rbit.modals import PALETTE_ITEMS

    source = (Path(__file__).resolve().parent.parent / "rbit" / "app.py").read_text(encoding="utf-8")
    dispatch = source[source.index("dispatch = {"):source.index("fn = dispatch.get(act)")]
    handled = set(re.findall(r'^\s*"([a-z_]+)":', dispatch, flags=re.MULTILINE))
    ids = {item[1] for item in PALETTE_ITEMS}
    assert ids <= handled, ids - handled
    assert {"undo_turn", "cost"} <= ids
