"""Modal dialogs for rbit: Commands (Ctrl+P), Select model, Themes, Sessions, Checkpoints, Todo, Connect API, Permission."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .models import AVAILABLE_MODELS, CheckpointMeta, ModelInfo, Session, TodoItem, short_id
from .themes import CURATED_THEMES


PALETTE_ITEMS: list[tuple[str, str, str, str]] = [
    ("Suggested", "models", "Switch model", "ctrl+m"),
    ("Suggested", "new_session", "New session", "ctrl+n"),
    ("Suggested", "todo", "Manage Todo list", "ctrl+o"),
    ("Suggested", "checkpoint", "Save workspace checkpoint", "/checkpoint"),
    ("Agent", "toggle_agent", "Switch agent (Build ↔ Plan)", "tab"),
    ("Agent", "init", "Initialize / analyze workspace", "/init"),
    ("Session", "sessions", "Switch session", "/sessions"),
    ("Session", "rewind", "Rewind to saved checkpoint", "/rewind"),
    ("Session", "export", "Export session to Markdown", "/export"),
    ("Session", "undo", "Undo last file edit", "/undo"),
    ("Session", "redo", "Redo file edit", "/redo"),
    ("Session", "compact", "Compact session context", "/compact"),
    ("Session", "retry", "Re-run the last prompt", "/retry"),
    ("Session", "fork", "Fork session into a new branch", "/fork"),
    ("Session", "commit", "Draft/confirm a Git commit", "/commit"),
    ("Session", "copy", "Copy the last answer", "ctrl+y"),
    ("Session", "diff", "View modified files diff summary", "/diff"),
    ("Session", "clear", "Clear messages", "ctrl+l"),
    ("System", "toggle_sidebar", "Toggle sidebar", "ctrl+b"),
    ("System", "theme", "Switch theme", "ctrl+t"),
    ("System", "connect", "Connect provider / API key", "/connect"),
    ("System", "doctor", "Run system & API diagnostics", "/doctor"),
    ("System", "help", "Help & keybindings", "/help"),
    ("System", "exit", "Exit rbit", "ctrl+q"),
]


def _modal_header(title: str) -> Table:
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right")
    grid.add_row(Text(title, style="bold #eeeeee"), Text("esc", style="#808080"))
    return grid


def _two_col_option(left_text: str, right_text: str = "", active_dot: bool = False) -> Table:
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right")
    ltxt = Text()
    if active_dot:
        ltxt.append("• ", style="bold")
    ltxt.append(left_text)
    rtxt = Text(right_text, style="dim")
    grid.add_row(ltxt, rtxt)
    return grid


HELP_MARKDOWN = """
# rbit Keybindings & Power Commands

## Navigation
- `tab` — switch Build ↔ Plan agent
- `ctrl+p` / `ctrl+k` — command palette
- `ctrl+b` — toggle right sidebar (Context / MCP / LSP / Ollama / Todo / Files)
- `ctrl+m` — switch model
- `ctrl+t` — switch theme
- `ctrl+o` — Todo manager
- `ctrl+n` — new session
- `ctrl+y` — copy last answer
- `ctrl+q` — quit

## Prompt shorthands
- `!<command>` — direct shell execution (zero LLM tokens)
- `@filename` — fuzzy-attach project file
- `@diff` / `@git` — attach git diff / branch+status+log
- `@staged` / `@tree` — attach staged diff / workspace tree

## Slash commands
- `/checkpoint [n]` / `/rewind` — save or restore workspace snapshot
- `/remember <rule>` — persistent project memory
- `/export` / `/compact` — export session / compact context
- `/retry` / `/fork [N]` — re-run last prompt / branch session
- `/commit [yes]` — draft or confirm a Git commit
- `/doctor` — environment check
- `/copy` — copy last answer
- `/models` / `/themes` — switch model or theme
- `/todo [task]` — Todo checklist manager
- `/undo` / `/redo` — revert or reapply edits
- `/diff` — modified files summary
- `/help` / `F1` — this screen
"""


class HelpModal(ModalScreen[None]):
    """Full-screen keybindings cheat sheet (F1 / /help)."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("f1", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="oc-modal help-modal"):
            yield Static(_modal_header("Help & keybindings"), classes="oc-modal-header")
            with VerticalScroll(id="help-scroll"):
                yield Markdown(HELP_MARKDOWN, id="help-content")
            yield Label("esc / f1 close", classes="oc-modal-foot")

    def action_close(self) -> None:
        self.dismiss(None)


class CommandPaletteModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self._filtered: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="oc-modal"):
            yield Static(_modal_header("Commands"), classes="oc-modal-header")
            yield Input(placeholder="Search", id="palette-search", classes="oc-modal-search")
            yield OptionList(id="palette-list", classes="oc-modal-list")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#palette-search", Input).focus()

    def _populate(self, query: str) -> None:
        q = query.lower().strip()
        olist = self.query_one("#palette-list", OptionList)
        olist.clear_options()
        self._filtered.clear()

        last_group = ""
        for group, act_id, title, shortcut in PALETTE_ITEMS:
            if q and q not in title.lower() and q not in act_id.lower() and q not in group.lower():
                continue
            if group != last_group and not q:
                if last_group:
                    olist.add_option(None)
                olist.add_option(Option(Text(group, style="bold #fab283"), disabled=True))
                last_group = group
            self._filtered.append(act_id)
            olist.add_option(Option(_two_col_option(title, shortcut), id=act_id))

        if self._filtered:
            for idx in range(olist.option_count):
                opt = olist.get_option_at_index(idx)
                if not opt.disabled:
                    olist.highlighted = idx
                    break

    @on(Input.Changed, "#palette-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._populate(event.value)

    @on(Input.Submitted, "#palette-search")
    def _on_enter_search(self) -> None:
        olist = self.query_one("#palette-list", OptionList)
        if olist.highlighted is not None:
            opt = olist.get_option_at_index(olist.highlighted)
            if opt.id:
                self.dismiss(opt.id)

    @on(OptionList.OptionSelected, "#palette-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.dismiss(event.option_id)

    def key_down(self) -> None:
        self.query_one("#palette-list", OptionList).action_cursor_down()

    def key_up(self) -> None:
        self.query_one("#palette-list", OptionList).action_cursor_up()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelSelectorModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, active_model: str, extra_models: list[ModelInfo] | None = None) -> None:
        super().__init__()
        self.active_model = active_model
        self.all_models: list[ModelInfo] = list(AVAILABLE_MODELS)
        existing_ids = {m.id for m in self.all_models}
        for em in extra_models or []:
            if em.id not in existing_ids:
                self.all_models.append(em)

    def compose(self) -> ComposeResult:
        with Vertical(classes="oc-modal"):
            yield Static(_modal_header("Select model"), classes="oc-modal-header")
            yield Input(placeholder="Search models or enter custom provider/model...", id="model-search", classes="oc-modal-search")
            yield OptionList(id="model-list", classes="oc-modal-list")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#model-search", Input).focus()

    def _populate(self, query: str) -> None:
        q = query.lower().strip()
        olist = self.query_one("#model-list", OptionList)
        olist.clear_options()

        last_prov = ""
        matched_ids: list[str] = []
        for m in self.all_models:
            hay = f"{m.id} {m.name} {m.provider}".lower()
            if q and q not in hay:
                continue
            if m.provider != last_prov and not q:
                if last_prov:
                    olist.add_option(None)
                olist.add_option(Option(Text(m.provider, style="bold #fab283"), disabled=True))
                last_prov = m.provider
            matched_ids.append(m.id)
            right = "Free" if m.cost_per_1k == 0 else f"{m.context_window // 1000}k"
            olist.add_option(
                Option(
                    _two_col_option(m.name, right, active_dot=(m.id == self.active_model)),
                    id=m.id,
                )
            )

        if q and q not in [mid.lower() for mid in matched_ids]:
            custom = query.strip()
            olist.add_option(Option(_two_col_option(f"Use custom: {custom}", "Custom"), id=custom))

        for idx in range(olist.option_count):
            opt = olist.get_option_at_index(idx)
            if not opt.disabled:
                olist.highlighted = idx
                break

    @on(Input.Changed, "#model-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._populate(event.value)

    @on(Input.Submitted, "#model-search")
    def _on_submit(self) -> None:
        olist = self.query_one("#model-list", OptionList)
        if olist.highlighted is not None:
            opt = olist.get_option_at_index(olist.highlighted)
            if opt.id:
                self.dismiss(opt.id)

    @on(OptionList.OptionSelected, "#model-list")
    def _on_opt(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.dismiss(event.option_id)

    def key_down(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_down()

    def key_up(self) -> None:
        self.query_one("#model-list", OptionList).action_cursor_up()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ThemeSelectorModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, current_theme: str) -> None:
        super().__init__()
        self.original_theme = current_theme

    def compose(self) -> ComposeResult:
        with Vertical(classes="oc-modal"):
            yield Static(_modal_header("Themes"), classes="oc-modal-header")
            yield Input(placeholder="Search themes", id="theme-search", classes="oc-modal-search")
            yield OptionList(id="theme-list", classes="oc-modal-list")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#theme-search", Input).focus()

    def _populate(self, query: str) -> None:
        q = query.lower().strip()
        olist = self.query_one("#theme-list", OptionList)
        olist.clear_options()
        sel_idx = 0
        shown = 0
        for tid, label in CURATED_THEMES:
            if q and q not in tid.lower() and q not in label.lower():
                continue
            is_cur = tid == self.original_theme
            olist.add_option(Option(_two_col_option(label, tid, active_dot=is_cur), id=tid))
            if is_cur:
                sel_idx = shown
            shown += 1
        if shown:
            olist.highlighted = sel_idx

    @on(Input.Changed, "#theme-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._populate(event.value)

    @on(Input.Submitted, "#theme-search")
    def _on_enter(self) -> None:
        olist = self.query_one("#theme-list", OptionList)
        if olist.highlighted is not None:
            opt = olist.get_option_at_index(olist.highlighted)
            if opt.id:
                self.dismiss(opt.id)

    @on(OptionList.OptionHighlighted, "#theme-list")
    def _on_highlight(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_id and event.option_id in self.app.available_themes:
            self.app.theme = event.option_id

    @on(OptionList.OptionSelected, "#theme-list")
    def _on_select(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def key_down(self) -> None:
        self.query_one("#theme-list", OptionList).action_cursor_down()

    def key_up(self) -> None:
        self.query_one("#theme-list", OptionList).action_cursor_up()

    def action_cancel(self) -> None:
        if self.original_theme in self.app.available_themes:
            self.app.theme = self.original_theme
        self.dismiss(None)


class SessionPickerModal(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("ctrl+d", "del_sess", "Delete"),
    ]

    def __init__(self, sessions: list[Session], current_id: str) -> None:
        super().__init__()
        self.sessions = sessions
        self.current_id = current_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="oc-modal"):
            yield Static(_modal_header("Sessions"), classes="oc-modal-header")
            yield OptionList(id="session-list", classes="oc-modal-list")
            yield Label("enter switch · ctrl+d delete · esc close", classes="oc-modal-foot")

    def on_mount(self) -> None:
        olist = self.query_one("#session-list", OptionList)
        olist.add_option(Option(_two_col_option("+ New session", "ctrl+n"), id="__new__"))
        for s in self.sessions:
            olist.add_option(
                Option(
                    _two_col_option(
                        s.title[:38],
                        f"{len(s.messages)} msg · {s.created_at}",
                        active_dot=(s.id == self.current_id),
                    ),
                    id=s.id,
                )
            )
        olist.highlighted = 0
        olist.focus()

    @on(OptionList.OptionSelected, "#session-list")
    def _on_sel(self, event: OptionList.OptionSelected) -> None:
        if event.option_id == "__new__":
            self.dismiss(("new", ""))
        elif event.option_id:
            self.dismiss(("switch", event.option_id))

    def action_del_sess(self) -> None:
        olist = self.query_one("#session-list", OptionList)
        if olist.highlighted is not None and olist.highlighted > 0:
            target = self.sessions[olist.highlighted - 1]
            self.dismiss(("delete", target.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class CheckpointModal(ModalScreen[tuple[str, str] | None]):
    """Time-travel modal for creating or rewinding Git-free workspace checkpoints."""

    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, checkpoints: list[CheckpointMeta]) -> None:
        super().__init__()
        self.checkpoints = checkpoints

    def compose(self) -> ComposeResult:
        with Vertical(classes="oc-modal"):
            yield Static(_modal_header("Workspace Checkpoints"), classes="oc-modal-header")
            yield Input(placeholder="New checkpoint label (press Enter to save)...", id="cp-input", classes="oc-modal-search")
            yield OptionList(id="cp-list", classes="oc-modal-list")
            yield Label("enter on list = rewind workspace · esc close", classes="oc-modal-foot")

    def on_mount(self) -> None:
        olist = self.query_one("#cp-list", OptionList)
        if not self.checkpoints:
            olist.add_option(Option(Text("No checkpoints yet — type a label above to create one", style="#808080"), disabled=True))
            self.query_one("#cp-input", Input).focus()
        else:
            for cp in self.checkpoints:
                olist.add_option(
                    Option(
                        _two_col_option(f"↺ {cp.label}", f"{cp.file_count} files · {cp.created_at}"),
                        id=cp.id,
                    )
                )
            olist.highlighted = 0
            olist.focus()

    @on(Input.Submitted, "#cp-input")
    def _on_create(self, event: Input.Submitted) -> None:
        label = event.value.strip() or "Manual checkpoint"
        self.dismiss(("create", label))

    @on(OptionList.OptionSelected, "#cp-list")
    def _on_rewind(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.dismiss(("restore", event.option_id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class TodoManagerModal(ModalScreen[list[TodoItem] | None]):
    """Interactive Todo manager (/todo)."""

    BINDINGS = [
        Binding("escape", "save_and_close", "Done"),
        Binding("ctrl+d", "delete_task", "Delete"),
        Binding("ctrl+p", "cycle_prio", "Priority"),
    ]

    STATUS_CYCLE = ["pending", "in_progress", "completed"]

    def __init__(self, todos: list[TodoItem]) -> None:
        super().__init__()
        self.todos: list[TodoItem] = [TodoItem.from_dict(t.to_dict()) for t in todos]

    def compose(self) -> ComposeResult:
        with Vertical(classes="oc-modal"):
            yield Static(_modal_header("Todos"), classes="oc-modal-header")
            yield Input(placeholder="Add new task and press Enter...", id="todo-new-input", classes="oc-modal-search")
            yield OptionList(id="todo-modal-list", classes="oc-modal-list")
            yield Label("enter toggle status · ctrl+p priority · ctrl+d delete · esc save", classes="oc-modal-foot")

    def on_mount(self) -> None:
        self._refresh_list()
        self.query_one("#todo-new-input", Input).focus()

    def _refresh_list(self, keep_idx: int = 0) -> None:
        olist = self.query_one("#todo-modal-list", OptionList)
        olist.clear_options()
        for t in self.todos:
            mark = "[✓]" if t.status == "completed" else ("[•]" if t.status == "in_progress" else "[ ]")
            olist.add_option(Option(_two_col_option(f"{mark} {t.content}", t.priority), id=t.id))
        if self.todos:
            olist.highlighted = min(max(0, keep_idx), len(self.todos) - 1)

    @on(Input.Submitted, "#todo-new-input")
    def _on_add_enter(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        if val:
            self.todos.append(TodoItem(id=short_id(), content=val, status="pending", priority="medium"))
            event.input.value = ""
            self._refresh_list(len(self.todos) - 1)
        else:
            self._toggle_highlighted()

    def _toggle_highlighted(self) -> None:
        olist = self.query_one("#todo-modal-list", OptionList)
        if olist.highlighted is None or not (0 <= olist.highlighted < len(self.todos)):
            return
        idx = olist.highlighted
        cur = self.todos[idx].status
        nxt = self.STATUS_CYCLE[(self.STATUS_CYCLE.index(cur) + 1) % len(self.STATUS_CYCLE)] if cur in self.STATUS_CYCLE else "pending"
        self.todos[idx].status = nxt
        self._refresh_list(idx)

    @on(OptionList.OptionSelected, "#todo-modal-list")
    def _on_list_sel(self) -> None:
        self._toggle_highlighted()

    def key_down(self) -> None:
        self.query_one("#todo-modal-list", OptionList).action_cursor_down()

    def key_up(self) -> None:
        self.query_one("#todo-modal-list", OptionList).action_cursor_up()

    def action_cycle_prio(self) -> None:
        olist = self.query_one("#todo-modal-list", OptionList)
        if olist.highlighted is None or not (0 <= olist.highlighted < len(self.todos)):
            return
        idx = olist.highlighted
        order = ["low", "medium", "high"]
        cur = self.todos[idx].priority
        self.todos[idx].priority = order[(order.index(cur) + 1) % len(order)] if cur in order else "high"
        self._refresh_list(idx)

    def action_delete_task(self) -> None:
        olist = self.query_one("#todo-modal-list", OptionList)
        if olist.highlighted is None or not (0 <= olist.highlighted < len(self.todos)):
            return
        idx = olist.highlighted
        self.todos.pop(idx)
        self._refresh_list(max(0, idx - 1))

    def action_save_and_close(self) -> None:
        self.dismiss(self.todos)


class ConnectApiModal(ModalScreen[dict[str, str] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, api_base: str, api_key: str) -> None:
        super().__init__()
        self.api_base = api_base
        self.api_key = api_key

    def compose(self) -> ComposeResult:
        with Vertical(classes="oc-modal"):
            yield Static(_modal_header("Connect provider"), classes="oc-modal-header")
            yield Label("Base URL (OpenRouter / OpenAI / Ollama):", classes="oc-field-label")
            yield Input(value=self.api_base, placeholder="https://openrouter.ai/api/v1", id="inp-api-base", classes="oc-modal-search")
            yield Label("API Key (leave empty for built-in rbit Smart Agent):", classes="oc-field-label")
            yield Input(value=self.api_key, password=True, placeholder="sk-...", id="inp-api-key", classes="oc-modal-search")
            with Horizontal(classes="oc-btn-row"):
                yield Button("Connect", id="btn-save-api", variant="primary")
                yield Button("Reset to Local", id="btn-clear-api")
                yield Button("Cancel", id="btn-cancel-api")

    @on(Button.Pressed, "#btn-save-api")
    def _save(self) -> None:
        base = self.query_one("#inp-api-base", Input).value.strip() or "https://openrouter.ai/api/v1"
        key = self.query_one("#inp-api-key", Input).value.strip()
        self.dismiss({"api_base": base, "api_key": key})

    @on(Button.Pressed, "#btn-clear-api")
    def _clear(self) -> None:
        self.dismiss({"api_base": "https://openrouter.ai/api/v1", "api_key": ""})

    @on(Button.Pressed, "#btn-cancel-api")
    def action_cancel(self) -> None:
        self.dismiss(None)


class PermissionModal(ModalScreen[str]):
    """Permission prompt: Allow once / Allow always / Reject."""

    BINDINGS = [
        Binding("y", "allow_once", "Allow once"),
        Binding("a", "allow_always", "Always allow"),
        Binding("n", "deny", "Reject"),
        Binding("escape", "deny", "Reject"),
    ]

    def __init__(self, tool_name: str, detail: str) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="oc-modal"):
            yield Static(_modal_header("Permission required"), classes="oc-modal-header")
            yield Static(
                f"[#808080]Tool:[/] [bold #fab283]{self.tool_name}[/]\n\n"
                f"[bold #eeeeee]{self.detail}[/]",
                classes="oc-perm-body",
            )
            yield Label("y allow once   a always allow   esc reject", classes="oc-modal-foot")

    def action_allow_once(self) -> None:
        self.dismiss("once")

    def action_allow_always(self) -> None:
        self.dismiss("always")

    def action_deny(self) -> None:
        self.dismiss("deny")
