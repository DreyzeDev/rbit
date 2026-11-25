"""Subagent definitions (`.rbit/agents/*.md`, global dirs, config `agents`).

A subagent description file may use simple frontmatter:

    ---
    description: Review PRs
    mode: read-only
    tools: read, grep, glob
    ---
    You are a meticulous reviewer...

The built-in `explore` agent is always available.  Custom agents are read-only
unless `mode: build` is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .skills import _parse_frontmatter

READONLY_TOOL_NAMES = {
    "read", "glob", "grep", "webfetch", "websearch", "symbols", "diagnostics", "lint", "git",
    "list_changes", "todowrite", "skill",
}

EXPLORE_PROMPT = (
    "You are an explore subagent inside rbit. You have ONLY read-only tools. "
    "{workspace}\n"
    "Investigate the user's goal and return a compact summary (max ~200 words) "
    "with concrete `file:line` references. Do not ask questions."
)


@dataclass
class SubagentDef:
    name: str
    description: str = ""
    prompt: str = ""
    tools: set[str] | None = None
    read_only: bool = True
    source: str = ""
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def builtin_subagents() -> dict[str, SubagentDef]:
    return {
        "explore": SubagentDef(
            name="explore",
            description="Read-only codebase exploration with file:line references.",
            prompt=EXPLORE_PROMPT,
            tools=set(READONLY_TOOL_NAMES),
            read_only=True,
        ),
        "researcher": SubagentDef(
            name="researcher",
            description="Read-only researcher: inspects codebase, docs, and web references.",
            prompt=EXPLORE_PROMPT,
            tools=set(READONLY_TOOL_NAMES),
            read_only=True,
        ),
        "reviewer": SubagentDef(
            name="reviewer",
            description="Read-only code reviewer: checks diffs, bugs, and security risks.",
            prompt=(
                "You are a code reviewer subagent inside rbit. {workspace}\n"
                "Inspect code and git diffs; report findings with file:line references. Never modify files."
            ),
            tools=set(READONLY_TOOL_NAMES),
            read_only=True,
        ),
        "coder": SubagentDef(
            name="coder",
            description="Implementation subagent: edits code and runs diagnostics.",
            prompt=(
                "You are a coder subagent inside rbit. {workspace}\n"
                "Implement the assigned scoped change cleanly and verify with diagnostics/lint."
            ),
            tools=set(READONLY_TOOL_NAMES) | {"write", "edit", "multi_edit", "apply_patch"},
            read_only=False,
        ),
        "tester": SubagentDef(
            name="tester",
            description="Testing subagent: writes and runs tests, reports failures.",
            prompt=(
                "You are a tester subagent inside rbit. {workspace}\n"
                "Run relevant tests and add regression tests for the assigned scope."
            ),
            tools=set(READONLY_TOOL_NAMES) | {"write", "edit", "bash"},
            read_only=False,
        ),
        "debugger": SubagentDef(
            name="debugger",
            description="Debugging subagent: reproduces errors, isolates root cause, and fixes.",
            prompt=(
                "You are a debugger subagent inside rbit. {workspace}\n"
                "Reproduce the failure, isolate root cause, apply a minimal fix, and verify."
            ),
            tools=set(READONLY_TOOL_NAMES) | {"write", "edit", "bash"},
            read_only=False,
        ),
    }


def _parse_tools(value: Any) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value]
    else:
        return None
    return {item for item in items if item} or None


def _definition(name: str, meta: dict[str, Any], body: str, source: str) -> SubagentDef:
    mode = str(meta.get("mode") or "").strip().lower()
    read_only = mode not in ("build", "primary", "all")
    tools = _parse_tools(meta.get("tools")) if isinstance(meta, dict) else None
    if tools is None and read_only:
        tools = set(READONLY_TOOL_NAMES)
    return SubagentDef(
        name=name,
        description=str(meta.get("description") or "").strip(),
        prompt=body.strip() or EXPLORE_PROMPT,
        tools=tools,
        read_only=read_only,
        source=source,
        model=str(meta.get("model") or "").strip(),
    )


def _load_file(path: Path) -> SubagentDef | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or (path.parent.name if path.name.lower() == "agent.md" else path.stem)
    if not name:
        return None
    return _definition(name, meta, body or text, str(path))


def _collect(directory: Path, agents: dict[str, SubagentDef], *, override: bool = False) -> None:
    """Project directories pass `override=True` so they win over global/built-in ones."""
    if not directory.is_dir():
        return
    seen: set[str] = set()
    for entry in sorted(directory.iterdir()):
        definition: SubagentDef | None = None
        if entry.is_file() and entry.suffix.lower() == ".md":
            definition = _load_file(entry)
        elif entry.is_dir():
            nested = next(
                (c for c in sorted(entry.iterdir()) if c.is_file() and c.name.lower() == "agent.md"),
                None,
            )
            if nested is not None:
                definition = _load_file(nested)
        if definition is None or definition.name.lower() in seen:
            continue
        seen.add(definition.name.lower())
        _store(agents, definition.name, definition, override=override)


def _store(agents: dict[str, SubagentDef], name: str, definition: SubagentDef, *, override: bool) -> None:
    """Agent names match case-insensitively, like skill names."""
    existing = next((key for key in agents if key.lower() == name.lower()), None)
    if existing is not None:
        if not override:
            return
        del agents[existing]
    agents[name] = definition


def global_agent_dirs() -> list[Path]:
    home = Path.home()
    dirs = [home / ".config" / "rbit" / "agents", home / ".rbit" / "agents"]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        dirs.insert(0, Path(xdg) / "rbit" / "agents")
    return dirs


def load_subagents(workspace: Path, config: dict[str, Any] | None = None) -> dict[str, SubagentDef]:
    agents = builtin_subagents()
    for directory in global_agent_dirs():
        _collect(directory, agents)
    _collect(workspace / ".rbit" / "agents", agents, override=True)
    _collect(workspace / ".rbit" / "agent", agents, override=True)
    entries = (config or {}).get("agents")
    if isinstance(entries, dict):
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            if not any(key in entry for key in ("description", "prompt", "mode", "tools", "model")):
                continue
            definition = _definition(str(name), entry, str(entry.get("prompt") or ""), "config")
            agents[str(name)] = definition
    return agents


def subagent_names(agents: dict[str, SubagentDef]) -> list[str]:
    return sorted(agents)
