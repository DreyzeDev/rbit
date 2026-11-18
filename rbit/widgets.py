"""Terminal UI widgets for rbit (Home View, Session View, Inline Tool Diff Cards, Flat Sidebar)."""

from __future__ import annotations

import random
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__
from .models import ChatMessage, ModelInfo, Session, ToolCallRecord, get_model_info
from .permissions import mode_short_label as permission_mode_label


SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/models", "List and switch AI models (incl. local Ollama)"),
    ("/themes", "Switch color theme (live preview)"),
    ("/effort", "Select reasoning effort (auto / low / medium / high)"),
    ("/permissions", "Choose what rbit may do without asking (ask / commands / edits / auto)"),
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


class PromptInput(Input):
    """Prompt input that accepts whole multi-line pastes.

    The stock Input keeps only the first line of a Paste event, silently
    truncating long prompts (code, logs, big messages) pasted from the
    clipboard. Lines are joined with spaces since the input is single-line."""

    def _on_paste(self, event: events.Paste) -> None:
        text = event.text
        if text.strip():
            line = " ".join(text.splitlines()) or text
            selection = self.selection
            if selection.is_empty:
                self.insert_text_at_cursor(line)
            else:
                self.replace(line, *selection)
            # Textual invokes _on_paste for every class in the MRO, so Input's
            # first-line-only handler would run after this one — blank the text
            # to make it a no-op.
            event.text = ""
        event.stop()

    def action_paste(self) -> None:
        clipboard = self.app.clipboard
        if not clipboard:
            return
        line = " ".join(clipboard.splitlines()) or clipboard
        start, end = self.selection
        self.replace(line, start, end)


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
        api_latency: float | None = None,
    ) -> None:
        info = get_model_info(model_id, extra_models)
        used = session.total_tokens
        pct = min(100, round((used / max(1, info.context_window)) * 100))
        cost = (used / 1000.0) * info.cost_per_1k
        self._title = session.title or "New Session"
        self._stats = f"{used:,}  {pct}% (${cost:.2f})"
        if api_latency is not None:
            self._stats += f" · api {api_latency:.1f}s"
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


def _image_halfblocks(path: Path, cols: int = 28) -> Text:
    """Renders an image as terminal halfblock art (requires Pillow)."""
    try:
        from PIL import Image
    except ImportError:
        return Text("(preview unavailable: pip install Pillow)", style="#8a8a8a")
    im = Image.open(path).convert("RGB")
    rows = max(8, cols // 2)
    im = im.resize((cols, rows * 2))
    px = im.load()
    t = Text()
    for y in range(0, rows * 2, 2):
        for x in range(cols):
            top = px[x, y]
            bottom = px[x, min(y + 1, rows * 2 - 1)]
            t.append(
                "▀",
                style=f"#{top[0]:02x}{top[1]:02x}{top[2]:02x} on #{bottom[0]:02x}{bottom[1]:02x}{bottom[2]:02x}",
            )
        t.append("\n")
    return t


class UserMessageCard(Static):
    """User message block: #141414 panel with thick agent-colored left border."""

    IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

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
            for att in self.msg.attachments:
                if Path(att).suffix.lower() in self.IMAGE_EXTS:
                    try:
                        parts.append(_image_halfblocks(Path(att)))
                    except Exception:
                        continue
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
        self._stream_timer: Any = None

    def compose(self) -> ComposeResult:
        yield Markdown(self.msg.content, classes="assistant-md")
        yield Static(self._build_meta_text(), classes="assistant-meta")

    def on_resize(self, _event: Any) -> None:
        """Re-pin when a finished card grows after being mounted.

        The card grows in stages: mount → compose → Markdown render → reflow of
        each table/fence. On a long session (~500 widgets) those stages span far
        more frames than any fixed timer, so a pin issued on a deadline fired
        *before* the answer reached its final height and the tail stayed below
        the fold.

        Live cards are skipped: their streaming flush already re-pins on every
        update, and resize fires on each of those, which would stack redundant
        scrolls mid-stream.
        """
        if self._live:
            return
        scroll = self.parent
        if isinstance(scroll, ChatScroll) and scroll.stuck_to_bottom:
            scroll.call_after_refresh(scroll.pin_to_end)

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
        if self.msg.pinned:
            meta.append(" 📌", style="#f5a742")
        if self.msg.tokens and info.cost_per_1k:
            meta.append(f" · ${self.msg.tokens / 1000 * info.cost_per_1k:.4f}", style="#808080")
        if self.msg.interrupted:
            meta.append(" · interrupted", style="italic #e5c07b")
        return meta

    async def update_stream(self, new_markdown: str) -> None:
        self.msg.content = new_markdown
        try:
            self.query_one(Markdown)
        except NoMatches:
            return
        if self._stream_timer is not None:
            return  # a flush is already scheduled; it renders the latest content
        # Markdown.update reparses the whole document on every call — coalesce
        # SSE deltas to ~10 Hz instead of re-rendering on each token.
        self._stream_timer = self.set_timer(0.1, self._flush_stream_update)

    async def _flush_stream_update(self) -> None:
        self._stream_timer = None
        try:
            md = self.query_one(Markdown)
        except NoMatches:
            return
        await md.update(self.msg.content)
        # Markdown.update reflows a frame later; re-pin only after it landed,
        # otherwise the freshly grown card pushes the tail out of view.
        try:
            scroll = self.parent
            if isinstance(scroll, ChatScroll):
                scroll.call_after_refresh(scroll.pin_to_end)
        except Exception:
            pass

    async def finalize(self, final_msg: ChatMessage) -> None:
        self.msg = final_msg
        self._live = False
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        try:
            md = self.query_one(Markdown)
            meta = self.query_one(".assistant-meta", Static)
        except NoMatches:
            # The card was mounted on the very last stream chunk and has not
            # composed its children yet. Returning here would leave the answer
            # unrendered, so retry once the children exist — `compose()` picks
            # up `self.msg` only for the *initial* frame, not late updates.
            self.call_after_refresh(lambda: self._run_coro_safely(self._finalize_retry()))
            return
        await md.update(final_msg.content)
        meta.update(self._build_meta_text())
        try:
            scroll = self.parent
            if isinstance(scroll, ChatScroll):
                # The turn is over: the user asked for this answer, so show it
                # even if they had scrolled up while the tools were running.
                scroll.pin_to_end(force=True)
        except Exception:
            pass

    async def _finalize_retry(self) -> None:
        """Second attempt at rendering the final answer after a late compose."""
        try:
            md = self.query_one(Markdown)
            meta = self.query_one(".assistant-meta", Static)
        except NoMatches:
            return
        await md.update(self.msg.content)
        meta.update(self._build_meta_text())
        try:
            scroll = self.parent
            if isinstance(scroll, ChatScroll):
                scroll.pin_to_end(force=True)
        except Exception:
            pass

    def _run_coro_safely(self, coro: Any) -> None:
        try:
            self.run_worker(coro, group="finalize-retry", exclusive=True)
        except Exception:
            pass


class ChatScroll(VerticalScroll):
    """Chat log that follows new output only while the user sits at the bottom."""

    stuck_to_bottom: bool = True

    #: How many extra frames to keep re-scrolling while Markdown reflows.
    PIN_RETRIES: int = 6

    #: Distance (rows) from the tail still considered "at the bottom".
    BOTTOM_TOLERANCE: float = 4.0

    def on_mount(self) -> None:
        self._pin_attempts = 0
        self._pinning = False
        self._pin_target: float | None = None

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        # Only a *user* scroll may disable following. Programmatic pins land on
        # an offset that was current when they were issued, while the content
        # has usually grown since — judging those as "the user scrolled up"
        # silently killed auto-follow for the rest of the turn, so the final
        # answer was appended below the fold and never seen.
        if self._pinning:
            return
        target = self._pin_target
        # A pin only excuses the *tail* offset it asked for. If the view moved
        # somewhere else entirely (the user scrolled up), that is a real user
        # action and follow must disengage.
        if target is not None and target > 0 and abs(new_value - target) < 2:
            self._pin_target = None
            return
        self._pin_target = None
        if self.max_scroll_y <= 0:
            self.stuck_to_bottom = True
            return
        self.stuck_to_bottom = new_value >= self.max_scroll_y - self.BOTTOM_TOLERANCE

    def follow_end(self) -> None:
        self.pin_to_end()

    def pin_to_end(self, force: bool = False) -> None:
        """Keeps the tail of the newest message inside the viewport.

        A single `scroll_end()` lands on the *previous* maximum scroll offset:
        mounting a card or growing a `Markdown` widget only reflows on the next
        frame, so the reply's last lines stayed below the fold (reproduced with
        a 16-row gap on a 20-chunk stream). Re-issuing the scroll after each
        refresh closes that gap; the retries absorb multi-frame Markdown
        reflow while tokens keep arriving.

        `force=True` re-engages following — used when the app itself starts a
        new turn, where the user clearly wants to see what happens next.
        """
        if force:
            self.stuck_to_bottom = True
        if not self.stuck_to_bottom:
            return
        self._scroll_to_tail()
        self._pin_attempts = 0
        self.call_after_refresh(self._apply_pin)

    def _scroll_to_tail(self) -> None:
        """Scrolls to the tail while marking the move as programmatic."""
        self._pin_target = self.max_scroll_y
        self._pinning = True
        try:
            self.scroll_end(animate=False)
        finally:
            self._pinning = False

    def reveal_tail(self) -> None:
        """Scrolls to the tail unconditionally, ignoring the follow flag.

        `pin_to_end()` deliberately respects `stuck_to_bottom` so it never fights
        a user who scrolled up. At the end of a turn that is exactly wrong: the
        user scrolled up while tools were running, and the answer they asked for
        must be shown. This bypasses the flag entirely.
        """
        self.stuck_to_bottom = True
        self._pin_target = self.max_scroll_y
        self._pinning = True
        try:
            self.scroll_end(animate=False)
        finally:
            self._pinning = False

    def _apply_pin(self) -> None:
        if not self.stuck_to_bottom:
            return
        self._scroll_to_tail()
        if self.scroll_y >= self.max_scroll_y - 1:
            return
        self._pin_attempts += 1
        if self._pin_attempts < self.PIN_RETRIES:
            self.call_after_refresh(self._apply_pin)



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
        mcp_status: dict[str, dict[str, Any]] | None = None,
        mcp_configs: dict[str, dict[str, Any]] | None = None,
        permission_mode: str = "",
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
        if permission_mode:
            accent = "#e06c75" if permission_mode == "auto" else "#fab283"
            ctx.append(f"\n{permission_mode_label(permission_mode)}", style=accent)
        if checkpoints_count:
            ctx.append(f"\n{checkpoints_count} checkpoints", style="#fab283")
        self.query_one("#sb-context", Static).update(ctx)

        # 3. MCP (real connected servers; /mcp manages them)
        mcp = Text()
        mcp.append("MCP\n", style="bold #eeeeee")
        configured = list((mcp_configs or {}).keys())
        if not configured:
            mcp.append("No servers (/mcp add)", style="#686868")
        else:
            for name in configured[:6]:
                status = (mcp_status or {}).get(name) or {}
                if status.get("connected"):
                    mcp.append("• ", style="bold #7fd88f")
                    mcp.append(f"{name} ", style="#eeeeee")
                    count = int(status.get("tools") or 0)
                    mcp.append(f"{count} tool{'s' if count != 1 else ''}", style="#808080")
                else:
                    mcp.append("• ", style="bold #e06c75")
                    mcp.append(f"{name} ", style="#eeeeee")
                    error = str(status.get("error") or "idle")
                    mcp.append(error[:16], style="#808080")
                mcp.append("\n")
            if len(configured) > 6:
                mcp.append(f"+{len(configured) - 6} more\n", style="#686868")
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

        # 6. Modified Files (click a file to open its session diff)
        files = session.modified_files
        if not files:
            fblock = Text()
            fblock.append("Modified Files\n", style="bold #eeeeee")
            fblock.append("No modified files", style="#686868")
            self.query_one("#sb-files", Static).update(fblock)
        else:
            lines = ["[bold #eeeeee]Modified Files[/]"]
            for rel, stat in list(files.items())[-10:]:
                short_rel = rel if len(rel) <= 22 else "…" + rel[-21:]
                safe = rel.replace("\\", "/").replace("'", "")
                line = f"[@click=app.open_file_diff('{safe}')]{short_rel}[/]"
                if stat.added:
                    line += f" [#7fd88f]+{stat.added}[/]"
                if stat.removed:
                    line += f" [#e06c75]-{stat.removed}[/]"
                lines.append(line)
            self.query_one("#sb-files", Static).update("\n".join(lines))


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
