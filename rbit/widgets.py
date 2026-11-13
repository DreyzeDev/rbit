"""Terminal UI widgets for rbit (Home View, Session View, Inline Tool Diff Cards, Flat Sidebar)."""

from __future__ import annotations

import random
import re
import subprocess
import time
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Markdown, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__
from .models import ChatMessage, ModelInfo, Session, ToolCallRecord, get_model_info


SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/models", "List and switch AI models (incl. local Ollama)"),
    ("/themes", "Switch color theme (live preview)"),
    ("/effort", "Select reasoning effort (auto / low / medium / high)"),
    ("/todo", "Open interactive Todo list manager"),
    ("/checkpoint", "Save named workspace snapshot (no Git needed)"),
    ("/rewind", "Rewind workspace to a saved checkpoint"),
    ("/remember", "Save persistent project rule to .rbit/memory.md"),
    ("/export", "Export current session to Markdown"),
    ("/doctor", "Run environment, Git, API & Ollama diagnostics"),
    ("/sessions", "List and switch saved sessions"),
    ("/new", "Start a new session"),
    ("/undo", "Undo last file modification"),
    ("/redo", "Redo previously undone modification"),
    ("/compact", "Compact session context history"),
    ("/retry", "Re-run the last user prompt"),
    ("/fork", "Branch the session into a new one at message N"),
    ("/commit", "Draft a Git commit message from the current diff"),
    ("/copy", "Copy the last assistant answer to the clipboard"),
    ("/search", "Search session messages and tool output"),
    ("/diff", "Show modified files summary"),
    ("/init", "Analyze workspace and build initial Todo plan"),
    ("/connect", "Connect API provider (OpenRouter / OpenAI / Ollama)"),
    ("/clear", "Clear current session messages"),
    ("/help", "Show keyboard shortcuts and commands"),
    ("/exit", "Exit rbit"),
]

HOME_TIPS: list[tuple[str, str, str]] = [
    ("Prefix any command with ", "!cmd", " to run shell commands directly with zero tokens"),
    ("Use ", "@filename", " to fuzzy-attach any project file into your prompt"),
    ("Press ", "Tab", " anytime to switch between Build and Plan agents"),
    ("Run ", "/checkpoint", " and /rewind for Git-free workspace time travel"),
    ("Run ", "/todo", " or press Ctrl+O to manage your live task checklist"),
    ("Press ", "Ctrl+P", " to open the grouped command palette"),
    ("Run ", "/remember", " to teach rbit persistent rules for this project"),
]

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def detect_git_branch(workspace: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=1.2,
        )
        if res.returncode == 0:
            branch = res.stdout.strip()
            if branch:
                return branch
    except Exception:
        pass
    return ""


def agent_accent(mode: str) -> str:
    return "#fab283" if mode == "build" else "#5c9cf5"


class HomeLogo(Static):
    """Shaded pixel block logo spelling 'rbit' with amber dot accent."""

    def __init__(self) -> None:
        super().__init__(id="home-logo")

    def render(self) -> Text:
        txt = Text(justify="center")
        txt.append("        ██▄     ", style="bold #8a8a8a")
        txt.append("██  ", style="bold #fab283")
        txt.append(" ██▄  \n", style="bold #eeeeee")
        txt.append("██▄▀▀   ██▄▄▄   ", style="bold #9e9e9e")
        txt.append("▄▄  ", style="bold #eeeeee")
        txt.append("▀██▀▀ \n", style="bold #eeeeee")
        txt.append("██▀     ██▀ ██  ", style="bold #b8b8b8")
        txt.append("██   ", style="bold #eeeeee")
        txt.append("██   \n", style="bold #eeeeee")
        txt.append("██      ██▄▄██  ", style="bold #9e9e9e")
        txt.append("██   ", style="bold #d8d8d8")
        txt.append("██▄▄ \n", style="bold #d8d8d8")
        txt.append("▀▀      ▀▀▀▀▀   ", style="#484848")
        txt.append("▀▀   ", style="#606060")
        txt.append(" ▀▀▀ ", style="#606060")
        return txt


class HomeTipBanner(Static):
    """Subtle centered '● Tip ...' line below the prompt on the Home screen."""

    def __init__(self) -> None:
        super().__init__(id="home-tip")
        self._tip = random.choice(HOME_TIPS)

    def pick_new_tip(self) -> None:
        self._tip = random.choice(HOME_TIPS)
        self.refresh()

    def render(self) -> Text:
        pre, hi, post = self._tip
        t = Text(justify="center")
        t.append("● Tip ", style="bold #fab283")
        t.append(pre, style="#808080")
        t.append(hi, style="bold #eeeeee")
        t.append(post, style="#808080")
        return t


class AgentModeBadge(Static):
    """Clickable agent name inside the prompt footer ('Build' / 'Plan')."""

    class Toggled(Message):
        pass

    def __init__(self, mode: str = "build") -> None:
        super().__init__(id="agent-badge")
        self.mode = mode

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        color = agent_accent(mode)
        t = Text()
        t.append(mode.capitalize(), style=f"bold {color}")
        self.update(t)

    def on_mount(self) -> None:
        self.set_mode(self.mode)

    def on_click(self, _event: events.Click) -> None:
        self.post_message(self.Toggled())


class PromptModelLabel(Static):
    """Clickable model + provider label inside the prompt box (opens /models)."""

    class Clicked(Message):
        pass

    def set_model(
        self,
        model_id: str,
        extra_models: list[ModelInfo] | None = None,
        effort: str = "auto",
    ) -> None:
        info = get_model_info(model_id, extra_models)
        t = Text()
        t.append(f"  {info.name}", style="#eeeeee")
        t.append(f"  {info.provider}", style="#808080")
        if effort and effort != "auto":
            t.append(f"  effort:{effort}", style="#fab283")
        self.update(t)

    def on_click(self, _event: events.Click) -> None:
        self.post_message(self.Clicked())


class PromptSubHints(Static):
    """Line directly beneath the prompt box: spinner + 'esc interrupt' or 'tab agents  ctrl+p commands'."""

    def __init__(self) -> None:
        super().__init__(id="prompt-sub-hints")
        self._busy = False
        self._status = ""
        self._mode = "build"
        self._frame = 0

    def update_state(self, busy: bool, status: str, mode: str, frame: int = 0) -> None:
        self._busy = busy
        self._status = status
        self._mode = mode
        self._frame = frame
        self.refresh()

    def render(self) -> Table:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right")

        left = Text()
        right = Text()

        if self._busy:
            spin = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
            accent = agent_accent(self._mode)
            left.append(f" {spin} ", style=f"bold {accent}")
            left.append("Thinking...", style="#808080")
            right.append("esc", style="bold #eeeeee")
            right.append(" interrupt", style="#808080")
        else:
            right.append("tab", style="#eeeeee")
            right.append(" agents   ", style="#808080")
            right.append("ctrl+p", style="#eeeeee")
            right.append(" commands", style="#808080")

        grid.add_row(left, right)
        return grid


class ThinkingIndicator(Static):
    """Inline chat line: spins while the model thinks; click to reveal gray reasoning."""

    MAX_REASONING_CHARS = 1600

    def __init__(self) -> None:
        super().__init__(classes="thinking-line")
        self._frame = 0
        self._reasoning = ""
        self._expanded = False
        self._active = True
        self._elapsed = 0.0
        self._started = time.monotonic()

    def set_reasoning(self, text: str) -> None:
        self._reasoning = text
        if self._expanded:
            self.refresh()

    @property
    def has_reasoning(self) -> bool:
        return bool(self._reasoning.strip())

    def tick(self, frame: int) -> None:
        self._frame = frame
        if self._active:
            self._elapsed = time.monotonic() - self._started
            self.refresh()

    def finish(self) -> None:
        self._active = False
        self._elapsed = time.monotonic() - self._started
        self._expanded = False
        self.refresh()

    def toggle(self) -> None:
        if not self.has_reasoning:
            return
        self._expanded = not self._expanded
        self.refresh()

    def on_click(self, _event: events.Click) -> None:
        self.toggle()

    def _header(self) -> Text:
        head = Text()
        if self._active:
            spin = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
            head.append(f"{spin} Thinking...", style="bold #f5a742")
            if self.has_reasoning:
                head.append("  click to show reasoning", style="#808080")
            return head
        head.append("▾ " if self._expanded else "▸ ", style="bold #808080")
        head.append(f"Thought for {self._elapsed:.0f}s", style="#808080")
        if self.has_reasoning:
            head.append(
                "  click to hide" if self._expanded else "  click to show reasoning",
                style="#606060",
            )
        return head

    def render(self) -> Text:
        head = self._header()
        if not self._expanded or not self.has_reasoning:
            return head
        body = self._reasoning.strip()
        if len(body) > self.MAX_REASONING_CHARS:
            body = "…" + body[-self.MAX_REASONING_CHARS:]
        text = Text()
        text.append_text(head)
        text.append("\n")
        text.append(body, style="#8f8f8f")
        return text


class SessionHeaderBar(Static):
    """Top session bar: '# Session Title' on the left, 'tokens (%) $cost' on the right."""

    def __init__(self) -> None:
        super().__init__(id="session-header")
        self._title = "New Session"
        self._stats = ""
        self._used = 0
        self._pct = 0
        self._cost = 0.0

    def update_header(
        self,
        session: Session,
        model_id: str,
        extra_models: list[ModelInfo] | None = None,
    ) -> None:
        info = get_model_info(model_id, extra_models)
        used = session.total_tokens
        pct = min(100, round((used / max(1, info.context_window)) * 100))
        cost = (used / 1000.0) * info.cost_per_1k
        self._title = session.title or "New Session"
        self._stats = f"{used:,}  {pct}% (${cost:.2f})"
        self._used = used
        self._pct = pct
        self._cost = cost
        self.refresh()

    def render(self) -> Table:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right")
        left = Text()
        left.append("# ", style="bold #808080")
        left.append(self._title, style="bold #eeeeee")
        right = Text()
        right.append(f"{self._used:,}", style="#c8c8c8")
        right.append(f"  {self._pct}%", style="#808080")
        right.append(f"  ${self._cost:.2f}", style="#808080")
        grid.add_row(left, right)
        return grid


class UserMessageCard(Static):
    """User message block: #141414 panel with thick agent-colored left border."""

    def __init__(self, msg: ChatMessage) -> None:
        super().__init__(classes=f"user-card mode-{msg.agent}")
        self.msg = msg

    def render(self) -> Group:
        body = Text(self.msg.content, style="#eeeeee")
        parts: list[object] = [body]
        if self.msg.attachments:
            att_line = Text()
            for att in self.msg.attachments:
                att_line.append(f" @{att} ", style="bold #0a0a0a on #fab283")
                att_line.append(" ")
            parts.append(att_line)
        return Group(*parts)


def _render_unified_diff(diff_text: str) -> Text:
    """Renders unified diff with a colored gutter: additions green, deletions red."""
    out = Text()
    old_ln = 0
    new_ln = 0
    lines = diff_text.splitlines()
    shown = 0
    add_style = "#d4f7dc on #13261a"
    del_style = "#f7ccd0 on #2b1418"
    for raw in lines:
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        if raw.startswith("@@"):
            try:
                seg = raw.split("@@")[1].strip().split()
                old_ln = abs(int(seg[0].split(",")[0]))
                new_ln = abs(int(seg[1].split(",")[0]))
            except Exception:
                old_ln, new_ln = 1, 1
            continue
        if shown >= 55:
            out.append("   ... diff truncated ...\n", style="dim #808080")
            break
        if raw.startswith("+"):
            out.append(f" {new_ln:>4} ", style="#4f6a52")
            out.append("+ ", style="bold #7fd88f")
            out.append(f"{raw[1:]}\n", style=add_style)
            new_ln += 1
            shown += 1
        elif raw.startswith("-"):
            out.append(f" {old_ln:>4} ", style="#6a4f56")
            out.append("- ", style="bold #e06c75")
            out.append(f"{raw[1:]}\n", style=del_style)
            old_ln += 1
            shown += 1
        else:
            content = raw[1:] if raw.startswith(" ") else raw
            out.append(f" {new_ln:>4} ", style="#3a3a3a")
            out.append("  ", style="#3a3a3a")
            out.append(f"{content}\n", style="#8a8a8a")
            old_ln += 1
            new_ln += 1
            shown += 1
    return out


def _append_counts(target: Text, summary: str, base_style: str = "#808080") -> None:
    """Appends a tool summary, tinting the `(+N -M)` counters green/red."""
    match = re.search(r"\(\+(\d+) -(\d+)\)", summary)
    if not match:
        target.append(summary, style=base_style)
        return
    prefix = summary[: match.start()].strip()
    if prefix:
        target.append(f"{prefix} ", style=base_style)
    target.append(f"+{match.group(1)}", style="bold #7fd88f")
    target.append(" ", style=base_style)
    target.append(f"-{match.group(2)}", style="bold #e06c75")
    suffix = summary[match.end():].strip()
    if suffix:
        target.append(f" {suffix}", style=base_style)


class ToolCallCard(Static):
    """Inline tool call renderer (→ Read, ✱ Glob/Grep, ↗ WebFetch, ← Write/Edit/MultiEdit + diff, $ Bash, # Todos)."""

    expanded: reactive[bool] = reactive(False)

    class StopRequested(Message):
        """Posted when the user asks to stop a background bash process from its card."""

        def __init__(self, pid: int) -> None:
            super().__init__()
            self.pid = pid

    class CopyRequested(Message):
        """Posted when the user right-clicks a card to copy its path or command."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, record: ToolCallRecord) -> None:
        super().__init__(classes="tool-card")
        self.record = record
        self.stop_state = "running" if record.pid else ""
        if record.diff or record.name in ("todowrite", "bash"):
            self.expanded = True

    def action_toggle_expanded(self) -> None:
        self.expanded = not self.expanded

    def update_record(self, record: ToolCallRecord) -> None:
        self.record = record
        if record.pid and not self.stop_state:
            self.stop_state = "running"
        if record.diff or record.name in ("todowrite", "bash"):
            self.expanded = True
        self.refresh(layout=True)

    def mark_stopped(self) -> None:
        if self.record.pid:
            self.stop_state = "stopped"
            self.refresh(layout=True)

    def copy_target(self) -> str:
        args = self.record.arguments or {}
        name = self.record.name
        if name == "bash":
            return str(args.get("command") or "")
        if name in ("read", "write", "edit", "multi_edit", "apply_patch", "symbols", "diagnostics", "lint"):
            return str(args.get("filePath") or args.get("path") or "")
        if name in ("grep", "glob"):
            return str(args.get("pattern") or "")
        if name == "webfetch":
            return str(args.get("url") or "")
        return ""

    def on_click(self, event: events.Click) -> None:
        if getattr(event, "button", 1) == 3:
            text = self.copy_target()
            if text:
                self.post_message(self.CopyRequested(text))
            return
        if self.record.pid and self.stop_state == "running":
            self.post_message(self.StopRequested(self.record.pid))
            return
        self.expanded = not self.expanded

    def render(self) -> Group:
        rec = self.record
        name = rec.name
        args = rec.arguments or {}

        if rec.status == "running":
            line = Text()
            line.append("~ ", style="#808080")
            line.append(f"{self._format_label(name, args)}...", style="italic #808080")
            return Group(line)

        is_err = rec.status != "success"
        err_color = "#e06c75"

        # 1. Inline search/read/fetch tools
        if name in ("read", "glob", "grep", "webfetch") and not is_err:
            line = Text()
            if name == "read":
                fpath = args.get("filePath", ".")
                line.append("→ ", style="#5c9cf5")
                line.append(f"Read {fpath}", style="#a8a8a8")
            elif name == "glob":
                pat = args.get("pattern", "*")
                line.append("✱ ", style="#9d7cd8")
                line.append(f'Glob "{pat}" ', style="#a8a8a8")
                line.append(f"({rec.summary})", style="#585858")
            elif name == "grep":
                pat = args.get("pattern", "")
                line.append("✱ ", style="#9d7cd8")
                line.append(f'Grep "{pat}" ', style="#a8a8a8")
                line.append(f"({rec.summary})", style="#585858")
            elif name == "webfetch":
                url = args.get("url", "")
                line.append("↗ ", style="#5c9cf5")
                line.append(f"WebFetch {url} ", style="#a8a8a8")
                line.append(f"({rec.summary})", style="#585858")

            if not self.expanded or not rec.output:
                return Group(line)
            preview = "\n".join(rec.output.splitlines()[:20])
            return Group(line, Text(preview, style="#686868"))

        # 2. Symbols / diagnostics / lint / git / subagent
        if name in ("symbols", "diagnostics", "lint", "git", "subagent") and not is_err:
            icons = {
                "symbols": ("⌘", "#5c9cf5"),
                "diagnostics": ("⚕", "#fab283"),
                "lint": ("⚑", "#e5c07b"),
                "git": ("⎇", "#9d7cd8"),
                "subagent": ("⤷", "#7fd88f"),
            }
            icon, icon_color = icons[name]
            line = Text()
            line.append(f"{icon} ", style=icon_color)
            line.append(f"{name.capitalize()} ", style="#a8a8a8")
            if rec.summary:
                line.append(f"({rec.summary})", style="#585858")
            if not self.expanded or not rec.output:
                return Group(line)
            preview = "\n".join(rec.output.splitlines()[:20])
            return Group(line, Text(preview, style="#686868"))

        # 3. Write / Edit / MultiEdit with inline diff block
        if name in ("write", "edit", "multi_edit", "apply_patch"):
            fpath = args.get("filePath", "")
            verb = {"write": "Write", "edit": "Edit", "multi_edit": "MultiEdit", "apply_patch": "Patch"}[name]
            header = Text()
            verb_color = {
                "write": "#7fd88f",
                "edit": "#fab283",
                "multi_edit": "#fab283",
                "apply_patch": "#9d7cd8",
            }[name]
            header.append(
                "← " if not is_err else "✗ ",
                style=verb_color if not is_err else err_color,
            )
            header.append(f"{verb} {fpath} ", style="#d8d8d8" if not is_err else err_color)
            _append_counts(
                header,
                rec.summary.replace(fpath, "").strip(),
                base_style="#808080" if not is_err else err_color,
            )
            if not self.expanded:
                return Group(header)
            if rec.diff:
                return Group(header, _render_unified_diff(rec.diff))
            if rec.output:
                return Group(header, Text(rec.output, style=err_color if is_err else "#808080"))
            return Group(header)

        # 3. TodoWrite checklist block
        if name == "todowrite":
            header = Text()
            header.append("# ", style="bold #fab283")
            header.append("Todos", style="bold #eeeeee")
            if not self.expanded:
                return Group(header)
            todo_lines = Text()
            raw_todos = args.get("todos") or []
            for item in raw_todos:
                st = item.get("status", "pending")
                content = item.get("content", "")
                if st == "completed":
                    todo_lines.append("[✓] ", style="bold #7fd88f")
                    todo_lines.append(f"{content}\n", style="strike #808080")
                elif st == "in_progress":
                    todo_lines.append("[•] ", style="bold #fab283")
                    todo_lines.append(f"{content}\n", style="#eeeeee")
                else:
                    todo_lines.append("[ ] ", style="#585858")
                    todo_lines.append(f"{content}\n", style="#9a9a9a")
            return Group(header, todo_lines)

        # 4. Bash command block (agent bash or !bang command)
        if name == "bash":
            cmd = args.get("command", "")
            is_bang = bool(args.get("bang"))
            header = Text()
            header.append("! " if is_bang else "$ ", style="bold #fab283" if not is_err else err_color)
            header.append(cmd, style="#eeeeee" if not is_err else err_color)
            if rec.duration_ms:
                header.append(f"  ({rec.duration_ms}ms)", style="#585858")
            if rec.pid:
                if self.stop_state == "stopped":
                    header.append(f"  ⏹ stopped (pid {rec.pid})", style="#808080")
                else:
                    header.append(f"  ⏹ stop (pid {rec.pid})", style="bold #e06c75")
            if not self.expanded or not rec.output:
                return Group(header)
            body = Text()
            out_lines = rec.output.splitlines()[:24]
            for idx, raw_line in enumerate(out_lines):
                body.append("  │ ", style="#3a3a3a")
                body.append(raw_line, style=err_color if is_err else "#9a9a9a")
                if idx < len(out_lines) - 1:
                    body.append("\n")
            return Group(header, body)

        fallback = Text(f"{'✗' if is_err else '•'} {name}: {rec.summary}", style=err_color if is_err else "#808080")
        return Group(fallback)

    def _format_label(self, name: str, args: dict) -> str:
        if name == "read":
            return f"Reading {args.get('filePath', '')}"
        if name in ("write", "edit", "multi_edit", "apply_patch"):
            return f"Writing {args.get('filePath', '')}"
        if name == "glob":
            return f"Searching files {args.get('pattern', '*')}"
        if name == "grep":
            return f"Searching content {args.get('pattern', '')}"
        if name == "webfetch":
            return f"Fetching {args.get('url', '')}"
        if name == "bash":
            return f"Running {args.get('command', '')[:40]}"
        if name == "todowrite":
            return "Updating todos"
        if name in ("symbols", "diagnostics", "lint", "git", "subagent"):
            return f"Running {name}"
        return name


class AssistantMessageCard(Vertical):
    """Assistant response: clean Markdown + '▣ Build · model · 1.2s' footer, with live streaming updates."""

    def __init__(
        self,
        msg: ChatMessage,
        extra_models: list[ModelInfo] | None = None,
        live: bool = False,
    ) -> None:
        super().__init__(classes="assistant-card")
        self.msg = msg
        self.extra_models = extra_models
        self._live = live

    def compose(self) -> ComposeResult:
        yield Markdown(self.msg.content, classes="assistant-md")
        yield Static(self._build_meta_text(), classes="assistant-meta")

    def _build_meta_text(self) -> Text:
        info = get_model_info(self.msg.model, self.extra_models)
        meta = Text()
        meta.append("▣ ", style=f"bold {agent_accent(self.msg.agent)}")
        if self.msg.duration_s > 0:
            suffix = f" · {self.msg.duration_s:.1f}s"
        elif self._live:
            suffix = " · streaming..."
        else:
            suffix = ""
        meta.append(f"{self.msg.agent.capitalize()} · {info.name}{suffix}", style="#808080")
        if self.msg.interrupted:
            meta.append(" · interrupted", style="italic #e5c07b")
        return meta

    async def update_stream(self, new_markdown: str) -> None:
        self.msg.content = new_markdown
        try:
            md = self.query_one(Markdown)
        except NoMatches:
            return
        await md.update(new_markdown)

    async def finalize(self, final_msg: ChatMessage) -> None:
        self.msg = final_msg
        self._live = False
        try:
            md = self.query_one(Markdown)
            meta = self.query_one(".assistant-meta", Static)
        except NoMatches:
            return
        await md.update(final_msg.content)
        meta.update(self._build_meta_text())


class ChatScroll(VerticalScroll):
    """Chat log that follows new output only while the user sits at the bottom."""

    stuck_to_bottom: bool = True

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        max_y = self.max_scroll_y
        self.stuck_to_bottom = new_value >= max(0.0, max_y - 4)

    def follow_end(self) -> None:
        if self.stuck_to_bottom:
            self.scroll_end(animate=False)


class SidebarPanel(Vertical):
    """Right sidebar: flat #141414 surface with Context, MCP, LSP, Todo, Modified Files."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(id="sidebar")
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        yield Static(id="sb-title", classes="sb-block")
        yield Static(id="sb-context", classes="sb-block")
        yield Static(id="sb-mcp", classes="sb-block")
        yield Static(id="sb-lsp", classes="sb-block")
        yield Static(id="sb-ollama", classes="sb-block")
        yield Static(id="sb-todo", classes="sb-block")
        yield Static(id="sb-files", classes="sb-block")

    def refresh_all(
        self,
        session: Session,
        model_id: str,
        api_connected: bool,
        checkpoints_count: int = 0,
        extra_models: list[ModelInfo] | None = None,
        ollama_online: bool | None = None,
    ) -> None:
        info = get_model_info(model_id, extra_models)
        used = session.total_tokens
        pct = min(100, round((used / max(1, info.context_window)) * 100))
        cost = (used / 1000.0) * info.cost_per_1k

        # 1. Workspace
        title_txt = Text(self.workspace.name or str(self.workspace), style="bold #eeeeee")
        self.query_one("#sb-title", Static).update(title_txt)

        # 2. Context
        ctx = Text()
        ctx.append("Context\n", style="bold #eeeeee")
        ctx.append(f"{used:,} tokens\n", style="#808080")
        ctx.append(f"{pct}% used\n", style="#808080")
        ctx.append(f"${cost:.2f} spent", style="#808080")
        if checkpoints_count:
            ctx.append(f"\n{checkpoints_count} checkpoints", style="#fab283")
        self.query_one("#sb-context", Static).update(ctx)

        # 3. MCP
        mcp = Text()
        mcp.append("MCP\n", style="bold #eeeeee")
        mcp.append("• ", style="bold #7fd88f")
        mcp.append("rbit-tools ", style="#eeeeee")
        mcp.append("Connected", style="#808080")
        if api_connected:
            mcp.append("\n• ", style="bold #7fd88f")
            mcp.append("remote-llm ", style="#eeeeee")
            mcp.append("Streaming", style="#808080")
        self.query_one("#sb-mcp", Static).update(mcp)

        # 4. LSP
        lsp = Text()
        lsp.append("LSP\n", style="bold #eeeeee")
        lsp.append("• ", style="bold #7fd88f")
        lsp.append("python ", style="#eeeeee")
        lsp.append("Ready", style="#808080")
        self.query_one("#sb-lsp", Static).update(lsp)

        # 4b. Ollama
        local_models = [m for m in (extra_models or []) if "ollama" in m.provider.lower()]
        ollama = Text()
        ollama.append("Ollama\n", style="bold #eeeeee")
        if ollama_online is False:
            ollama.append("• ", style="bold #e06c75")
            ollama.append("offline ", style="#eeeeee")
            ollama.append("localhost:11434", style="#808080")
        else:
            ollama.append("• ", style="bold #7fd88f")
            ollama.append("online ", style="#eeeeee")
            count = len(local_models)
            ollama.append(f"{count} model{'s' if count != 1 else ''}", style="#808080")
        self.query_one("#sb-ollama", Static).update(ollama)

        # 5. Todo
        todo = Text()
        done = sum(1 for t in session.todos if t.status == "completed")
        if session.todos:
            todo.append(f"Todo ({done}/{len(session.todos)})\n", style="bold #eeeeee")
            for item in session.todos:
                if item.status == "completed":
                    todo.append("[✓] ", style="bold #7fd88f")
                    todo.append(f"{item.content}\n", style="strike #808080")
                elif item.status == "in_progress":
                    todo.append("[•] ", style="bold #fab283")
                    todo.append(f"{item.content}\n", style="#fab283")
                elif item.status == "cancelled":
                    todo.append("[✕] ", style="#e06c75")
                    todo.append(f"{item.content}\n", style="#606060")
                else:
                    todo.append("[ ] ", style="#686868")
                    todo.append(f"{item.content}\n", style="#808080")
        else:
            todo.append("Todo\n", style="bold #eeeeee")
            todo.append("No active tasks (/todo)", style="#686868")
        self.query_one("#sb-todo", Static).update(todo)

        # 6. Modified Files
        files = session.modified_files
        fblock = Text()
        fblock.append("Modified Files\n", style="bold #eeeeee")
        if not files:
            fblock.append("No modified files", style="#686868")
        else:
            for rel, stat in list(files.items())[-10:]:
                short_rel = rel if len(rel) <= 22 else "…" + rel[-21:]
                fblock.append(f"{short_rel:<23} ", style="#eeeeee")
                if stat.added:
                    fblock.append(f"+{stat.added}", style="#7fd88f")
                if stat.removed:
                    fblock.append(f" -{stat.removed}", style="#e06c75")
                fblock.append("\n")
        self.query_one("#sb-files", Static).update(fblock)


class AutocompletePopup(OptionList):
    """Floating completion menu for /commands and @files."""

    def __init__(self) -> None:
        super().__init__(id="autocomplete-popup")
        self.current_kind: str = ""
        self.values: list[str] = []

    def show_slash(self, prefix: str, extra: list[tuple[str, str]] | None = None) -> None:
        pool = list(SLASH_COMMANDS) + list(extra or [])
        matches = [
            (cmd, desc)
            for cmd, desc in pool
            if cmd.startswith(prefix.lower())
        ]
        if not matches:
            self.display = False
            return
        self.current_kind = "slash"
        self.values = [cmd for cmd, _ in matches]
        self.clear_options()
        for cmd, desc in matches[:10]:
            label = Text()
            label.append(f"{cmd:<14}", style="bold #eeeeee")
            label.append(desc, style="#808080")
            self.add_option(Option(label, id=cmd))
        self.highlighted = 0
        self.display = True

    def show_files(self, query: str, workspace_files: list[str]) -> None:
        q = query.lower()
        matches = [f for f in workspace_files if q in f.lower()][:10]
        if not matches:
            self.display = False
            return
        self.current_kind = "file"
        self.values = matches
        self.clear_options()
        for rel in matches:
            label = Text()
            label.append(f"@{rel}", style="#eeeeee")
            self.add_option(Option(label, id=rel))
        self.highlighted = 0
        self.display = True

    def accept_highlighted(self) -> tuple[str, str] | None:
        if not self.display or self.highlighted is None or not self.values:
            return None
        idx = self.highlighted
        if 0 <= idx < len(self.values):
            val = self.values[idx]
            kind = self.current_kind
            self.display = False
            return kind, val
        return None


class BottomStatusBar(Static):
    """1-line bottom footer: 'cwd:branch' on left, '• rbit v1.2.0' on right."""

    def __init__(self) -> None:
        super().__init__(id="status-bar")
        self._cwd = ""
        self._branch = ""

    def update_bar(self, workspace: Path, branch: str) -> None:
        self._cwd = str(workspace)
        self._branch = branch
        self.refresh()

    def render(self) -> Table:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right")

        left = Text()
        cwd = self._cwd
        home = str(Path.home())
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        left.append(cwd, style="#808080")
        if self._branch:
            left.append(f":{self._branch}", style="#686868")

        right = Text()
        right.append("• ", style="bold #7fd88f")
        right.append("rbit ", style="bold #eeeeee")
        right.append(f"v{__version__}", style="#808080")

        grid.add_row(left, right)
        return grid
