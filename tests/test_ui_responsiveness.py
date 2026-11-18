"""Regression tests for UI responsiveness fixes (audit of blocking calls).

Each test pins one fix:
- checkpoint listing must not re-read every JSON on each sidebar refresh
- the git branch must not be resolved synchronously in the constructor
- @-file completion must be debounced instead of walking the tree per keystroke
- session saves must not run on the UI thread
- the event-loop lag watchdog must record stalls
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rbit.models import CheckpointMeta
from rbit.storage import StorageManager


# --- checkpoint caching ---------------------------------------------------


def test_list_checkpoints_caches_until_directory_changes(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_dirs()
    storage.save_checkpoint("first", {"a.txt": "hello"})
    storage.invalidate_checkpoint_cache()

    first = storage.list_checkpoints()
    assert len(first) == 1

    # Second call must be served from the cache: tamper with the file content
    # so a real re-read would produce different results.
    (storage.checkpoints_dir / f"{first[0].id}.json").write_text(
        '{"id": "tampered", "label": "tampered", "files": {}}',
        encoding="utf-8",
    )
    cached = storage.list_checkpoints()
    assert cached[0].id == first[0].id


def test_save_checkpoint_invalidates_cache(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_dirs()
    storage.save_checkpoint("one", {"a.txt": "x"})
    assert len(storage.list_checkpoints()) == 1

    storage.save_checkpoint("two", {"b.txt": "y"})
    assert len(storage.list_checkpoints()) == 2


def test_checkpoint_count_matches_list(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_dirs()
    assert storage.checkpoint_count() == 0

    storage.save_checkpoint("one", {"a.txt": "x"})
    storage.save_checkpoint("two", {"b.txt": "y"})

    assert storage.checkpoint_count() == len(storage.list_checkpoints()) == 2


def test_checkpoint_count_avoids_json_parsing(tmp_path: Path, monkeypatch) -> None:
    """Once warm, the count must come from cache without touching json.loads."""
    storage = StorageManager(tmp_path)
    storage.ensure_dirs()
    storage.save_checkpoint("one", {"a.txt": "x"})
    assert storage.checkpoint_count() == 1

    calls = {"n": 0}
    real_loads = __import__("json").loads

    def counting_loads(*args, **kwargs):
        calls["n"] += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr("json.loads", counting_loads)
    assert storage.checkpoint_count() == 1
    assert calls["n"] == 0


def test_list_checkpoints_returns_copy(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.ensure_dirs()
    storage.save_checkpoint("one", {"a.txt": "x"})

    items = storage.list_checkpoints()
    items.append(CheckpointMeta(id="ghost", label="ghost", created_at="", file_count=0))
    assert len(storage.list_checkpoints()) == 1


# --- UI app behaviour -----------------------------------------------------


@pytest.fixture()
def app_workspace(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text("# hi\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_git_branch_resolved_off_constructor(app_workspace: Path) -> None:
    """`git_branch` starts empty and is filled by a background worker."""
    from rbit.app import RbitApp

    app = RbitApp(workspace=app_workspace)
    # Nothing may shell out during construction.
    assert app.git_branch == ""


@pytest.mark.asyncio
async def test_file_completion_is_debounced(app_workspace: Path) -> None:
    from rbit.app import RbitApp

    app = RbitApp(workspace=app_workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        executed = {"n": 0}

        def boom(limit: int = 120):
            executed["n"] += 1
            return ["main.py"]

        app.executor.list_workspace_files = boom  # type: ignore[assignment]

        app._queue_file_completion("main")
        app._queue_file_completion("main.")
        app._queue_file_completion("main.p")
        # Rapid typing must not trigger a single workspace walk yet.
        assert executed["n"] == 0

        await pilot.pause(0.3)
        assert executed["n"] == 1


@pytest.mark.asyncio
async def test_lag_watchdog_records_stall(app_workspace: Path) -> None:
    from rbit.app import RbitApp

    app = RbitApp(workspace=app_workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.ui_lag_stats["count"] == 0

        # Pretend the loop was blocked for a full second.
        app._last_tick = asyncio.get_running_loop().time() - 2.0
        app._check_event_loop_lag()

        stats = app.ui_lag_stats
        assert stats["count"] >= 1
        assert stats["worst_ms"] > 250.0


# --- http client / TLS context -------------------------------------------


def test_ssl_context_is_created_once() -> None:
    """httpx rebuilt the TLS context per client (~250 ms of CA bundle loading)."""
    from rbit import agent

    first = agent._default_ssl_context()
    second = agent._default_ssl_context()
    assert first is second


def test_http_client_reuses_cached_context() -> None:
    from rbit import agent

    ctx = agent._default_ssl_context()
    client = agent._http_client(5.0)
    try:
        assert client is not None
        # The shared context must be what gets cached, not a fresh object.
        assert agent._default_ssl_context() is ctx
    finally:
        pass


def test_no_duplicate_client_construction_in_agent() -> None:
    """Every request path must go through the cached-context factory."""
    source = Path(__file__).resolve().parents[1] / "rbit" / "agent.py"
    body = source.read_text(encoding="utf-8")
    # One definition inside `_http_client`; no ad-hoc constructions elsewhere.
    assert body.count("httpx.AsyncClient(") == 1


# --- streaming: the answer must stay visible -----------------------------


def _session_with_user_turn() -> Any:
    """A session with one user message puts the TUI in session state.

    In home state `#chat-scroll` is `display: none`, so it has zero height and
    scroll assertions are meaningless.
    """
    from rbit.models import ChatMessage, Session, short_id

    sess = Session()
    sess.messages = [ChatMessage(id=short_id(), role="user", content="go")]
    return sess


@pytest.mark.asyncio
async def test_streaming_reply_tail_stays_visible(app_workspace: Path) -> None:
    """Regression: the end of a streamed answer ended up below the fold.

    `scroll_end()` resolves against the *previous* maximum scroll offset, but
    mounting a card (or growing a Markdown widget) only reflows on the next
    frame. With a 20-chunk stream the visible area lagged by 16 rows, so the
    user saw the start of the answer and the tail was cut off.
    """
    from rbit.app import RbitApp
    from rbit.models import ChatMessage, short_id
    from rbit.widgets import AssistantMessageCard, ChatScroll

    app = RbitApp(workspace=app_workspace, initial_session=_session_with_user_turn())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.1)
        scroll = app.query_one("#chat-scroll", ChatScroll)

        app._live_card = None
        acc: list[str] = []
        for i in range(20):
            acc.append("\n".join(f"line {i}-{j}" for j in range(6)))
            partial = "\n\n".join(acc)
            if app._live_card is None:
                draft = ChatMessage(
                    id=short_id(),
                    role="assistant",
                    content=partial,
                    agent="build",
                    model="m",
                )
                app._live_card = AssistantMessageCard(draft, [], live=True)
                await scroll.mount(app._live_card)
            else:
                await app._live_card.update_stream(partial)
            app._smart_scroll_end()
            await pilot.pause(0.02)

        await pilot.pause(0.35)

        assert scroll.max_scroll_y > 10, "test needs a scrollable log to be meaningful"
        gap = scroll.max_scroll_y - scroll.scroll_y
        assert gap <= 1, f"answer tail hidden: {gap:.0f} rows below the fold"


@pytest.mark.asyncio
async def test_pinning_does_not_hijack_user_scroll_up(app_workspace: Path) -> None:
    """Pinning must never fight the user: scrolling up must stick."""
    from rbit.app import RbitApp
    from rbit.models import ChatMessage, Session, short_id
    from rbit.widgets import ChatScroll, UserMessageCard

    sess = Session()
    sess.messages = [
        ChatMessage(id=short_id(), role="user", content=f"msg {i}\n" * 3) for i in range(20)
    ]
    app = RbitApp(workspace=app_workspace, initial_session=sess)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause(0.15)
        scroll = app.query_one("#chat-scroll", ChatScroll)
        scroll.mount_all(
            [
                UserMessageCard(ChatMessage(id=short_id(), role="user", content=f"m{i}\n" * 3))
                for i in range(20)
            ]
        )
        await pilot.pause(0.1)
        scroll.pin_to_end()
        await pilot.pause(0.2)
        assert scroll.stuck_to_bottom is True

        scroll.scroll_home(animate=False)
        await pilot.pause(0.2)
        assert scroll.stuck_to_bottom is False

        before = scroll.scroll_y
        scroll.pin_to_end()
        await pilot.pause(0.2)
        assert scroll.scroll_y == before, "pin_to_end must not yank a user who scrolled up"


@pytest.mark.asyncio
async def test_tool_cards_then_final_answer_stays_visible(app_workspace: Path) -> None:
    """Regression: after tool cards the final answer was appended off-screen.

    `watch_scroll_y` compared the post-pin offset against a `max_scroll_y` that
    had just grown (a mounted tool card), concluded "the user scrolled up" and
    cleared `stuck_to_bottom`. Auto-follow then stayed off for the rest of the
    turn, so the tools were visible but the model's answer was below the fold —
    it looked like the AI never answered.
    """
    from rbit.app import RbitApp
    from rbit.models import ChatMessage, ToolCallRecord, short_id
    from rbit.widgets import AssistantMessageCard, ChatScroll, ToolCallCard

    app = RbitApp(workspace=app_workspace, initial_session=_session_with_user_turn())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.1)
        scroll = app.query_one("#chat-scroll", ChatScroll)
        scroll.pin_to_end(force=True)
        await pilot.pause(0.1)
        assert scroll.stuck_to_bottom is True

        # A turn that runs many tools, exactly like on_tool_start/on_tool_end.
        for i in range(6):
            rec = ToolCallRecord(
                id=f"t{i}",
                name="read",
                arguments={"filePath": f"src/mod{i}.py"},
                output="\n".join(f"line {n}" for n in range(8)),
                status="success",
                summary="Read 8 lines",
            )
            await scroll.mount(ToolCallCard(rec))
            app._smart_scroll_end()
            await pilot.pause(0.03)
            assert scroll.stuck_to_bottom is True, f"lost auto-follow after tool {i}"

        # Then the answer streams in and is finalized.
        app._live_card = None
        acc: list[str] = []
        for i in range(8):
            acc.append("\n".join(f"answer {i}-{j}" for j in range(5)))
            partial = "\n\n".join(acc)
            if app._live_card is None:
                draft = ChatMessage(
                    id=short_id(), role="assistant", content=partial, agent="build", model="m"
                )
                app._live_card = AssistantMessageCard(draft, [], live=True)
                await scroll.mount(app._live_card)
            else:
                await app._live_card.update_stream(partial)
            app._smart_scroll_end()
            await pilot.pause(0.02)

        await pilot.pause(0.3)

        assert scroll.max_scroll_y > 10, "test needs a scrollable log"
        gap = scroll.max_scroll_y - scroll.scroll_y
        assert gap <= 1, f"final answer hidden below the fold: gap {gap:.0f} rows"


@pytest.mark.asyncio
async def test_new_turn_reengages_following_after_user_scrolled_up(app_workspace: Path) -> None:
    """Submitting a prompt must re-attach to the tail even from mid-log."""
    from rbit.app import RbitApp
    from rbit.models import ChatMessage, Session, short_id
    from rbit.widgets import ChatScroll, UserMessageCard

    sess = Session()
    sess.messages = [
        ChatMessage(id=short_id(), role="user", content=f"old {i}\n" * 3) for i in range(20)
    ]
    app = RbitApp(workspace=app_workspace, initial_session=sess)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause(0.15)
        scroll = app.query_one("#chat-scroll", ChatScroll)
        scroll.scroll_home(animate=False)
        await pilot.pause(0.1)
        assert scroll.stuck_to_bottom is False

        await scroll.mount(UserMessageCard(ChatMessage(id=short_id(), role="user", content="new")))
        scroll.pin_to_end(force=True)
        await pilot.pause(0.2)

        assert scroll.stuck_to_bottom is True
        assert scroll.max_scroll_y - scroll.scroll_y <= 1


@pytest.mark.asyncio
async def test_answer_revealed_at_end_of_turn_even_if_user_scrolled_up(
    app_workspace: Path,
) -> None:
    """The reported bug: tools run, user scrolls up, the answer is never seen.

    During a turn auto-follow is deliberately timid so it cannot fight the user.
    That is fine while tools stream, but when the turn completes the answer is
    exactly what was asked for — `_reveal_turn_result()` re-attaches and pins it
    into view.
    """
    from rbit.app import RbitApp
    from rbit.models import ChatMessage, Session, ToolCallRecord, short_id
    from rbit.widgets import ChatScroll, ToolCallCard

    # Pre-existing history: the log is already scrollable before the turn
    # starts, so "scroll up mid-turn" is a meaningful action.
    sess = Session()
    for t in range(8):
        sess.messages.append(
            ChatMessage(id=short_id(), role="user", content=f"раньше {t}\n" * 2)
        )
        prev = ChatMessage(
            id=short_id(), role="assistant",
            content=f"ответ {t}\n" + "\n".join(f"строка {i}" for i in range(8)),
            agent="build", model="m", duration_s=1.0,
        )
        for i in range(5):
            prev.tool_calls.append(
                ToolCallRecord(
                    id=f"h{t}_{i}", name="read",
                    arguments={"filePath": f"old{i}.py"},
                    output="\n".join(f"l{n}" for n in range(8)),
                    status="success", summary="Read",
                )
            )
        sess.messages.append(prev)
    sess.messages.append(ChatMessage(id=short_id(), role="user", content="почини сборку"))
    app = RbitApp(workspace=app_workspace, initial_session=sess)

    async def fake_run_turn(session, user_msg, agent_mode, model_id, on_tool_start=None,
                            on_tool_end=None, on_status=None, on_stream_chunk=None,
                            on_thinking=None, on_reasoning_chunk=None, **_kw):
        for i in range(10):
            rec = ToolCallRecord(
                id=f"t{i}",
                name="read",
                arguments={"filePath": f"m{i}.py"},
                output="\n".join(f"l{n}" for n in range(12)),
                status="success",
                summary="Read",
            )
            if on_tool_start:
                await on_tool_start(rec)
            if on_tool_end:
                await on_tool_end(rec)
            await asyncio.sleep(0.03)
        return ChatMessage(
            id=short_id(),
            role="assistant",
            content="\n".join(f"итог {i}" for i in range(25)),
            agent=agent_mode,
            model=model_id,
            duration_s=2.0,
        )

    app.engine.run_turn = fake_run_turn  # type: ignore[assignment]

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.4)
        scroll = app.query_one("#chat-scroll", ChatScroll)
        assert scroll.max_scroll_y > 10, "test needs history that scrolls"

        app._submit_prompt("почини сборку")
        for _ in range(40):
            await pilot.pause(0.05)
            if app.is_busy and len(list(scroll.query("ToolCallCard"))) >= 15:
                break

        # The user scrolls up while the turn is still running. Set the flag
        # explicitly: driving it through scroll_home races with the turn's own
        # streaming pins, which is a harness artefact, not product behaviour.
        scroll.stuck_to_bottom = False
        scroll.scroll_home(animate=False)
        await pilot.pause(0.1)

        # Let the turn finish; reveal must bring the answer back.
        for _ in range(100):
            await pilot.pause(0.1)
            if not app.is_busy:
                break
        await pilot.pause(1.2)

        assert scroll.max_scroll_y > 10, "test needs a scrollable log"
        gap = scroll.max_scroll_y - scroll.scroll_y
        assert gap <= 1, f"answer not revealed at end of turn: {gap:.0f} rows hidden"


@pytest.mark.asyncio
async def test_finalize_renders_when_compose_has_not_run_yet(app_workspace: Path) -> None:
    """`finalize()` used to bail out on a fresh card and lose the answer."""
    from rbit.app import RbitApp
    from rbit.models import ChatMessage, short_id
    from rbit.widgets import AssistantMessageCard, ChatScroll

    app = RbitApp(workspace=app_workspace, initial_session=_session_with_user_turn())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.1)
        scroll = app.query_one("#chat-scroll", ChatScroll)

        reply = ChatMessage(
            id=short_id(),
            role="assistant",
            content="финальный ответ, который обязан быть виден",
            agent="build",
            model="m",
            duration_s=1.5,
        )
        card = AssistantMessageCard(reply, [], live=True)
        scroll.mount(card)  # not awaited: children are not composed yet
        await card.finalize(reply)
        await pilot.pause(0.4)

        md = card.query_one("Markdown")
        rendered = getattr(md, "_markdown", "") or ""
        assert "финальный ответ" in rendered
        assert card.size.height > 0
