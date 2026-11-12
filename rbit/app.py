"""Main rbit Textual Application — Full-screen TUI (Home View + Session View + Modals + Bang Shell + Checkpoints)."""

from __future__ import annotations

import asyncio
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Input, OptionList, Static

from . import __version__
from .agent import AgentEngine
from .modals import (
    CheckpointModal,
    CommandPaletteModal,
    ConnectApiModal,
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
from .tools import VIRTUAL_ATTACHMENTS, ToolExecutor
from .widgets import (
    AgentModeBadge,
    AssistantMessageCard,
    AutocompletePopup,
    BottomStatusBar,
    ChatScroll,
    HomeLogo,
    HomeTipBanner,
    PromptModelLabel,
    PromptSubHints,
    SessionHeaderBar,
    SidebarPanel,
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
    padding: 0 2;
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
    background: $surface;
    border-left: thick #323232;
    padding: 1 2;
    margin: 1 2 0 2;
}

/* --- Scrollable Session Feed --- */
#chat-scroll {
    width: 1fr;
    height: 1fr;
    padding: 1 2 0 2;
    scrollbar-size-vertical: 1;
}

.user-card {
    background: $surface;
    padding: 1 2;
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
    background: $surface;
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
    padding: 0 2 1 2;
}

#autocomplete-popup {
    display: none;
    max-height: 10;
    background: $surface;
    border: tall #2a2a2a;
    margin-bottom: 1;
}

#prompt-box {
    width: 100%;
    height: auto;
    background: $surface;
    padding: 1 2 0 2;
}

#prompt-box.mode-build {
    border-left: thick #fab283;
}

#prompt-box.mode-plan {
    border-left: thick #5c9cf5;
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
    width: 40;
    height: 1fr;
    background: $surface;
    border-left: vkey #1e1e1e;
    padding: 1 2;
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
    border: tall #282828;
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
        Binding("ctrl+k", "open_palette", "Commands", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=False),
        Binding("ctrl+m", "open_models", "Models", show=False),
        Binding("ctrl+t", "open_themes", "Themes", show=False),
        Binding("ctrl+o", "open_todos", "Todos", show=False),
        Binding("ctrl+n", "new_session", "New Session", show=False),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
        Binding("ctrl+y", "copy_last", "Copy", show=False),
        Binding("ctrl+f", "search_session", "Search", show=False),
        Binding("ctrl+x", "stop_background", "Stop background", show=False),
        Binding("ctrl+e", "collapse_all_cards", "Collapse cards", show=False),
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
        self._spin_frame: int = 0
        self._has_inline_notices: bool = False
        self._sidebar_override_on_home: bool = False
        self._tool_widgets: dict[str, ToolCallCard] = {}
        self._save_timer: Timer | None = None
        self._live_card: AssistantMessageCard | None = None
        self._interrupted_turn: bool = False

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
                        yield Input(
                            placeholder='Ask anything... "!git status", "@file", "/checkpoint"',
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
        self.register_theme(RBIT_DARK)
        self.register_theme(RBIT_LIGHT)
        self.register_theme(RBIT_HIGH_CONTRAST)

        desired_theme = self.config_data.get("theme", "rbit-dark")
        if desired_theme in self.available_themes:
            self.theme = desired_theme
        else:
            self.theme = "rbit-dark"

        self.set_interval(0.09, self._tick_spinner)
        if self.session.messages:
            self._rebuild_chat_view()
        self._update_layout_mode()
        self._sync_ui()
        self.query_one("#prompt-input", Input).focus()
        self._discover_models()

        if self.initial_prompt:
            self.set_timer(0.15, lambda: self._submit_prompt(self.initial_prompt or ""))

    def on_unmount(self) -> None:
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
                status=self.busy_status,
                mode=self.agent_mode,
                frame=self._spin_frame,
            )

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
            status=self.busy_status,
            mode=self.agent_mode,
            frame=self._spin_frame,
        )
        self.query_one(SessionHeaderBar).update_header(self.session, model_id, self.engine.extra_models)
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
            self.workers.cancel_all()
            self._interrupted_turn = self._finalize_interrupted()
            self.is_busy = False
            self.busy_status = ""
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
            inp.value = re.sub(r"@([^\s]*)$", f"@{value} ", inp.value)
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
        self.session = Session()
        self.executor.session = self.session
        self._has_inline_notices = False
        self._sidebar_override_on_home = False
        self.query_one(HomeTipBanner).pick_new_tip()
        self._rebuild_chat_view()
        self._sync_ui()
        self.query_one("#prompt-input", Input).focus()

    def action_clear_chat(self) -> None:
        self.session.messages.clear()
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
        self.session = sess
        self.executor.session = sess
        self._has_inline_notices = False
        self._rebuild_chat_view()
        self._sync_ui()

    def _rebuild_chat_view(self) -> None:
        scroll = self.query_one("#chat-scroll", ChatScroll)
        scroll.remove_children()
        self._tool_widgets.clear()
        for m in self.session.messages:
            if m.role == "user":
                scroll.mount(UserMessageCard(m))
            elif m.role == "assistant":
                for tc in m.tool_calls:
                    scroll.mount(ToolCallCard(tc))
                scroll.mount(AssistantMessageCard(m, self.engine.extra_models))
        scroll.scroll_end(animate=False)

    def _post_notice(self, text: str) -> None:
        self._has_inline_notices = True
        self._update_layout_mode()
        scroll = self.query_one("#chat-scroll", ChatScroll)
        scroll.mount(Static(text, classes="system-notice"))
        self._smart_scroll_end()

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
        except Exception:
            pass

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

        custom = self.storage.load_custom_commands()
        custom_name = cmd.lstrip("/")
        if custom_name in custom:
            template = custom[custom_name]["template"]
            prompt = template.replace("{args}", arg) if "{args}" in template else (
                template + (f"\n\n{arg}" if arg else "")
            )
            self._submit_prompt(prompt.strip())
            return True
        return False

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
        if len(self.session.messages) <= 2:
            self.notify("Context is already compact.", timeout=2.0)
            return
        self._compact_context()

    @work(exclusive=True)
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
        self._execute_agent_turn(user_msg)

    def _cmd_fork(self, arg: str) -> None:
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
            if accepted and event.value.startswith("/") and event.value.strip() != accepted[1]:
                self._apply_completion(*accepted)
                return
            popup.display = False

        raw = event.value.strip()
        if not raw:
            self._toggle_last_card()
            return

        event.input.value = ""
        self.prompt_history.append(raw)
        self.history_cursor = -1

        if raw.startswith("!"):
            cmd_str = raw[1:].strip()
            if cmd_str:
                self._execute_bang_command(cmd_str)
            return

        if raw.startswith("/") and self._handle_slash_command(raw):
            return

        self._submit_prompt(raw)

    @work(exclusive=True)
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
        if self.is_busy:
            self.notify("Agent is currently working — press Esc to interrupt.", severity="warning")
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

    @work(exclusive=True)
    async def _execute_agent_turn(self, user_msg: ChatMessage) -> None:
        scroll = self.query_one("#chat-scroll", ChatScroll)
        model_id = self.config_data.get("model", "rbit/smart-local")
        self._live_card = None
        self._interrupted_turn = False

        async def on_tool_start(rec: ToolCallRecord) -> None:
            card = ToolCallCard(rec)
            self._tool_widgets[rec.id] = card
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

        async def on_stream_chunk(partial_md: str) -> None:
            if self._live_card is None:
                draft = ChatMessage(
                    id=short_id(),
                    role="assistant",
                    content=partial_md,
                    agent=self.agent_mode,
                    model=model_id,
                    duration_s=0.0,
                )
                self._live_card = AssistantMessageCard(draft, self.engine.extra_models)
                scroll.mount(self._live_card)
            else:
                await self._live_card.update_stream(partial_md)
            self._smart_scroll_end()

        try:
            reply = await self.engine.run_turn(
                session=self.session,
                user_msg=user_msg,
                agent_mode=self.agent_mode,
                model_id=model_id,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
                on_status=on_status,
                on_stream_chunk=on_stream_chunk,
            )
            if self._interrupted_turn:
                self._live_card = None
                return
            self.session.messages.append(reply)
            self._schedule_save_session(delay=0.05)
            live_card = self._live_card
            self._live_card = None
            if live_card is not None:
                await live_card.finalize(reply)
            else:
                scroll.mount(AssistantMessageCard(reply, self.engine.extra_models))
            self._smart_scroll_end()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._post_notice(f"✗ Error: {exc}")
        finally:
            self.is_busy = False
            self.busy_status = ""
            self._sync_ui()
