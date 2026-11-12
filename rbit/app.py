"""Main rbit Textual Application — Full-screen TUI (Home View + Session View + Modals + Bang Shell + Checkpoints)."""

from __future__ import annotations

import asyncio
import platform
import re
import sys
from pathlib import Path

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static

from . import __version__
from .agent import AgentEngine
from .modals import (
    CheckpointModal,
    CommandPaletteModal,
    ConnectApiModal,
    ModelSelectorModal,
    PermissionModal,
    SessionPickerModal,
    ThemeSelectorModal,
    TodoManagerModal,
)
from .models import ChatMessage, Session, TodoItem, ToolCallRecord, short_id
from .storage import StorageManager
from .themes import RBIT_DARK, RBIT_LIGHT
from .tools import ToolExecutor
from .widgets import (
    AgentModeBadge,
    AssistantMessageCard,
    AutocompletePopup,
    BottomStatusBar,
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
    ) -> None:
        super().__init__()
        self.workspace = (workspace or Path.cwd()).resolve()
        self.storage = StorageManager(self.workspace)
        self.config_data = self.storage.load_config()
        if initial_model:
            self.config_data["model"] = initial_model

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

        self.executor = ToolExecutor(
            workspace=self.workspace,
            session=self.session,
            storage=self.storage,
            permission_cb=self._ask_permission,
            get_config=lambda: self.config_data,
        )
        self.engine = AgentEngine(
            workspace=self.workspace,
            executor=self.executor,
            get_config=lambda: self.config_data,
            storage=self.storage,
        )

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace-row"):
            with Vertical(id="center-stage", classes="is-home"):
                yield HomeLogo()
                yield SessionHeaderBar()
                yield VerticalScroll(id="chat-scroll")
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
        self._discover_local_models()

        if self.initial_prompt:
            self.set_timer(0.15, lambda: self._submit_prompt(self.initial_prompt or ""))

    @work
    async def _discover_local_models(self) -> None:
        found = await self.engine.refresh_ollama_models()
        if found:
            self._sync_ui()

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

    def _update_layout_mode(self) -> None:
        stage = self.query_one("#center-stage", Vertical)
        logo = self.query_one(HomeLogo)
        tip = self.query_one(HomeTipBanner)
        header = self.query_one(SessionHeaderBar)
        scroll = self.query_one("#chat-scroll", VerticalScroll)
        sidebar = self.query_one(SidebarPanel)

        if self._is_home_state:
            stage.remove_class("is-session")
            stage.add_class("is-home")
            logo.display = True
            tip.display = True
            header.display = False
            scroll.display = False
            sidebar.display = self._sidebar_override_on_home
        else:
            stage.remove_class("is-home")
            stage.add_class("is-session")
            logo.display = False
            tip.display = False
            header.display = True
            scroll.display = True
            sidebar.display = bool(self.config_data.get("sidebar_visible", True))

    def _sync_ui(self) -> None:
        model_id = self.config_data.get("model", "rbit/smart-local")
        api_ok = bool((self.config_data.get("api_key") or "").strip())
        cp_count = len(self.storage.list_checkpoints())

        self._update_layout_mode()

        pbox = self.query_one("#prompt-box", Vertical)
        pbox.remove_class("mode-build", "mode-plan")
        pbox.add_class(f"mode-{self.agent_mode}")

        self.query_one(AgentModeBadge).set_mode(self.agent_mode)
        self.query_one(PromptModelLabel).set_model(model_id, self.engine.extra_models)
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
        )
        self.query_one(BottomStatusBar).update_bar(self.workspace, self.git_branch)

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
            popup.show_slash(val)
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
        act = await self.push_screen_wait(CommandPaletteModal())
        if not act:
            self.query_one("#prompt-input", Input).focus()
            return
        dispatch = {
            "models": self.action_open_models,
            "theme": self.action_open_themes,
            "todo": self.action_open_todos,
            "checkpoint": lambda: self._cmd_checkpoint(""),
            "rewind": self.action_open_checkpoints,
            "export": lambda: self._cmd_export(""),
            "doctor": self._cmd_doctor,
            "toggle_agent": self.action_toggle_agent,
            "toggle_sidebar": self.action_toggle_sidebar,
            "new_session": self.action_new_session,
            "sessions": self.action_open_sessions,
            "undo": self._cmd_undo,
            "redo": self._cmd_redo,
            "diff": self._cmd_diff,
            "compact": self._cmd_compact,
            "init": lambda: self._submit_prompt("Проанализируй проект и создай подробный Todo-план"),
            "connect": self.action_open_connect,
            "clear": self.action_clear_chat,
            "help": self._cmd_help,
            "exit": self.exit,
        }
        fn = dispatch.get(act)
        if fn:
            fn()

    @work
    async def action_open_models(self) -> None:
        self._discover_local_models()
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

    def _load_session_into_ui(self, sess: Session) -> None:
        self.session = sess
        self.executor.session = sess
        self._has_inline_notices = False
        self._rebuild_chat_view()
        self._sync_ui()

    def _rebuild_chat_view(self) -> None:
        scroll = self.query_one("#chat-scroll", VerticalScroll)
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
        scroll = self.query_one("#chat-scroll", VerticalScroll)
        scroll.mount(Static(text, classes="system-notice"))
        scroll.scroll_end(animate=False)

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
        if cmd == "/init":
            self._submit_prompt("Проанализируй структуру этого проекта и создай стартовый Todo-план")
            return True
        if cmd == "/help":
            self._cmd_help()
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
        count = len(self.session.messages)
        if count <= 2:
            self.notify("Context is already compact.", timeout=2.0)
            return
        summary_msg = ChatMessage(
            id=short_id(),
            role="assistant",
            content=(
                f"**[Compacted]** Summarized {count - 2} earlier messages. "
                f"Active todos: {len(self.session.todos)}."
            ),
            agent=self.agent_mode,
            model=self.config_data.get("model", "rbit/smart-local"),
            tokens=24,
        )
        self.session.messages = [summary_msg] + self.session.messages[-2:]
        self.storage.save_session(self.session)
        self._rebuild_chat_view()
        self._sync_ui()

    def _cmd_help(self) -> None:
        help_txt = (
            "# rbit Keybindings & Power Commands\n"
            "  tab              Switch Build ↔ Plan agent\n"
            "  ctrl+p / k       Open Command Palette\n"
            "  ctrl+b           Toggle right sidebar (Context / MCP / LSP / Todo / Files)\n"
            "  !<command>       Direct Bang Shell execution (zero LLM tokens)\n"
            "  @filename        Fuzzy-attach project file to prompt\n"
            "  /checkpoint [n]  Save Git-free workspace snapshot   /rewind    Restore snapshot\n"
            "  /remember <rule> Save persistent project memory     /export    Export session .md\n"
            "  /todo [task]     Todo checklist manager             /doctor    Environment check\n"
            "  /models /themes  Switch model or live theme         /undo      Revert file edits"
        )
        self._post_notice(help_txt)

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

        scroll = self.query_one("#chat-scroll", VerticalScroll)
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
        valid_attachments = [
            rel for rel in attachments if self.executor.resolve_path(rel).exists()
        ]

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

        scroll = self.query_one("#chat-scroll", VerticalScroll)
        scroll.mount(UserMessageCard(user_msg))
        scroll.scroll_end(animate=False)

        self._execute_agent_turn(user_msg)

    @work(exclusive=True)
    async def _execute_agent_turn(self, user_msg: ChatMessage) -> None:
        scroll = self.query_one("#chat-scroll", VerticalScroll)
        model_id = self.config_data.get("model", "rbit/smart-local")
        live_card: AssistantMessageCard | None = None

        async def on_tool_start(rec: ToolCallRecord) -> None:
            card = ToolCallCard(rec)
            self._tool_widgets[rec.id] = card
            scroll.mount(card)
            scroll.scroll_end(animate=False)

        async def on_tool_end(rec: ToolCallRecord) -> None:
            card = self._tool_widgets.get(rec.id)
            if card is not None:
                card.update_record(rec)
            self._sync_ui()
            scroll.scroll_end(animate=False)

        async def on_status(status_text: str) -> None:
            self.busy_status = status_text
            self._sync_ui()

        async def on_stream_chunk(partial_md: str) -> None:
            nonlocal live_card
            if live_card is None:
                draft = ChatMessage(
                    id=short_id(),
                    role="assistant",
                    content=partial_md,
                    agent=self.agent_mode,
                    model=model_id,
                    duration_s=0.0,
                )
                live_card = AssistantMessageCard(draft, self.engine.extra_models)
                scroll.mount(live_card)
            else:
                await live_card.update_stream(partial_md)
            scroll.scroll_end(animate=False)

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
            self.session.messages.append(reply)
            self.storage.save_session(self.session)
            if live_card is not None:
                await live_card.finalize(reply)
            else:
                scroll.mount(AssistantMessageCard(reply, self.engine.extra_models))
            scroll.scroll_end(animate=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._post_notice(f"✗ Error: {exc}")
        finally:
            self.is_busy = False
            self.busy_status = ""
            self._sync_ui()
