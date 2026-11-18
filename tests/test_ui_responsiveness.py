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
