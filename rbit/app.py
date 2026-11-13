"""Main rbit Textual Application — Full-screen TUI (Home View + Session View + Modals + Bang Shell + Checkpoints)."""

from __future__ import annotations

import asyncio
import difflib
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Input, OptionList, Static

from . import __version__
from .agent import AgentEngine
from .modals import (
    CheckpointModal,
    CommandPaletteModal,
    ConnectApiModal,
    DiffViewerModal,
    PromptHistoryModal,
    EffortSelectorModal,
    HelpModal,
    ModelSelectorModal,
    PermissionModal,
    SearchResultsModal,
    SessionPickerModal,
    ThemeSelectorModal,
    TodoManagerModal,
)
from .models import (
    ChatMessage,
    FileChangeStat,
    Session,
    TodoItem,
    ToolCallRecord,
    count_tokens,
    short_id,
)
from .storage import StorageManager
from .themes import RBIT_DARK, RBIT_HIGH_CONTRAST, RBIT_LIGHT
from .tools import MUTATING_TOOLS, VIRTUAL_ATTACHMENTS, ToolExecutor, detect_test_command
from .widgets import (
    AgentModeBadge,
    AssistantMessageCard,
    AutocompletePopup,
    BottomStatusBar,
    ChatScroll,
    HomeLogo,
    HomeTipBanner,
    PromptInput,
    PromptModelLabel,
    PromptSubHints,
    SessionHeaderBar,
    SidebarPanel,
    ThinkingIndicator,
    ToolCallCard,
    UserMessageCard,
    detect_git_branch,
)

CLIPBOARD_COMMANDS: tuple[tuple[str, ...], ...] = tuple(
    cmd
    for cmd in (
        ("pbcopy",) if sys.platform == "darwin" else (),
        ("wl-copy",) if not sys.platform.startswith("win") else (),
        ("xclip", "-selection", "clipboard") if not sys.platform.startswith("win") else (),
        ("xsel", "--clipboard", "--input") if not sys.platform.startswith("win") else (),
        ("clip",) if sys.platform.startswith("win") else (),
    )
    if cmd
)


def _write_clipboard(text: str) -> bool:
    """Copies text using pyperclip when available, else native clipboard utilities."""
    if not text:
        return False
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        return True
    except Exception:
        pass
    for cmd in CLIPBOARD_COMMANDS:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text, text=True, timeout=5, check=True)
            return True
        except Exception:
            continue
    return False


RBIT_TUI_CSS = """
Screen {
    background: $background;
    color: $foreground;
    scrollbar-background: transparent;
    scrollbar-background-hover: transparent;
    scrollbar-background-active: transparent;
    scrollbar-color: #2e2e2e;
    scrollbar-color-hover: #4a4a4a;
    scrollbar-color-active: #fab283;
    scrollbar-corner-color: transparent;
}

#workspace-row {
    width: 1fr;
    height: 1fr;
}

/* --- Center Stage: switches seamlessly between centered Home View and full Session View --- */
#center-stage {
    width: 1fr;
    height: 1fr;
}

#center-stage.is-home {
    align: center middle;
    padding: 0 3;
}

#center-stage.is-session {
    align: left top;
    padding: 0;
}

/* --- Home Logo & Tip --- */
#home-logo {
    width: 78;
    content-align: center middle;
    margin-bottom: 2;
}

#home-tip {
    width: 78;
    content-align: center middle;
    margin-top: 2;
}

/* --- Session Top Header Bar --- */
#session-header {
    width: 100%;
    height: auto;
    background: transparent;
    border-bottom: solid #1c1c1c;
    padding: 0 3;
    margin: 1 0 0 0;
}

/* --- Scrollable Session Feed --- */
#chat-scroll {
    width: 1fr;
    height: 1fr;
    padding: 1 3 0 3;
    scrollbar-size-vertical: 1;
    scrollbar-background: transparent;
    scrollbar-background-hover: transparent;
    scrollbar-background-active: transparent;
    scrollbar-color: #2e2e2e;
    scrollbar-color-hover: #4a4a4a;
    scrollbar-color-active: #fab283;
    scrollbar-corner-color: transparent;
}

.user-card {
    background: transparent;
    border-left: thick #3a3a3a;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

.user-card.mode-build {
    border-left: thick #fab283;
}

.user-card.mode-plan {
    border-left: thick #5c9cf5;
}

.tool-card {
    background: transparent;
    padding: 0 0 0 2;
    margin: 0 0 1 0;
    height: auto;
}

.tool-card:hover {
    background: #101010;
}

.thinking-line {
    height: auto;
    padding: 0 0 0 2;
    margin: 0 0 1 0;
}

.assistant-card {
    padding: 0 1 1 2;
    margin: 0 0 1 0;
    height: auto;
}

.assistant-md {
    margin: 0;
    padding: 0;
    background: transparent;
}

.assistant-meta {
    margin-top: 1;
}

.assistant-md MarkdownFence {
    background: #101216;
    color: #d6d6d6;
    margin: 0 0 1 0;
    padding: 0;
}

.assistant-md MarkdownFence > Label {
    padding: 0 1;
}

.assistant-md MarkdownH1,
.assistant-md MarkdownH2,
.assistant-md MarkdownH3 {
    margin: 1 0 0 0;
}

.system-notice {
    background: $surface;
    border-left: thick #5c9cf5;
    color: $foreground;
    padding: 1 2;
    margin: 1 0;
    height: auto;
}

/* --- Prompt Component --- */
#prompt-outer {
    height: auto;
}

#center-stage.is-home #prompt-outer {
    width: 78;
}

#center-stage.is-session #prompt-outer {
    width: 100%;
    padding: 0 3 1 3;
}

#autocomplete-popup {
    display: none;
    max-height: 10;
    background: $surface;
    border: round #2a2a2a;
    margin-bottom: 1;
}

#prompt-box {
    width: 100%;
    height: auto;
    background: $surface;
    padding: 0 2;
    border: round #2a2a2a;
}

#prompt-box.mode-build {
    border: round #fab283;
}

#prompt-box.mode-plan {
    border: round #5c9cf5;
}

#prompt-input {
    width: 1fr;
    border: none;
    background: transparent;
    padding: 0;
    height: 1;
    color: $foreground;
}

#prompt-input:focus {
    border: none;
    background: transparent;
}

#prompt-meta-row {
    width: 100%;
    height: 1;
    margin-top: 1;
    margin-bottom: 1;
}

#agent-badge {
    width: auto;
}

#prompt-model-label {
    width: auto;
}

#prompt-sub-hints {
    width: 100%;
    height: 1;
    margin-top: 0;
    padding: 0 1;
}

/* --- Flat Right Sidebar --- */
#sidebar {
    width: 36;
    height: 1fr;
    background: $surface;
    border-left: solid #1c1c1c;
    padding: 1 1;
}

.sb-block {
    height: auto;
    margin-bottom: 1;
    padding-bottom: 1;
    border-bottom: solid #1f1f1f;
}

/* --- 1-Line Bottom Footer --- */
#status-bar {
    dock: bottom;
    height: 1;
    background: $background;
    padding: 0 2;
}

/* --- Modals (Signature #fab283 highlight bar with dark text) --- */
ModalScreen {
    align: center middle;
    background: rgba(0, 0, 0, 0.76);
}

.oc-modal {
    width: 68;
    max-width: 92%;
    height: auto;
    max-height: 82%;
    background: $surface;
    border: round #2a2a2a;
    padding: 1 2;
}

.oc-modal-header {
    width: 100%;
    height: 1;
    margin-bottom: 1;
}

.oc-modal-search {
    border: none;
    background: $panel;
    padding: 0 1;
    height: 1;
    margin-bottom: 1;
}

.oc-modal-search:focus {
    border: none;
    background: $panel;
}

.oc-modal-list {
    height: auto;
    max-height: 18;
    background: transparent;
    border: none;
    padding: 0;
}

.oc-modal-list:focus {
    border: none;
}

OptionList > .option-list--option-highlighted {
    background: #fab283;
    color: #0a0a0a;
    text-style: bold;
}

#autocomplete-popup > .option-list--option-highlighted {
    background: #fab283;
    color: #0a0a0a;
    text-style: bold;
}

.oc-modal-foot {
    color: #808080;
    margin-top: 1;
}

.help-modal {
    height: 82%;
}

#help-scroll {
    height: 1fr;
}

.oc-field-label {
    color: #808080;
    margin-top: 1;
}

.oc-perm-body {
    background: $panel;
    border-left: thick #fab283;
    padding: 1 2;
    margin: 1 0;
}

.oc-btn-row {
    height: auto;
    margin-top: 1;
}

.oc-btn-row Button {
    margin-right: 1;
}
"""


class RbitApp(App[None]):
    """rbit — Next-Gen Full-Screen Terminal AI Coding Agent."""

    TITLE = "rbit"
    CSS = RBIT_TUI_CSS

    BINDINGS = [
        Binding("ctrl+p", "open_palette", "Commands", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=False),
        Binding("ctrl+m", "open_models", "Models", show=False),
        Binding("ctrl+t", "open_themes", "Themes", show=False),
        Binding("ctrl+o", "open_todos", "Todos", show=False),
        Binding("ctrl+n", "new_session", "New Session", show=False),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
        Binding("ctrl+y", "copy_last", "Copy", show=False),
        Binding("ctrl+f", "search_session", "Search", show=False),
        Binding("ctrl+shift+x", "stop_background", "Stop background", show=False),
        Binding("ctrl+shift+e", "collapse_all_cards", "Collapse cards", show=False),
        Binding("ctrl+r", "retry_last", "Retry", show=False),
        Binding("ctrl+h", "history_search", "Prompt history", show=False),
        Binding("f1", "show_help", "Help", show=False),
        Binding("escape", "interrupt_or_close", "Interrupt", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        workspace: Path | None = None,
        initial_session: Session | None = None,
        initial_prompt: str | None = None,
        initial_agent: str = "build",
        initial_model: str | None = None,
        initial_effort: str | None = None,
        allow_outside_paths: bool = False,
    ) -> None:
        super().__init__()
        self.workspace = (workspace or Path.cwd()).resolve()
        self.storage = StorageManager(self.workspace)
        self.config_data = self.storage.load_config()
        if initial_model:
            self.config_data["model"] = initial_model
        if initial_effort:
            self.config_data["effort"] = initial_effort

        self.session = initial_session or Session()
        self.agent_mode: str = initial_agent if initial_agent in ("build", "plan") else "build"
        self.initial_prompt = initial_prompt
        self.git_branch: str = detect_git_branch(self.workspace)
        self.prompt_history: list[str] = []
        self.history_cursor: int = -1
        self.is_busy: bool = False
        self.busy_status: str = ""
        self._busy_worker: Any = None
        self._queued_prompt: str | None = None
        self._auto_fix_round: bool = False
        self._spin_frame: int = 0
        self._has_inline_notices: bool = False
        self._sidebar_override_on_home: bool = False
        self._tool_widgets: dict[str, ToolCallCard] = {}
        self._save_timer: Timer | None = None
        self._live_card: AssistantMessageCard | None = None
        self._thinking: ThinkingIndicator | None = None
        self._reasoning_acc: str = ""
        self._interrupted_turn: bool = False
        self._history_window: int = 30
        self._last_error: str = ""

        self.executor = ToolExecutor(
            workspace=self.workspace,
            session=self.session,
            storage=self.storage,
            permission_cb=self._ask_permission,
            get_config=lambda: self.config_data,
            on_todos_updated=self._on_todos_updated,
            on_files_updated=self._on_files_updated,
        )
        if allow_outside_paths:
            self.executor.allow_outside_paths = True
        if self.config_data.get("readonly"):
            self.executor.session_readonly = True
        self.engine = AgentEngine(
            workspace=self.workspace,
            executor=self.executor,
            get_config=lambda: self.config_data,
            storage=self.storage,
        )
        self._pending_commit: str = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace-row"):
            with Vertical(id="center-stage", classes="is-home"):
                yield HomeLogo()
                yield SessionHeaderBar()
                yield ChatScroll(id="chat-scroll")
                with Vertical(id="prompt-outer"):
                    yield AutocompletePopup()
                    with Vertical(id="prompt-box", classes="mode-build"):
                        yield PromptInput(
                            placeholder="Ask anything...",
                            id="prompt-input",
                        )
                        with Horizontal(id="prompt-meta-row"):
                            yield AgentModeBadge(self.agent_mode)
                            yield PromptModelLabel(id="prompt-model-label")
                    yield PromptSubHints()
                yield HomeTipBanner()
            yield SidebarPanel(self.workspace)
        yield BottomStatusBar()

    def on_mount(self) -> None:
        acquired, other_pid = self.storage.acquire_lock()
        if not acquired:
            self.notify(
                f"Внимание: другой rbit уже работает в этом воркспейсе (pid {other_pid}).",
                severity="warning",
                timeout=5.0,
            )
        self._plugins_loaded = False
        self._maybe_show_doctor_hint()
        # Config-driven keybindings: {"bindings": {"open_models": "ctrl+shift+m"}}
        custom_bindings = self.config_data.get("bindings")
        if isinstance(custom_bindings, dict) and custom_bindings:
            merged = []
            for b in self.BINDINGS:
                new_key = custom_bindings.get(b.action)
                merged.append(Binding(new_key, b.action, b.description, show=b.show) if new_key else b)
            self.BINDINGS = merged
        from .plugins import load_plugins

        self._loaded_plugins = load_plugins(self)
        self.register_theme(RBIT_DARK)
        self.register_theme(RBIT_LIGHT)
        self.register_theme(RBIT_HIGH_CONTRAST)

        desired_theme = self.config_data.get("theme", "rbit-dark")
        if desired_theme in self.available_themes:
            self.theme = desired_theme
        else:
            self.theme = "rbit-dark"

        self.set_interval(0.09, self._tick_spinner)
        if not self.session.messages and self.initial_prompt is None:
            unfinished = next(
                (s for s in self.storage.list_sessions() if not s.finished and s.messages),
                None,
            )
            if unfinished is not None:
                self.session = unfinished
                self.executor.session = unfinished
                self.notify("Recovered an unfinished session — continue where you left off.", timeout=4.0)
        if self.session.messages:
            self._rebuild_chat_view()
        self._update_layout_mode()
        self._sync_ui()
        self.query_one("#prompt-input", Input).focus()
        self._discover_models()

        if self.initial_prompt:
            self.set_timer(0.15, lambda: self._submit_prompt(self.initial_prompt or ""))

    def _maybe_show_doctor_hint(self) -> None:
        hint_file = self.workspace / ".rbit" / ".doctor_hint_shown"
        if hint_file.exists():
            return
        has_key = bool((self.config_data.get("api_key") or "").strip())
        if has_key:
            return
        try:
            hint_file.parent.mkdir(parents=True, exist_ok=True)
            hint_file.write_text("1", encoding="utf-8")
        except OSError:
            pass
        self.notify(
            "API-ключ не задан — работает встроенный Smart-агент. "
            "Откройте /connect или проверьте `rbit doctor`.",
            timeout=6.0,
        )

    def on_unmount(self) -> None:
        self.storage.release_lock()
        if self._save_timer is None:
            return
        self._save_timer.stop()
        self._save_timer = None
        self._flush_save_session()

    @work
    async def _discover_models(self, force: bool = False) -> None:
        await self.engine.auto_discover_models(self.config_data, force=force)
        self._persist_provider_models()
        screen = self.screen
        if isinstance(screen, ModelSelectorModal):
            screen.refresh_models(self.engine.extra_models)
        self._sync_ui()

    def _persist_provider_models(self) -> None:
        models = self.engine.persistable_provider_models()
        if models:
            self.config_data["provider_models"] = models
            self.storage.save_config(self.config_data)

    def _discover_local_models(self) -> None:
        self._discover_models()

    def _tick_spinner(self) -> None:
        if self.is_busy:
            self._spin_frame += 1
            self.query_one(PromptSubHints).update_state(
                busy=True,
                status=self._busy_status_display,
                mode=self.agent_mode,
                frame=self._spin_frame,
            )
            if self._thinking is not None:
                self._thinking.tick(self._spin_frame)

    @property
    def _busy_status_display(self) -> str:
        if self._queued_prompt and self.is_busy:
            preview = self._queued_prompt.strip().replace("\n", " ")[:28]
            return f"{self.busy_status}  ·  в очереди: {preview}…".strip()
        return self.busy_status

    @property
    def _is_home_state(self) -> bool:
        return len(self.session.messages) == 0 and not self._has_inline_notices

    @property
    def _is_compact(self) -> bool:
        try:
            return self.size.width < 100
        except Exception:
            return False

    def _update_layout_mode(self) -> None:
        stage = self.query_one("#center-stage", Vertical)
        logo = self.query_one(HomeLogo)
        tip = self.query_one(HomeTipBanner)
        header = self.query_one(SessionHeaderBar)
        scroll = self.query_one("#chat-scroll", ChatScroll)
        sidebar = self.query_one(SidebarPanel)

        if self._is_home_state:
            stage.remove_class("is-session")
            stage.add_class("is-home")
            logo.display = True
            tip.display = True
            header.display = False
            scroll.display = False
            sidebar.display = self._sidebar_override_on_home and not self._is_compact
        else:
            stage.remove_class("is-home")
            stage.add_class("is-session")
            logo.display = False
            tip.display = False
            header.display = True
            scroll.display = True
            sidebar.display = bool(self.config_data.get("sidebar_visible", True)) and not self._is_compact

    def on_resize(self, event: events.Resize) -> None:
        self._update_layout_mode()

    def on_screen_suspend(self, event: events.ScreenSuspend) -> None:
        # A modal is coming up: the autocomplete popup would otherwise linger
        # behind the overlay with a stale file list.
        try:
            self.query_one(AutocompletePopup).display = False
        except NoMatches:
            pass

    @work
    async def action_open_file_diff(self, rel: str) -> None:
        """Shows the session's cumulative diff for a modified file (sidebar click)."""
        try:
            target = self.executor.resolve_path(rel)
        except Exception:
            self.notify(f"Cannot resolve path: {rel}", severity="warning")
            return
        target_key = str(target)
        snapshots = [s for s in self.executor.undo_stack if s.path == target_key]
        original = ""
        if snapshots:
            first = snapshots[0]
            if first.existed_before:
                original = first.previous_content
        try:
            current = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.notify(f"Cannot read {rel}: {exc}", severity="warning")
            return
        diff = "\n".join(
            difflib.unified_diff(
                original.splitlines(),
                current.splitlines(),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
        )
        self.push_screen(DiffViewerModal(rel, diff))

    def _sync_ui(self) -> None:
        model_id = self.config_data.get("model", "rbit/smart-local")
        api_ok = bool((self.config_data.get("api_key") or "").strip())
        cp_count = len(self.storage.list_checkpoints())

        self._update_layout_mode()

        pbox = self.query_one("#prompt-box", Vertical)
        pbox.remove_class("mode-build", "mode-plan")
        pbox.add_class(f"mode-{self.agent_mode}")

        self.query_one(AgentModeBadge).set_mode(self.agent_mode)
        self.query_one(PromptModelLabel).set_model(
            model_id,
            self.engine.extra_models,
            effort=str(self.config_data.get("effort", "auto")),
        )
        self.query_one(PromptSubHints).update_state(
            busy=self.is_busy,
            status=self._busy_status_display,
            mode=self.agent_mode,
            frame=self._spin_frame,
        )
        self.query_one(SessionHeaderBar).update_header(
            self.session, model_id, self.engine.extra_models, api_latency=self.engine.last_api_latency
        )
        self.query_one(SidebarPanel).refresh_all(
            self.session,
            model_id,
            api_ok,
            checkpoints_count=cp_count,
            extra_models=self.engine.extra_models,
            ollama_online=self.engine.ollama_online,
        )
        self.query_one(BottomStatusBar).update_bar(self.workspace, self.git_branch)

    def _on_todos_updated(self, todos: list[TodoItem]) -> None:
        """Keeps sidebar + modal in sync when the agent rewrites the todo list."""
        self.session.todos = todos
        if todos:
            self._sidebar_override_on_home = True
        self._sync_ui()

    def _on_files_updated(self) -> None:
        self._sync_ui()

    async def _ask_permission(self, tool_name: str, detail: str) -> bool:
        choice = await self.push_screen_wait(PermissionModal(tool_name, detail))
        if choice == "always":
            self.executor.always_allow_bash = True
            return True
        return choice == "once"

    # --- Key Handling ---
    def on_key(self, event: events.Key) -> None:
        if len(self.screen_stack) > 1:
            return

        popup = self.query_one(AutocompletePopup)
        inp = self.query_one("#prompt-input", Input)

        if event.key == "tab":
            event.prevent_default()
            event.stop()
            if popup.display:
                accepted = popup.accept_highlighted()
                if accepted:
                    self._apply_completion(*accepted)
                return
            self.action_toggle_agent()
            return

        if popup.display:
            if event.key == "down":
                event.prevent_default()
                event.stop()
                popup.action_cursor_down()
                return
            if event.key == "up":
                event.prevent_default()
                event.stop()
                popup.action_cursor_up()
                return

        if inp.has_focus and not inp.value and self.prompt_history:
            if event.key == "up":
                event.prevent_default()
                self.history_cursor = min(len(self.prompt_history) - 1, self.history_cursor + 1)
                inp.value = self.prompt_history[-(self.history_cursor + 1)]
                inp.cursor_position = len(inp.value)
            elif event.key == "down" and self.history_cursor >= 0:
                event.prevent_default()
                self.history_cursor -= 1
                inp.value = self.prompt_history[-(self.history_cursor + 1)] if self.history_cursor >= 0 else ""
                inp.cursor_position = len(inp.value)

    @on(AgentModeBadge.Toggled)
    def _on_badge_clicked(self) -> None:
        self.action_toggle_agent()

    @on(PromptModelLabel.Clicked)
    def _on_model_label_clicked(self) -> None:
        self.action_open_models()

    @on(ToolCallCard.CopyRequested)
    def _on_card_copy_requested(self, event: ToolCallCard.CopyRequested) -> None:
        if _write_clipboard(event.text):
            self.notify(f"Copied: {event.text[:60]}", timeout=2.0)
        else:
            self.notify("Clipboard helper not found.", severity="error", timeout=2.0)

    @on(ToolCallCard.StopRequested)
    async def _on_stop_background_requested(self, event: ToolCallCard.StopRequested) -> None:
        killed = await self.executor.terminate_background_pid(event.pid)
        for card in self._tool_widgets.values():
            if card.record.pid == event.pid:
                card.mark_stopped()
        if killed:
            self.notify(f"Stopped background process {event.pid}.", timeout=2.0)
        else:
            self.notify(f"Background process {event.pid} is already gone.", severity="warning", timeout=2.0)

    def action_toggle_agent(self) -> None:
        self.agent_mode = "plan" if self.agent_mode == "build" else "build"
        self._sync_ui()
        self.query_one("#prompt-input", Input).focus()

    def action_toggle_sidebar(self) -> None:
        if self._is_home_state:
            self._sidebar_override_on_home = not self._sidebar_override_on_home
        else:
            cur = bool(self.config_data.get("sidebar_visible", True))
            self.config_data["sidebar_visible"] = not cur
            self.storage.save_config(self.config_data)
        self._sync_ui()

    def action_interrupt_or_close(self) -> None:
        popup = self.query_one(AutocompletePopup)
        if popup.display:
            popup.display = False
            return
        if self.is_busy:
            # Cancel only the tracked busy worker (agent turn, compact, bang) —
            # cancel_all() would also kill model discovery and other side tasks.
            if self._busy_worker is not None:
                self._busy_worker.cancel()
            else:
                self.workers.cancel_all()
            self._interrupted_turn = self._finalize_interrupted()
            self.is_busy = False
            self.busy_status = ""
            self._queued_prompt = None  # interrupt means stop everything
            self._post_notice("Interrupted by user (esc).")
            self._sync_ui()

    # --- Autocomplete ---
    @on(Input.Changed, "#prompt-input")
    def _on_prompt_changed(self, event: Input.Changed) -> None:
        val = event.value
        popup = self.query_one(AutocompletePopup)

        if val.startswith("/") and " " not in val:
            extra = [
                (f"/{name}", str(meta.get("description", "")))
                for name, meta in self.storage.load_custom_commands().items()
            ]
            popup.show_slash(val, extra)
            return

        at_match = re.search(r"@([^\s]*)$", val)
        if at_match:
            query = at_match.group(1)
            files = self.executor.list_workspace_files(limit=120)
            popup.show_files(query, files)
            return

        popup.display = False

    @on(OptionList.OptionSelected, "#autocomplete-popup")
    def _on_popup_clicked(self, event: OptionList.OptionSelected) -> None:
        popup = self.query_one(AutocompletePopup)
        if event.option_id:
            self._apply_completion(popup.current_kind, event.option_id)
            popup.display = False

    def _apply_completion(self, kind: str, value: str) -> None:
        inp = self.query_one("#prompt-input", Input)
        if kind == "slash":
            inp.value = value + " "
        elif kind == "file":
            value = value.replace("\\", "/")
            # lambda replacement: a plain template string would interpret
            # control sequences like \t or \n inside Windows paths.
            inp.value = re.sub(r"@([^\s]*)$", lambda _m: f"@{value} ", inp.value, count=1)
        inp.cursor_position = len(inp.value)
        inp.focus()

    # --- Modals & Actions ---
    @work
    async def action_open_palette(self) -> None:
        act = await self.push_screen_wait(
            CommandPaletteModal(self.config_data.get("palette_recents", []))
        )
        if not act:
            self.query_one("#prompt-input", Input).focus()
            return
        self._record_palette_use(act)
        dispatch = {
            "models": self.action_open_models,
            "theme": self.action_open_themes,
        "effort": self.action_open_effort,
            "todo": self.action_open_todos,
            "checkpoint": lambda: self._cmd_checkpoint(""),
            "rewind": self.action_open_checkpoints,
            "export": lambda: self._cmd_export(""),
            "doctor": self._cmd_doctor,
            "toggle_agent": self.action_toggle_agent,
            "toggle_sidebar": self.action_toggle_sidebar,
            "stop_background": self.action_stop_background,
            "new_session": self.action_new_session,
            "sessions": self.action_open_sessions,
            "undo": self._cmd_undo,
            "redo": self._cmd_redo,
            "diff": self._cmd_diff,
            "compact": self._cmd_compact,
            "retry": self._cmd_retry,
            "fork": lambda: self._cmd_fork(""),
            "commit": lambda: self._cmd_commit(""),
            "copy": self.action_copy_last,
            "search": self.action_search_session,
            "init": lambda: self._submit_prompt("Проанализируй проект и создай подробный Todo-план"),
            "connect": self.action_open_connect,
            "clear": self.action_clear_chat,
            "help": self._cmd_help,
            "exit": self.exit,
        }
        fn = dispatch.get(act)
        if fn:
            fn()

    def _record_palette_use(self, act_id: str, keep: int = 5) -> None:
        recents = [act_id] + [
            item for item in self.config_data.get("palette_recents", []) if item != act_id
        ]
        self.config_data["palette_recents"] = recents[:keep]
        self.storage.save_config(self.config_data)

    @work
    async def action_open_models(self) -> None:
        self.engine.add_provider_models(self.engine.load_cached_provider_models(self.config_data))
        self._discover_models(force=True)
        chosen = await self.push_screen_wait(
            ModelSelectorModal(
                self.config_data.get("model", "rbit/smart-local"),
                extra_models=self.engine.extra_models,
            )
        )
        if chosen:
            self.config_data["model"] = chosen
            self.storage.save_config(self.config_data)
            self._sync_ui()
        self.query_one("#prompt-input", Input).focus()

    def _set_effort(self, value: str) -> None:
        effort = value.strip().lower()
        if effort not in ("auto", "low", "medium", "high"):
            self.notify("Effort must be one of: auto, low, medium, high", severity="warning")
            return
        self.config_data["effort"] = effort
        self.storage.save_config(self.config_data)
        self._sync_ui()
        self.notify(f"Reasoning effort: {effort}", timeout=2.0)

    @work
    async def action_open_effort(self) -> None:
        model_id = self.config_data.get("model", "rbit/smart-local")
        chosen = await self.push_screen_wait(
            EffortSelectorModal(self.config_data.get("effort", "auto"), model_id)
        )
        if chosen:
            self._set_effort(chosen)
        self.query_one("#prompt-input", Input).focus()

    @work
    async def action_open_themes(self) -> None:
        chosen = await self.push_screen_wait(ThemeSelectorModal(self.theme))
        if chosen and chosen in self.available_themes:
            self.theme = chosen
            self.config_data["theme"] = chosen
            self.storage.save_config(self.config_data)
            self._sync_ui()
        self.query_one("#prompt-input", Input).focus()

    @work
    async def action_open_todos(self) -> None:
        if self.is_busy:
            self.notify("Agent is busy — press Esc first.", severity="warning")
            return
        updated = await self.push_screen_wait(TodoManagerModal(self.session.todos))
        if updated is not None:
            self.session.todos = updated
            if self._is_home_state and updated:
                self._sidebar_override_on_home = True
            self.storage.save_session(self.session)
            self._sync_ui()
        self.query_one("#prompt-input", Input).focus()

    @work
    async def action_open_checkpoints(self) -> None:
        cps = self.storage.list_checkpoints()
        res = await self.push_screen_wait(CheckpointModal(cps))
        if res:
            action, payload = res
            if action == "create":
                self._cmd_checkpoint(payload)
            elif action == "restore":
                msg = self.executor.restore_checkpoint(payload)
                self._post_notice(f"↺ {msg}")
                self._sync_ui()
        self.query_one("#prompt-input", Input).focus()

    @work
    async def action_open_sessions(self) -> None:
        sessions = self.storage.list_sessions()
        res = await self.push_screen_wait(SessionPickerModal(sessions, self.session.id))
        if not res:
            self.query_one("#prompt-input", Input).focus()
            return
        action, sid = res
        if action == "new":
            self.action_new_session()
        elif action == "delete":
            self.storage.delete_session(sid)
            if sid == self.session.id:
                self.action_new_session()
        elif action == "switch":
            for s in sessions:
                if s.id == sid:
                    self._load_session_into_ui(s)
                    break
        self.query_one("#prompt-input", Input).focus()

    @work
    async def action_open_connect(self) -> None:
        res = await self.push_screen_wait(
            ConnectApiModal(
                api_base=self.config_data.get("api_base", "https://openrouter.ai/api/v1"),
                api_key=self.config_data.get("api_key", ""),
            )
        )
        if res is not None:
            self.config_data["api_base"] = res["api_base"]
            self.config_data["api_key"] = res["api_key"]
            self.storage.save_config(self.config_data)
            if self.config_data["api_key"]:
                ok, message, models = await self.engine.fetch_provider_models(
                    self.config_data["api_base"], self.config_data["api_key"], timeout=15.0
                )
                if ok and models:
                    added = self.engine.add_provider_models(models)
                    self._persist_provider_models()
                    provider = models[0].provider
                    message = f"{message} ({added} added to {provider})"
                self.notify(("✓ " if ok else "✗ ") + message, timeout=5.0)
            await self.engine.auto_discover_models(self.config_data, force=True)
            self._persist_provider_models()
            self._sync_ui()
        self.query_one("#prompt-input", Input).focus()

    def action_new_session(self) -> None:
        if self.is_busy:
            self.notify("Agent is busy — press Esc first.", severity="warning")
            return
        self._queued_prompt = None
        self.session = Session()
        self.executor.session = self.session
        self._history_window = 30
        self._has_inline_notices = False
        self._sidebar_override_on_home = False
        self.query_one(HomeTipBanner).pick_new_tip()
        self._rebuild_chat_view()
        self._sync_ui()
        self.query_one("#prompt-input", Input).focus()

    def action_clear_chat(self) -> None:
        if self.is_busy:
            self.notify("Agent is busy — press Esc first.", severity="warning")
            return
        self.session.messages.clear()
        self._history_window = 30
        self._has_inline_notices = False
        self._rebuild_chat_view()
        self._sync_ui()
        self.query_one("#prompt-input", Input).focus()

    @work
    async def action_search_session(self, initial_query: str = "") -> None:
        target = await self.push_screen_wait(SearchResultsModal(self.session, initial_query))
        if target:
            self._jump_to_message(target)
        self.query_one("#prompt-input", Input).focus()

    def _jump_to_message(self, message_id: str) -> None:
        for idx, m in enumerate(self.session.messages):
            if m.id != message_id:
                continue
            for card in self.query_one("#chat-scroll", ChatScroll).children:
                target = getattr(card, "msg", None)
                if target is not None and getattr(target, "id", None) == message_id:
                    card.scroll_visible(animate=False)
                    self.notify(f"Jumped to message #{idx + 1}.", timeout=2.0)
                    return
            return

    def action_copy_last(self) -> None:
        last = next(
            (m for m in reversed(self.session.messages) if m.role == "assistant" and m.content.strip()),
            None,
        )
        if last is None:
            self.notify("Nothing to copy yet — no assistant answer in this session.", severity="warning")
            return
        if _write_clipboard(last.content):
            self.notify("Copied the last answer to the clipboard.", timeout=2.0)
        else:
            self.notify(
                "Clipboard helper not found (install pyperclip or xclip/wl-copy).",
                severity="error",
                timeout=3.0,
            )

    def action_stop_background(self) -> None:
        pids = self.executor.background_pids()
        if not pids:
            self.notify("No background processes are running.", timeout=2.0)
            return
        self._stop_all_background(pids)

    @work
    async def _stop_all_background(self, pids: list[int]) -> None:
        for pid in pids:
            await self.executor.terminate_background_pid(pid)
        for card in self._tool_widgets.values():
            if card.record.pid in pids:
                card.mark_stopped()
        self.notify(f"Stopped {len(pids)} background process{'es' if len(pids) != 1 else ''}.", timeout=2.0)

    def _load_session_into_ui(self, sess: Session) -> None:
        if self.is_busy:
            self.notify("Agent is busy — press Esc first.", severity="warning")
            return
        self._queued_prompt = None
        self.session = sess
        self.executor.session = sess
        self._history_window = 30
        self._has_inline_notices = False
        self._rebuild_chat_view()
        self._sync_ui()

    def _rebuild_chat_view(self, limit: int | None = None) -> None:
        scroll = self.query_one("#chat-scroll", ChatScroll)
        scroll.remove_children()
        self._tool_widgets.clear()
        # Live widgets were just detached; drop the stale references so an
        # in-flight turn re-creates its card instead of updating a ghost.
        self._live_card = None
        self._thinking = None
        window = self._history_window if limit is None else limit
        messages = self.session.messages
        truncated = len(messages) > window
        visible = messages[-window:] if truncated else messages
        if truncated:
            pinned_early = [m for m in messages[:-window] if m.pinned]
            if pinned_early:
                visible = [*pinned_early, *visible]
        widgets: list[Any] = []
        if truncated:
            widgets.append(
                Static("↑ Load older messages", id="load-older", classes="system-notice")
            )
        for m in visible:
            if m.role == "user":
                widgets.append(UserMessageCard(m))
            elif m.role == "assistant":
                for tc in m.tool_calls:
                    widgets.append(ToolCallCard(tc))
                widgets.append(AssistantMessageCard(m, self.engine.extra_models))
        if widgets:
            scroll.mount_all(widgets)
        scroll.scroll_end(animate=False)

    def _load_older(self) -> None:
        self._history_window += 30
        self._rebuild_chat_view()
        self.notify("Loaded older messages.", timeout=2.0)

    def on_click(self, event: events.Click) -> None:
        if getattr(event.widget, "id", None) == "load-older":
            self._load_older()

    def _post_notice(self, text: str) -> None:
        self._has_inline_notices = True
        self._update_layout_mode()
        scroll = self.query_one("#chat-scroll", ChatScroll)
        scroll.mount(Static(text, classes="system-notice"))
        self._smart_scroll_end()

    async def _auto_title(self, session: Session, first_user_text: str) -> None:
        derived = " ".join(first_user_text.split())[:40] or "New Session"
        title = await self.engine.generate_title(first_user_text) or derived
        session.title = title
        self.storage.save_session(session)
        self._sync_ui()

    def _maybe_auto_compact(self, model_id: str) -> None:
        if not self.config_data.get("auto_compact", True) or self.is_busy:
            return
        if len(self.session.messages) < 8:
            return
        from rbit.models import get_model_info

        info = get_model_info(model_id, self.engine.extra_models)
        threshold = int(self.config_data.get("auto_compact_at", 85))
        if not info.context_window:
            return
        pct = self.session.total_tokens / info.context_window * 100
        if pct >= threshold:
            self.notify(f"Контекст {pct:.0f}% — авто-компакция…", timeout=3.0)
            self._busy_worker = self._compact_context()

    def _cmd_mcp(self, arg: str) -> None:
        """Manages configured MCP server entries in config (launching is a future step)."""
        servers = self.config_data.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = {}
        parts = arg.split(maxsplit=2)
        if parts and parts[0] in ("add", "remove") :
            name = parts[1].strip() if len(parts) > 1 else ""
            if not name:
                self.notify("Usage: /mcp add <name> <command…> | /mcp remove <name>", severity="warning")
                return
            if parts[0] == "remove":
                servers.pop(name, None)
                self.notify(f"MCP server removed: {name}", timeout=2.0)
            else:
                servers[name] = {"command": parts[2] if len(parts) > 2 else ""}
                self.notify(f"MCP server added: {name} (config saved)", timeout=2.0)
            self.config_data["mcp_servers"] = servers
            self.storage.save_config(self.config_data)
            self._cmd_mcp("")
            return
        lines = ["[bold #eeeeee]MCP servers[/]", ""]
        if servers:
            for name, entry in servers.items():
                cmd_text = entry.get("command", "") if isinstance(entry, dict) else str(entry)
                lines.append(f"  • [bold]{name}[/] — {cmd_text}")
        else:
            lines.append("  (none configured — /mcp add <name> <command>)")
        lines.append("")
        lines.append("[#808080]Запуск серверов пока не подключён — конфигурация сохраняется для будущих версий.[/]")
        self._post_notice("\n".join(lines))

    def action_history_search(self) -> None:
        self.push_screen(PromptHistoryModal(self.prompt_history), self._on_history_picked)

    def _on_history_picked(self, prompt: str | None) -> None:
        if prompt:
            inp = self.query_one("#prompt-input", Input)
            inp.value = prompt
            inp.cursor_position = len(prompt)
            inp.focus()

    def _cmd_pin(self) -> None:
        for msg in reversed(self.session.messages):
            if msg.role == "assistant":
                msg.pinned = not msg.pinned
                self.storage.save_session(self.session)
                self._rebuild_chat_view()
                self.notify("📌 Pinned." if msg.pinned else "Unpinned.", timeout=1.5)
                return
        self.notify("No assistant message to pin.", severity="warning")

    def _cmd_undo_turn(self) -> None:
        n = self.executor.undo_to_count(getattr(self, "_turn_undo_mark", 0))
        self.notify(f"Undone {n} change(s) from this turn." if n else "Nothing to undo.", timeout=2.0)
        self._sync_ui()

    async def _cmd_test(self, path: str) -> None:
        cmd = path.strip() or detect_test_command(self.workspace)
        if not cmd:
            self.notify("No test command detected; use /test <command>.", severity="warning")
            return
        self.notify(f"Running: {cmd}", timeout=2.0)
        res = await self.executor.execute("bash", {"command": cmd, "timeout": 300})
        tail = "\n".join(res.output.splitlines()[-25:]) or "(no output)"
        if res.ok:
            self._post_notice(f"✓ Tests passed\n\n{tail}")
        else:
            self._post_notice(f"✗ Tests failed — отправляю падения агенту\n\n{tail}")
            self._submit_prompt(
                "[test failures] Тесты упали. Вот вывод:\n\n" + tail + "\n\nНайди причину и исправь."
            )

    def _cmd_review(self) -> None:
        self.agent_mode = "plan"
        self._submit_prompt(
            "Проведи код-ревью текущих изменений. @diff\n"
            "Сфокусируйся на корректности, безопасности, производительности и читаемости. "
            "Приоритизируй находки: [P0] блокеры, [P1] важно, [P2] полировка. Отвечай по-русски."
        )

    def _cmd_explain(self, path: str) -> None:
        path = path.strip()
        if not path:
            self.notify("Usage: /explain <file>", severity="warning")
            return
        self._submit_prompt(
            f"Объясни файл `{path}`: назначение, ключевые функции/классы, связи с остальным кодом. @ {path}"
        )

    def _cmd_deps(self) -> None:
        graph = self.executor.import_graph()
        lines = [f"`{k}` → {', '.join(sorted(v)) or '(no local imports)'}" for k, v in list(graph.items())[:30]]
        self._post_notice("# Import graph\n\n" + "\n".join(lines))

    def _get_editor(self) -> list[str]:
        """Editor command: $VISUAL/$EDITOR → config `editor` → platform default."""
        import os as _os
        import shlex

        cfg_editor = str(self.config_data.get("editor") or "").strip()
        for candidate in (_os.environ.get("VISUAL"), _os.environ.get("EDITOR"), cfg_editor):
            if candidate and candidate.strip():
                return shlex.split(candidate)
        return ["notepad"] if _os.name == "nt" else ["vi"]

    async def _cmd_edit(self, path: str) -> None:
        """/edit — open the prompt buffer (or a file) in an external editor."""
        editor = self._get_editor()
        editing_prompt = not path.strip()
        if editing_prompt:
            tmp = self.workspace / ".rbit" / "prompt-edit.md"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            current = self.query_one("#prompt-input", Input).value
            tmp.write_text(current, encoding="utf-8")
            target = tmp
        else:
            try:
                target = self.executor.resolve_path(path.strip())
            except Exception:
                self.notify(f"Cannot resolve path: {path}", severity="warning")
                return
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("", encoding="utf-8")
        try:
            with self.suspend():
                await asyncio.to_thread(
                    subprocess.run, [*editor, str(target)], check=False
                )
        except Exception as exc:
            self.notify(f"Editor failed: {exc}", severity="error")
            return
        if editing_prompt:
            new_text = target.read_text(encoding="utf-8", errors="replace").strip()
            inp = self.query_one("#prompt-input", Input)
            inp.value = new_text
            inp.cursor_position = len(new_text)
            inp.focus()
            if new_text:
                self.notify("Промпт подставлен — Enter для отправки.", timeout=2.0)
        else:
            self.notify(f"Saved: {self.executor.rel_display(target)}", timeout=2.0)
        self._sync_ui()

    async def _cmd_fix(self, command: str) -> None:
        """/fix <cmd> — run a command; when it fails, feed the output to the agent."""
        command = command.strip()
        if not command:
            self.notify("Usage: /fix <command>", severity="warning")
            return
        self.notify(f"Running: {command[:60]}", timeout=2.0)
        res = await self.executor.execute("bash", {"command": command, "timeout": 300})
        tail = "\n".join(res.output.splitlines()[-30:]) or "(no output)"
        if res.ok:
            self._post_notice(f"✓ Command succeeded — nothing to fix\n\n{tail}")
            return
        self._post_notice(f"✗ Command failed — отправляю вывод агенту\n\n{tail}")
        self._submit_prompt(
            f"[fix] Команда `{command}` упала с выводом:\n\n{tail}\n\nНайди причину и исправь."
        )

    async def _cmd_macro(self, name: str) -> None:
        import json as _json

        path = self.workspace / ".rbit" / "macros" / f"{name.strip()}.json"
        if not name.strip() or not path.exists():
            self.notify(f"Macro not found: {path}", severity="warning")
            return
        try:
            steps = _json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            self.notify(f"Bad macro JSON: {exc}", severity="error")
            return
        for step in steps if isinstance(steps, list) else []:
            tool = str(step.get("tool") or "")
            if not tool:
                continue
            res = await self.executor.execute(tool, step.get("args") or {})
            self._post_notice(f"▸ {tool}: {res.summary}\n\n{res.output[:1200]}")

    def _run_coro(self, coro) -> None:
        self.run_worker(coro, exclusive=False)

    def _os_notify(self, text: str) -> None:
        try:
            if os.name == "nt":
                try:
                    import winsound

                    winsound.MessageBeep()
                except Exception:
                    pass
            elif sys.platform == "darwin":
                subprocess.Popen(
                    ["osascript", "-e", f'display notification "{text}" with title "rbit"'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    ["notify-send", "rbit", text],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    @staticmethod
    def _phrase_is_build(lowered: str) -> bool:
        return lowered.startswith(("начни делать", "реализуй", "исправь", "делай", "fix:", "implement:"))

    def _build_self_check_message(self, reply: ChatMessage) -> ChatMessage | None:
        """Runs diagnostics on files mutated this turn; returns a fix prompt when broken."""
        mutated: list[str] = []
        seen: set[str] = set()
        for tc in reply.tool_calls:
            if tc.name not in MUTATING_TOOLS:
                continue
            args = tc.arguments or {}
            raw = args.get("filePath") or args.get("path")
            if not raw:
                continue
            try:
                rel = self.executor.rel_display(self.executor.resolve_path(str(raw)))
            except Exception:
                continue
            if rel not in seen:
                seen.add(rel)
                mutated.append(rel)
        if not mutated:
            return None
        problems: list[str] = []
        for rel in mutated:
            res = self.executor._tool_diagnostics({"filePath": rel})
            m = re.search(r"(\d+) issues", (res.summary or "").lower())
            if m and int(m.group(1)) > 0:
                problems.append(f"`{rel}` — {res.output[:400]}")
        if not problems:
            return None
        self._post_notice(
            "⚠ Self-check: проблемы в изменённых файлах — запускаю авто-фикс (1 круг):\n\n"
            + "\n\n".join(problems)
        )
        return ChatMessage(
            id=short_id(),
            role="user",
            content=(
                "[auto] Self-check found problems in the files you just changed:\n\n"
                + "\n\n".join(problems)
                + "\n\nFix them. Do not touch unrelated files."
            ),
            agent=self.agent_mode,
        )

    def _show_thinking(self) -> None:
        if self._thinking is not None:
            return
        self._reasoning_acc = ""
        self._thinking = ThinkingIndicator()
        self.query_one("#chat-scroll", ChatScroll).mount(self._thinking)
        self._smart_scroll_end()

    def _settle_thinking(self) -> None:
        """Stops the spinner; keeps a collapsed 'Thought for Ns' line if reasoning arrived."""
        widget = self._thinking
        self._thinking = None
        self._reasoning_acc = ""
        if widget is None:
            return
        if widget.has_reasoning:
            widget.finish()
            self._smart_scroll_end()
            return
        try:
            widget.remove()
        except Exception:
            pass

    def _hide_thinking(self) -> None:
        widget = self._thinking
        self._thinking = None
        if widget is None:
            return
        try:
            widget.remove()
        except Exception:
            pass

    def _smart_scroll_end(self) -> None:
        """Follows new output only while the user is already at the bottom."""
        self.query_one("#chat-scroll", ChatScroll).follow_end()

    def _finalize_interrupted(self) -> bool:
        """Persists the partial streamed answer with an `interrupted` marker."""
        card = self._live_card
        self._live_card = None
        if card is None or not card.msg.content.strip():
            return False
        pending = card.msg
        pending.interrupted = True
        self.session.messages.append(pending)
        try:
            card.query_one(".assistant-meta", Static).update(card._build_meta_text())
        except Exception:
            pass
        self._schedule_save_session()
        return True

    def _schedule_save_session(self, delay: float = 1.2) -> None:
        """Debounces session writes so streaming chunks do not rewrite disk constantly."""
        if self._save_timer is not None:
            self._save_timer.stop()
        self._save_timer = self.set_timer(max(0.01, delay), self._flush_save_session)

    def _flush_save_session(self) -> None:
        self._save_timer = None
        try:
            self.storage.save_session(self.session)
        except Exception as exc:
            self.notify(f"Не удалось сохранить сессию: {exc}", severity="error", timeout=4.0)

    def _toggle_last_card(self) -> None:
        scroll = self.query_one("#chat-scroll", ChatScroll)
        cards = list(scroll.query(ToolCallCard))
        if cards:
            cards[-1].expanded = not cards[-1].expanded

    def action_collapse_all_cards(self) -> None:
        scroll = self.query_one("#chat-scroll", ChatScroll)
        collapsed = 0
        for card in scroll.query(ToolCallCard):
            if card.expanded:
                card.expanded = False
                collapsed += 1
        self.notify(f"Collapsed {collapsed} tool card{'s' if collapsed != 1 else ''}.", timeout=2.0)

    # --- Slash Command Router ---
    def _handle_slash_command(self, raw: str) -> bool:
        parts = raw.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Custom project commands (from .rbit/commands) take priority over built-ins.
        custom = self.storage.load_custom_commands()
        custom_name = cmd.lstrip("/")
        if custom_name in custom:
            template = custom[custom_name]["template"]
            prompt = template.replace("{args}", arg) if "{args}" in template else (
                template + (f"\n\n{arg}" if arg else "")
            )
            self._submit_prompt(prompt.strip())
            return True

        if cmd in ("/exit", "/quit", "/q"):
            self.exit()
            return True
        if cmd in ("/models", "/model"):
            self.action_open_models()
            return True
        if cmd in ("/theme", "/themes"):
            self.action_open_themes()
            return True
        if cmd == "/effort":
            if arg:
                self._set_effort(arg)
            else:
                self.action_open_effort()
            return True
        if cmd in ("/sessions", "/session", "/resume"):
            self.action_open_sessions()
            return True
        if cmd == "/new":
            self.action_new_session()
            return True
        if cmd == "/clear":
            self.action_clear_chat()
            return True
        if cmd == "/connect":
            self.action_open_connect()
            return True
        if cmd == "/checkpoint":
            self._cmd_checkpoint(arg)
            return True
        if cmd == "/rewind":
            self.action_open_checkpoints()
            return True
        if cmd == "/remember":
            self._cmd_remember(arg)
            return True
        if cmd == "/edit":
            self._run_coro(self._cmd_edit(arg))
            return True
        if cmd == "/fix":
            self._run_coro(self._cmd_fix(arg))
            return True
        if cmd == "/export":
            self._cmd_export(arg)
            return True
        if cmd == "/doctor":
            self._cmd_doctor()
            return True
        if cmd in ("/todo", "/todos"):
            if arg:
                self.session.todos.append(TodoItem(id=short_id(), content=arg, status="pending", priority="medium"))
                if self._is_home_state:
                    self._sidebar_override_on_home = True
                self.storage.save_session(self.session)
                self._sync_ui()
                self.notify(f"Todo added: {arg}", timeout=2.0)
            else:
                self.action_open_todos()
            return True
        if cmd == "/pin":
            self._cmd_pin()
            return True
        if cmd == "/test":
            self._run_coro(self._cmd_test(arg))
            return True
        if cmd == "/review":
            self._cmd_review()
            return True
        if cmd == "/explain":
            self._cmd_explain(arg)
            return True
        if cmd == "/deps":
            self._cmd_deps()
            return True
        if cmd in ("/mcp", "/mcp-servers"):
            self._cmd_mcp(arg)
            return True
        if cmd == "/macro":
            self._run_coro(self._cmd_macro(arg))
            return True
        if cmd == "/undoturn":
            self._cmd_undo_turn()
            return True
        if cmd in ("/history", "/hist"):
            self.action_history_search()
            return True
        if cmd == "/undo":
            self._cmd_undo()
            return True
        if cmd == "/redo":
            self._cmd_redo()
            return True
        if cmd == "/diff":
            self._cmd_diff()
            return True
        if cmd == "/compact":
            self._cmd_compact()
            return True
        if cmd == "/retry":
            self._cmd_retry()
            return True
        if cmd == "/fork":
            self._cmd_fork(arg)
            return True
        if cmd == "/commit":
            self._cmd_commit(arg)
            return True
        if cmd == "/search":
            self.action_search_session(arg)
            return True
        if cmd == "/copy":
            self.action_copy_last()
            return True
        if cmd == "/init":
            self._submit_prompt("Проанализируй структуру этого проекта и создай стартовый Todo-план")
            return True
        if cmd == "/help":
            self._cmd_help()
            return True


    def _cmd_checkpoint(self, label: str) -> None:
        meta = self.executor.create_checkpoint(label or f"Step {len(self.session.messages) + 1}")
        note = f"✓ Saved checkpoint `{meta.label}` ({meta.file_count} files, id `{meta.id}`). Use `/rewind` anytime."
        if self._is_home_state:
            self.notify(note, timeout=3.0)
        else:
            self._post_notice(note)
        self._sync_ui()

    def _cmd_remember(self, rule: str) -> None:
        if not rule:
            mem = self.storage.load_project_memory()
            if mem:
                self._post_notice(f"# Project Memory (.rbit/memory.md)\n{mem}")
            else:
                self.notify("Usage: /remember <persistent rule for this project>", timeout=3.0)
            return
        path = self.storage.append_memory_rule(rule)
        msg = f"✓ Saved project rule to `{self.executor.rel_display(path)}`: {rule}"
        if self._is_home_state:
            self.notify(msg, timeout=3.0)
        else:
            self._post_notice(msg)

    def _cmd_export(self, arg: str) -> None:
        out_path = (self.workspace / arg) if arg else None
        saved = self.storage.export_session_markdown(self.session, out_path)
        rel = self.executor.rel_display(saved)
        msg = f"✓ Exported session Markdown to `{rel}`"
        if self._is_home_state:
            self.notify(msg, timeout=3.0)
        else:
            self._post_notice(msg)

    def _cmd_doctor(self) -> None:
        api_ok = "Configured" if (self.config_data.get("api_key") or "").strip() else "Not set (using rbit Smart Agent)"
        ollama_cnt = len(self.engine.extra_models)
        cp_cnt = len(self.storage.list_checkpoints())
        mem_ok = "Present" if self.storage.memory_path.exists() else "Empty"
        report = (
            f"# rbit Doctor v{__version__}\n"
            f"  • Python / OS   : {sys.version.split()[0]} ({platform.system()} {platform.release()})\n"
            f"  • Workspace     : {self.workspace}\n"
            f"  • Git Branch    : {self.git_branch or '(not a git repo)'}\n"
            f"  • Active Model  : {self.config_data.get('model')}\n"
            f"  • Remote API    : {api_ok} ({self.config_data.get('api_base')})\n"
            f"  • Local Ollama  : {ollama_cnt} local models detected\n"
            f"  • Checkpoints   : {cp_cnt} snapshots · Memory: {mem_ok}"
        )
        self._post_notice(report)

    def _cmd_undo(self) -> None:
        msg = self.executor.undo_last()
        self.storage.save_session(self.session)
        if self._is_home_state:
            self.notify(msg, timeout=2.5)
        else:
            self._post_notice(f"↺ {msg}")
        self._sync_ui()

    def _cmd_redo(self) -> None:
        msg = self.executor.redo_last()
        self.storage.save_session(self.session)
        if self._is_home_state:
            self.notify(msg, timeout=2.5)
        else:
            self._post_notice(f"↻ {msg}")
        self._sync_ui()

    def _cmd_diff(self) -> None:
        if not self.session.modified_files:
            self._post_notice("No modified files in the current session.")
            return
        lines = ["# Modified Files:"]
        for rel, st in self.session.modified_files.items():
            lines.append(f"  • {rel}  (+{st.added} / -{st.removed})")
        self._post_notice("\n".join(lines))

    def _cmd_compact(self) -> None:
        if self.is_busy:
            self.notify("Agent is busy — press Esc first.", severity="warning")
            return
        if len(self.session.messages) <= 2:
            self.notify("Context is already compact.", timeout=2.0)
            return
        self._busy_worker = self._compact_context()

    @work(exclusive=True, group="rbit-compact")
    async def _compact_context(self) -> None:
        count = len(self.session.messages)
        older = list(self.session.messages[:-2])
        self.is_busy = True
        self.busy_status = "Compacting context into a handoff summary..."
        self._sync_ui()
        try:
            summary_text = await self.engine.summarize_session(
                older, self.config_data.get("model", "rbit/smart-local")
            )
        except Exception as exc:
            summary_text = self.engine._heuristic_summary(older) + f"\n\n(LLM summary failed: {exc})"

        summary_msg = ChatMessage(
            id=short_id(),
            role="assistant",
            content=(
                f"**[Compacted]** {count - 2} earlier message(s) → handoff summary\n\n{summary_text}"
            ),
            agent=self.agent_mode,
            model=self.config_data.get("model", "rbit/smart-local"),
            tokens=max(24, count_tokens(summary_text)),
        )
        self.session.messages = [summary_msg] + self.session.messages[-2:]
        self.storage.save_session(self.session)
        self.is_busy = False
        self.busy_status = ""
        self._rebuild_chat_view()
        self._sync_ui()

    def action_retry_last(self) -> None:
        self._cmd_retry()

    def _cmd_retry(self) -> None:
        if self.is_busy:
            self.notify("Agent is busy — press Esc first.", severity="warning")
            return
        msgs = self.session.messages
        idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].role == "user"), None)
        if idx is None:
            self.notify("Nothing to retry — no user prompt in this session.", severity="warning")
            return
        user_msg = msgs[idx]
        self.session.messages = msgs[: idx + 1]
        self.storage.save_session(self.session)
        self._rebuild_chat_view()
        self.is_busy = True
        self.busy_status = "Retrying last prompt..."
        self._sync_ui()
        self._busy_worker = self._execute_agent_turn(user_msg)

    def _cmd_fork(self, arg: str) -> None:
        if self.is_busy:
            self.notify("Agent is busy — press Esc first.", severity="warning")
            return
        msgs = self.session.messages
        if not msgs:
            self.notify("Nothing to fork — session is empty.", severity="warning")
            return
        upto = len(msgs)
        if arg.strip():
            try:
                upto = max(1, min(len(msgs), int(arg.strip())))
            except ValueError:
                self.notify("Usage: /fork [N] — N is the message index to branch at.", timeout=3.0)
                return
        forked = Session(title=(self.session.title[:40] + " (fork)"))
        forked.messages = [ChatMessage.from_dict(m.to_dict()) for m in msgs[:upto]]
        forked.todos = [TodoItem.from_dict(t.to_dict()) for t in self.session.todos]
        forked.modified_files = {
            k: FileChangeStat.from_dict(v.to_dict()) for k, v in self.session.modified_files.items()
        }
        self._load_session_into_ui(forked)
        self.storage.save_session(forked)
        self._sync_ui()
        self.notify(f"Forked {upto} message(s) into new session {forked.id}.", timeout=3.0)

    def _cmd_commit(self, arg: str) -> None:
        token = arg.strip().lower()
        if self.is_busy and token not in ("cancel", "no", "abort"):
            self.notify("Agent is busy — press Esc first.", severity="warning")
            return
        if token in ("yes", "y", "confirm", "apply"):
            if not self._pending_commit:
                self.notify("No pending commit — run /commit first.", timeout=2.5)
                return
            self._run_commit_apply()
            return
        if token in ("cancel", "no", "abort"):
            self._pending_commit = ""
            self.notify("Commit cancelled.", timeout=2.0)
            return
        if arg.strip():
            self._pending_commit = arg.strip()
            self.notify("Custom message staged. Send /commit yes to apply it.", timeout=3.0)
            return
        self._draft_commit()

    @work(exclusive=True)
    async def _draft_commit(self) -> None:
        status = await self.engine._run_git("status", "--short")
        unstaged_stat = await self.engine._run_git("diff", "--stat")
        staged_stat = await self.engine._run_git("diff", "--staged", "--stat")
        unstaged = await self.engine._run_git("diff")
        staged = await self.engine._run_git("diff", "--staged")
        if not status.strip() and not unstaged.strip() and not staged.strip():
            self.notify("Nothing to commit — working tree is clean.", timeout=2.5)
            return
        stat = "\n".join(part for part in (unstaged_stat, staged_stat) if part) or status
        diff = "\n".join(part for part in (unstaged, staged) if part) or status
        message = await self.engine.draft_commit_message(
            stat, diff, self.config_data.get("model", "rbit/smart-local")
        )
        self._pending_commit = message
        preview = "\n".join(f"  {line}" for line in message.splitlines())
        self._post_notice(
            "# Proposed commit message\n\n"
            f"{preview}\n\n"
            "Send `/commit yes` to stage all changes and commit, "
            "`/commit <your message>` to replace it, or `/commit cancel` to abort."
        )
        self._sync_ui()

    @work(exclusive=True)
    async def _run_commit_apply(self) -> None:
        message = self._pending_commit
        if not message:
            return
        code, out = await self.engine._run_git_full("add", "-A")
        if code != 0:
            self._post_notice(f"git add failed:\n```text\n{out[:500]}\n```")
            return
        code, out = await self.engine._run_git_full("commit", "-m", message)
        if code != 0:
            self._post_notice(f"git commit failed:\n```text\n{out[:500]}\n```")
            return
        self._pending_commit = ""
        subject = message.splitlines()[0] if message else "(empty)"
        detail = out.splitlines()[0] if out else ""
        self._post_notice(f"Committed: `{subject}`\n{detail}")
        self._sync_ui()

    def _cmd_help(self) -> None:
        self.push_screen(HelpModal())

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    # --- Prompt Submission, Bang Shell & Agent Execution ---
    @on(Input.Submitted, "#prompt-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        popup = self.query_one(AutocompletePopup)
        if popup.display:
            accepted = popup.accept_highlighted()
            if accepted and accepted[0] == "file":
                at_match = re.search(r"@([^\s]*)$", event.value)
                if at_match and at_match.group(1) == accepted[1].replace("\\", "/"):
                    popup.display = False  # token already typed in full — submit as is
                else:
                    self._apply_completion(*accepted)
                    return
            elif accepted and event.value.startswith("/") and event.value.strip() != accepted[1]:
                self._apply_completion(*accepted)
                return
            else:
                popup.display = False

        raw = event.value.strip()
        if not raw:
            self._toggle_last_card()
            return

        event.input.value = ""
        self.prompt_history.append(raw)
        self.history_cursor = -1

        if raw.startswith("!"):
            if self.is_busy:
                self.notify("Agent is busy — press Esc first.", severity="warning")
                return
            cmd_str = raw[1:].strip()
            if cmd_str:
                self._busy_worker = self._execute_bang_command(cmd_str)
            return

        if raw.startswith("/") and self._handle_slash_command(raw):
            return

        self._submit_prompt(raw)

    @work(exclusive=True, group="rbit-bang")
    async def _execute_bang_command(self, cmd_str: str) -> None:
        """Executes !<command> directly in the session feed without calling an LLM."""
        self._has_inline_notices = True
        self.is_busy = True
        self.busy_status = f"Running ! {cmd_str}"
        self._sync_ui()

        scroll = self.query_one("#chat-scroll", ChatScroll)
        rec = ToolCallRecord(
            id=short_id(),
            name="bash",
            arguments={"command": cmd_str, "bang": True},
            status="running",
            summary="running...",
        )
        card = ToolCallCard(rec)
        scroll.mount(card)
        scroll.scroll_end(animate=False)

        try:
            res = await self.executor.execute_bang_shell(cmd_str)
            rec.status = "success" if res.ok else "error"
            rec.summary = res.summary
            rec.output = res.output
            rec.duration_ms = res.duration_ms
            card.update_record(rec)
        finally:
            self.is_busy = False
            self.busy_status = ""
            self._sync_ui()
            scroll.scroll_end(animate=False)

    def _submit_prompt(self, raw_text: str) -> None:
        # Phrase-based mode switch: planning words route the turn to Plan mode.
        lowered = raw_text.lower()
        if lowered.startswith(("спланируй", "составь план", "план:", "plan:")) and self.agent_mode != "plan":
            self.agent_mode = "plan"
            self.notify("Plan mode (по фразе в промпте).", timeout=2.0)
        elif self._phrase_is_build(lowered) and self.agent_mode != "build":
            self.agent_mode = "build"
            self.notify("Build mode (по фразе в промпте).", timeout=2.0)

        if self.is_busy:
            # Prompt queue: let the user type the next task while the agent works;
            # it is dispatched automatically when the current turn finishes.
            self._queued_prompt = raw_text
            preview = raw_text.strip().replace("\n", " ")[:40]
            self.notify(f"В очереди — выполнится после текущего хода: {preview}", timeout=3.0)
            self._sync_ui()
            return

        attachments = re.findall(r"@([^\s]+)", raw_text)
        valid_attachments = []
        for rel in attachments:
            if rel in VIRTUAL_ATTACHMENTS:
                valid_attachments.append(rel)
                continue
            try:
                if self.executor.resolve_path(rel).exists():
                    valid_attachments.append(rel)
            except ValueError:
                continue

        if len(self.session.messages) == 0 and self.session.title in ("New Session", "Новая сессия"):
            self.session.title = raw_text[:44]

        user_msg = ChatMessage(
            id=short_id(),
            role="user",
            content=raw_text,
            agent=self.agent_mode,
            attachments=valid_attachments,
        )
        self.session.messages.append(user_msg)
        self.storage.save_session(self.session)

        self.is_busy = True
        self.busy_status = "Thinking..."
        self._sync_ui()

        scroll = self.query_one("#chat-scroll", ChatScroll)
        scroll.mount(UserMessageCard(user_msg))
        scroll.scroll_end(animate=False)

        self._execute_agent_turn(user_msg)

    @work(exclusive=True, group="rbit-turn")
    async def _execute_agent_turn(self, user_msg: ChatMessage) -> None:
        scroll = self.query_one("#chat-scroll", ChatScroll)
        self._turn_undo_mark = len(self.executor.undo_stack)
        import time as _time

        self._turn_started = _time.monotonic()
        # Bind the turn to the session it started with, so a mid-turn session
        # switch can never route the reply into the wrong session.
        session = self.session
        model_id = self.config_data.get("model", "rbit/smart-local")
        self._live_card = None
        self._interrupted_turn = False
        session.finished = False
        self._schedule_save_session(delay=0.05)

        quiet = bool(self.config_data.get("quiet"))

        async def on_tool_start(rec: ToolCallRecord) -> None:
            self._settle_thinking()
            if quiet:
                return
            card = ToolCallCard(rec)
            self._tool_widgets[rec.id] = card
            if self._thinking is not None:
                scroll.mount(card, before=self._thinking)
            else:
                scroll.mount(card)
            self._smart_scroll_end()

        async def on_tool_end(rec: ToolCallRecord) -> None:
            card = self._tool_widgets.get(rec.id)
            if card is not None:
                card.update_record(rec)
            self._sync_ui()
            self._smart_scroll_end()

        async def on_status(status_text: str) -> None:
            self.busy_status = status_text
            self._sync_ui()

        async def on_thinking(active: bool) -> None:
            if active:
                self._show_thinking()
            else:
                self._settle_thinking()

        async def on_reasoning_chunk(piece: str) -> None:
            ind = self._thinking
            if ind is None:
                return
            self._reasoning_acc += piece
            ind.set_reasoning(self._reasoning_acc)

        async def on_stream_chunk(partial_md: str) -> None:
            if self._live_card is None:
                self._settle_thinking()
                draft = ChatMessage(
                    id=short_id(),
                    role="assistant",
                    content=partial_md,
                    agent=self.agent_mode,
                    model=model_id,
                    duration_s=0.0,
                )
                self._live_card = AssistantMessageCard(
                    draft, self.engine.extra_models, live=True
                )
                if self._thinking is not None:
                    scroll.mount(self._live_card, before=self._thinking)
                else:
                    scroll.mount(self._live_card)
            else:
                await self._live_card.update_stream(partial_md)
            self._smart_scroll_end()

        async def run_engine_once(msg: ChatMessage) -> ChatMessage:
            return await self.engine.run_turn(
                session=session,
                user_msg=msg,
                agent_mode=self.agent_mode,
                model_id=model_id,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
                on_status=on_status,
                on_stream_chunk=on_stream_chunk,
                on_thinking=on_thinking,
                on_reasoning_chunk=on_reasoning_chunk,
            )

        try:
            reply = await run_engine_once(user_msg)
            if self._interrupted_turn:
                self._live_card = None
                return
            session.messages.append(reply)
            self._schedule_save_session(delay=0.05)
            live_card = self._live_card
            self._live_card = None
            if live_card is not None:
                await live_card.finalize(reply)
            else:
                scroll.mount(AssistantMessageCard(reply, self.engine.extra_models))
            self._smart_scroll_end()

            # Self-check: diagnostics over files mutated this turn; a single
            # automatic fix round when something is broken.
            if (
                self.config_data.get("self_check", True)
                and not self._auto_fix_round
                and any(tc.name in MUTATING_TOOLS for tc in reply.tool_calls)
            ):
                fix_msg = self._build_self_check_message(reply)
                if fix_msg is not None:
                    self._auto_fix_round = True
                    try:
                        session.messages.append(fix_msg)
                        self._schedule_save_session(delay=0.05)
                        scroll.mount(UserMessageCard(fix_msg))
                        self._smart_scroll_end()
                        fix_reply = await run_engine_once(fix_msg)
                        if not self._interrupted_turn:
                            session.messages.append(fix_reply)
                            self._schedule_save_session(delay=0.05)
                            self._live_card = None
                            scroll.mount(AssistantMessageCard(fix_reply, self.engine.extra_models))
                            self._smart_scroll_end()
                    finally:
                        self._auto_fix_round = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = str(exc)
            lowered = str(exc).lower()
            hint = ""
            if any(k in lowered for k in ("401", "403", "api key", "unauthorized", "connect")):
                hint = " Check the provider with `/connect`."
            self._post_notice(f"✗ Error: {exc}\n\nPress `Ctrl+R` to retry the last prompt.{hint}")
        finally:
            self._settle_thinking()
            session.finished = True
            self._schedule_save_session(delay=0.05)
            self.is_busy = False
            self.busy_status = ""
            queued = self._queued_prompt
            self._queued_prompt = None
            self._sync_ui()
            if self.config_data.get("notify_done", True) and getattr(self, "_turn_started", 0):
                import time as _time

                if _time.monotonic() - self._turn_started > 30:
                    self._os_notify("Ход завершён")
            if queued:
                self.call_after_refresh(self._submit_prompt, queued)
            elif self.config_data.get("auto_title", True) and len(session.messages) <= 2 and session.title in (
                "New Session",
                "Новая сессия",
            ):
                first_user = next((m.content for m in session.messages if m.role == "user"), "")
                if first_user and not first_user.startswith(("/", "!")):
                    self._run_coro(self._auto_title(session, first_user))
                else:
                    self._maybe_auto_compact(model_id)
            else:
                self._maybe_auto_compact(model_id)
