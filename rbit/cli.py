"""CLI entry point & subcommands for rbit: TUI, `run`, `doctor`, `stats`, `sessions`, `export`, `map`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .agent import AgentEngine, discover_ollama_models
from .app import RbitApp
from .models import ChatMessage, Session, ToolCallRecord, get_model_info, short_id
from .storage import StorageManager
from .tools import ToolExecutor
from .widgets import detect_git_branch


def _resolve_workspace(raw_dir: str | None) -> Path:
    return Path(raw_dir).resolve() if raw_dir else Path.cwd().resolve()


async def _run_headless(args: argparse.Namespace) -> int:
    output_format = getattr(args, "output_format", "text")
    jsonl_mode = output_format == "jsonl"
    json_mode = args.json or output_format == "json"
    console = Console(stderr=json_mode or jsonl_mode)
    workspace = _resolve_workspace(args.dir)
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    if getattr(args, "prompt_profile", None):
        cfg["prompt_profile"] = args.prompt_profile

    model_id = args.model or cfg.get("model", "rbit/smart-local")
    agent_mode = args.agent or "build"

    if args.continue_last:
        existing = storage.list_sessions()
        session = existing[0] if existing else Session()
    elif args.session:
        session = storage.find_session(args.session) or Session()
    else:
        session = Session()

    prompt_text = " ".join(args.prompt).strip()
    if getattr(args, "stdin", False):
        stdin_text = sys.stdin.read().strip()
        prompt_text = (prompt_text + "\n\n" + stdin_text).strip()
    elif not prompt_text and not sys.stdin.isatty():
        prompt_text = sys.stdin.read().strip()

    if not prompt_text:
        console.print("[bold #e06c75]Error:[/] Prompt is required for `rbit run`.")
        return 1

    if len(session.messages) == 0 and session.title == "New Session":
        session.title = prompt_text[:44]

    executor = ToolExecutor(workspace=workspace, session=session, storage=storage, get_config=lambda: cfg)
    executor.always_allow_bash = True  # Non-interactive CLI run
    executor.allow_outside_paths = bool(getattr(args, "allow_outside", False))
    executor.session_readonly = bool(getattr(args, "readonly", False))
    engine = AgentEngine(
        workspace=workspace,
        executor=executor,
        get_config=lambda: cfg,
        storage=storage,
    )
    await engine.refresh_ollama_models()

    user_msg = ChatMessage(
        id=short_id(),
        role="user",
        content=prompt_text,
        agent=agent_mode,
    )
    session.messages.append(user_msg)

    def _jsonl(payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    async def on_tool_start(rec: ToolCallRecord) -> None:
        if jsonl_mode:
            _jsonl({"event": "tool_start", "name": rec.name, "arguments": rec.arguments})
        elif not json_mode:
            console.print(f"[#808080]  ~ {rec.name}[/] [dim]{json.dumps(rec.arguments, ensure_ascii=False)}[/]")

    async def on_tool_end(rec: ToolCallRecord) -> None:
        if jsonl_mode:
            _jsonl(
                {
                    "event": "tool_end",
                    "name": rec.name,
                    "status": rec.status,
                    "summary": rec.summary,
                }
            )
        elif not json_mode:
            color = "#7fd88f" if rec.status == "success" else "#e06c75"
            console.print(f"[{color}]  ✓ {rec.name}[/] [#808080]{rec.summary}[/]")

    async def on_status(_txt: str) -> None:
        return

    reply = await engine.run_turn(
        session=session,
        user_msg=user_msg,
        agent_mode=agent_mode,
        model_id=model_id,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
        on_status=on_status,
    )
    session.messages.append(reply)
    storage.save_session(session)

    if jsonl_mode:
        _jsonl(
            {
                "event": "message",
                "role": "assistant",
                "content": reply.content,
                "model": reply.model,
                "agent": reply.agent,
                "tokens": reply.tokens,
            }
        )
        _jsonl(
            {
                "event": "done",
                "ok": True,
                "session_id": session.id,
                "duration_s": reply.duration_s,
                "tokens": reply.tokens,
            }
        )
    elif json_mode:
        out = {
            "session_id": session.id,
            "model": reply.model,
            "agent": reply.agent,
            "duration_s": reply.duration_s,
            "tokens": reply.tokens,
            "content": reply.content,
            "tool_calls": [tc.to_dict() for tc in reply.tool_calls],
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    else:
        console.print()
        console.print(RichMarkdown(reply.content))
        console.print(
            f"\n[#fab283]▣[/] [#808080]{reply.agent.capitalize()} · {reply.model} · {reply.duration_s:.2f}s · session {session.id}[/]"
        )
    return 0


async def _run_doctor(workspace: Path) -> int:
    console = Console()
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    branch = detect_git_branch(workspace)
    ollama_models = await discover_ollama_models()

    tbl = Table(title=f"rbit Doctor v{__version__}", border_style="#282828")
    tbl.add_column("Check", style="bold #eeeeee")
    tbl.add_column("Status", style="#7fd88f")
    tbl.add_column("Details", style="#808080")

    tbl.add_row("Python Runtime", "OK", f"{sys.version.split()[0]} ({platform.system()} {platform.release()})")
    tbl.add_row("Workspace", "OK", str(workspace))
    tbl.add_row("Git Repository", "OK" if branch else "NONE", branch or "Not inside a Git repository (Checkpoints still active)")
    tbl.add_row("Default Model", "OK", str(cfg.get("model")))
    has_key = bool((cfg.get("api_key") or "").strip())
    tbl.add_row(
        "Remote LLM API",
        "CONNECTED" if has_key else "LOCAL FALLBACK",
        f"{cfg.get('api_base')} ({'API key set' if has_key else 'No key — using rbit/smart-local'})",
    )
    tbl.add_row(
        "Local Ollama",
        f"{len(ollama_models)} FOUND" if ollama_models else "OFFLINE",
        ", ".join(m.name for m in ollama_models[:5]) or "localhost:11434 not running",
    )
    tbl.add_row(
        "Project Memory",
        "ACTIVE" if storage.load_project_memory() else "EMPTY",
        str(storage.memory_path),
    )
    tbl.add_row("Saved Sessions", "OK", f"{len(storage.list_sessions())} sessions · {len(storage.list_checkpoints())} checkpoints")

    console.print(tbl)
    return 0


def _run_stats(workspace: Path) -> int:
    console = Console()
    storage = StorageManager(workspace)
    sessions = storage.list_sessions()
    checkpoints = storage.list_checkpoints()

    total_msgs = sum(len(s.messages) for s in sessions)
    total_tokens = sum(s.total_tokens for s in sessions)
    total_tools = sum(len(m.tool_calls) for s in sessions for m in s.messages)
    modified_set = {f for s in sessions for f in s.modified_files.keys()}

    cost_est = 0.0
    for s in sessions:
        for m in s.messages:
            if m.role == "assistant":
                info = get_model_info(m.model)
                cost_est += (m.tokens / 1000.0) * info.cost_per_1k

    panel_body = (
        f"[bold #eeeeee]Sessions:[/]       {len(sessions)}\n"
        f"[bold #eeeeee]Messages:[/]       {total_msgs}\n"
        f"[bold #eeeeee]Tool Calls:[/]     {total_tools}\n"
        f"[bold #eeeeee]Total Tokens:[/]   {total_tokens:,}\n"
        f"[bold #eeeeee]Est. API Cost:[/]  ${cost_est:.4f}\n"
        f"[bold #eeeeee]Files Touched:[/]  {len(modified_set)}\n"
        f"[bold #eeeeee]Checkpoints:[/]    {len(checkpoints)}"
    )
    console.print(Panel(panel_body, title=f"[bold #fab283]rbit Workspace Stats[/] ({workspace.name})", border_style="#282828"))
    return 0


def _run_sessions_list(workspace: Path) -> int:
    console = Console()
    storage = StorageManager(workspace)
    sessions = storage.list_sessions()
    if not sessions:
        console.print("[#808080]No saved sessions in this workspace.[/]")
        return 0
    tbl = Table(title="Saved rbit Sessions", border_style="#282828")
    tbl.add_column("ID", style="bold #fab283")
    tbl.add_column("Title", style="#eeeeee")
    tbl.add_column("Messages", justify="right", style="#808080")
    tbl.add_column("Tokens", justify="right", style="#808080")
    tbl.add_column("Created", style="#808080")
    for s in sessions:
        tbl.add_row(s.id, s.title, str(len(s.messages)), f"{s.total_tokens:,}", s.created_at)
    console.print(tbl)
    return 0


def _run_export(workspace: Path, session_id: str | None, out_file: str | None) -> int:
    console = Console()
    storage = StorageManager(workspace)
    sessions = storage.list_sessions()
    if not sessions:
        console.print("[bold #e06c75]No sessions available to export.[/]")
        return 1
    target = storage.find_session(session_id) if session_id else sessions[0]
    if not target:
        console.print(f"[bold #e06c75]Session '{session_id}' not found.[/]")
        return 1
    out_path = Path(out_file).resolve() if out_file else None
    saved = storage.export_session_markdown(target, out_path)
    console.print(f"[bold #7fd88f]✓ Exported session `{target.id}` to:[/] {saved}")
    return 0


def _run_map(workspace: Path, max_tokens: int | None = None, as_json: bool = False) -> int:
    from .tools import IGNORED_DIRS

    lang_by_ext = {
        ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
        ".ts": "TypeScript", ".tsx": "TypeScript", ".rs": "Rust", ".go": "Go",
        ".java": "Java", ".kt": "Kotlin", ".c": "C", ".h": "C", ".cpp": "C++",
        ".hpp": "C++", ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".sh": "Shell",
        ".ps1": "PowerShell", ".sql": "SQL", ".html": "HTML", ".css": "CSS",
        ".scss": "SCSS", ".md": "Markdown", ".json": "JSON", ".toml": "TOML",
        ".yaml": "YAML", ".yml": "YAML", ".svg": "SVG", ".txt": "Text",
    }
    files: list[dict[str, Any]] = []
    languages: dict[str, dict[str, int]] = {}
    total_bytes = 0
    for root, dirs, names in os.walk(workspace):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        for name in sorted(names):
            path = Path(root) / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            rel = path.relative_to(workspace).as_posix()
            lang = lang_by_ext.get(path.suffix.lower(), "Other")
            files.append({"path": rel, "bytes": size, "language": lang, "est_tokens": size // 4})
            total_bytes += size
            bucket = languages.setdefault(lang, {"files": 0, "bytes": 0})
            bucket["files"] += 1
            bucket["bytes"] += size

    files.sort(key=lambda f: f["bytes"], reverse=True)
    listed = files
    truncated = False
    if max_tokens and max_tokens > 0:
        budget = 0
        kept: list[dict[str, Any]] = []
        for item in files:
            if budget + item["est_tokens"] > max_tokens and kept:
                truncated = True
                break
            kept.append(item)
            budget += item["est_tokens"]
        listed = kept

    payload = {
        "workspace": str(workspace),
        "total_bytes": total_bytes,
        "est_tokens": total_bytes // 4,
        "file_count": len(files),
        "languages": languages,
        "files": listed,
        "truncated": truncated,
    }

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    console = Console()
    kib = total_bytes / 1024
    console.print(
        f"[bold #fab283]rbit map[/] [#808080]{len(files)} files · {kib:,.1f} KB · "
        f"~{total_bytes // 4:,} tokens[/]"
    )
    if languages:
        parts = [
            f"{lang} {meta['files']} ({meta['bytes'] / 1024:,.1f} KB)"
            for lang, meta in sorted(languages.items(), key=lambda kv: kv[1]["bytes"], reverse=True)
        ]
        console.print(f"[#5c9cf5]Languages:[/] [#eeeeee]{' · '.join(parts[:8])}[/]")
    if listed:
        table = Table(show_header=True, header_style="#5c9cf5", box=None)
        table.add_column("File", style="#eeeeee")
        table.add_column("Size", justify="right", style="#808080")
        table.add_column("~Tokens", justify="right", style="#808080")
        table.add_column("Lang", style="#808080")
        for item in listed[:20]:
            table.add_row(
                item["path"],
                f"{item['bytes'] / 1024:,.1f} KB",
                f"{item['est_tokens']:,}",
                item["language"],
            )
        console.print(table)
    if truncated:
        console.print(f"[#808080]... truncated to ~{max_tokens:,} token budget[/]")
    return 0


def _run_lint(workspace: Path, as_json: bool = False) -> int:
    executor = ToolExecutor(workspace=workspace, session=Session())
    ruff = shutil.which("ruff")
    output = ""
    ok = True
    if ruff:
        proc = subprocess.run(
            [ruff, "check", "--output-format", "concise", "."],
            cwd=str(workspace),
            capture_output=True,
            text=True,
        )
        ok = proc.returncode == 0
        output = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
        output = output or "No lint issues."
    else:
        res = executor._tool_diagnostics({"filePath": "."})
        ok = res.ok
        output = res.output

    issues = [
        line
        for line in output.splitlines()
        if line.strip() and not line.lower().startswith(("no lint issues", "no syntax issues"))
    ]
    payload = {
        "workspace": str(workspace),
        "linter": "ruff" if ruff else "builtin-diagnostics",
        "ok": ok,
        "issue_count": len(issues),
        "output": output,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        console = Console()
        console.print(
            f"[bold #fab283]rbit lint[/] [#808080]{len(issues)} issue(s) · "
            f"{'ruff' if ruff else 'builtin syntax diagnostics'}[/]"
        )
        if issues:
            console.print(output)
    return 0 if ok else 1


def _parse_version(text: str) -> tuple[int, ...]:
    """Parses a dotted version like '1.2.3' into a comparable tuple."""
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", text.strip().lstrip("v")):
        match = re.match(r"(\d+)", chunk)
        if match:
            parts.append(int(match.group(1)))
        else:
            break
    return tuple(parts) or (0,)


def _fetch_latest_pypi_version(name: str = "rbit", timeout: float = 4.0) -> str | None:
    """Looks up the newest release on PyPI; returns None when offline or unpublished."""
    import httpx

    try:
        resp = httpx.get(f"https://pypi.org/pypi/{name}/json", timeout=timeout)
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    try:
        return str(resp.json()["info"]["version"])
    except Exception:
        return None


def _run_update(check_only: bool = False, as_json: bool = False) -> int:
    """Checks PyPI for a newer release and optionally upgrades the installed package."""
    console = Console()
    latest = _fetch_latest_pypi_version()
    current = __version__
    if latest is None:
        payload = {
            "status": "unavailable",
            "current": current,
            "latest": None,
            "up_to_date": False,
            "message": "Could not reach PyPI (offline or the package is not published yet).",
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            console.print(
                "[bold #fab283]rbit update[/] [#808080]could not reach PyPI — "
                "check your network or install from source.[/]"
            )
        return 1

    up_to_date = _parse_version(latest) <= _parse_version(current)
    payload = {
        "status": "up-to-date" if up_to_date else "update-available",
        "current": current,
        "latest": latest,
        "up_to_date": up_to_date,
    }

    if up_to_date:
        payload["message"] = f"rbit v{current} is the latest release."
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            console.print(f"[bold #7fd88f]✓[/] rbit v{current} is up to date.")
        return 0

    if check_only:
        payload["message"] = f"rbit v{latest} is available (installed v{current})."
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            console.print(
                f"[bold #fab283]rbit update[/] v{latest} is available (installed v{current}). "
                "Run [bold]rbit update[/] without --check to install."
            )
        return 0

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "rbit"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    ok = proc.returncode == 0
    payload["status"] = "updated" if ok else "failed"
    payload["message"] = (
        f"Installed rbit v{latest}." if ok else (proc.stderr.strip()[-500:] or "pip install failed.")
    )
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if ok:
            console.print(f"[bold #7fd88f]✓[/] Updated rbit {current} → {latest}. Restart to use it.")
        else:
            console.print(f"[bold #e06c75]✗[/] Update failed: {payload['message']}")
    return 0 if ok else 1


def _parse_age_spec(spec: str) -> float:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", spec.lower())
    if not match:
        raise ValueError(f"invalid age '{spec}' (use e.g. 30d, 12h, 2w)")
    value = int(match.group(1))
    unit = match.group(2) or "d"
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return float(value * factor)


def _run_sessions_prune(workspace: Path, age_spec: str) -> int:
    console = Console()
    storage = StorageManager(workspace)
    storage.ensure_dirs()
    try:
        seconds = _parse_age_spec(age_spec)
    except ValueError as exc:
        console.print(f"[bold #e06c75]{exc}[/]")
        return 2

    cutoff = time.time() - seconds
    removed_sessions = 0
    for path in storage.sessions_dir.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed_sessions += 1
        except OSError:
            continue

    removed_checkpoints = 0
    for path in storage.checkpoints_dir.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed_checkpoints += 1
        except OSError:
            continue

    console.print(
        f"[bold #7fd88f]Pruned[/] [#eeeeee]older than {age_spec}:[/] "
        f"{removed_sessions} session(s), {removed_checkpoints} checkpoint(s)."
    )
    return 0


def _run_init(workspace: Path, force: bool = False, as_json: bool = False) -> int:
    from .tools import IGNORED_DIRS, GitIgnoreMatcher

    console = Console()
    ignore = GitIgnoreMatcher(workspace)
    lang_by_ext = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
        ".rs": "Rust", ".go": "Go", ".java": "Java", ".rb": "Ruby", ".php": "PHP",
        ".cs": "C#", ".c": "C", ".cpp": "C++", ".md": "Markdown", ".html": "HTML",
        ".css": "CSS", ".sh": "Shell", ".ps1": "PowerShell",
    }
    languages: dict[str, int] = {}
    file_count = 0
    for root, dirs, names in os.walk(workspace):
        root_rel = Path(root).relative_to(workspace).as_posix() if root != str(workspace) else ""
        dirs[:] = [
            d
            for d in dirs
            if d not in IGNORED_DIRS and not ignore.is_ignored(f"{root_rel}/{d}" if root_rel else d, is_dir=True)
        ]
        for name in names:
            rel = f"{root_rel}/{name}" if root_rel else name
            if ignore.is_ignored(rel):
                continue
            file_count += 1
            lang = lang_by_ext.get(Path(name).suffix.lower())
            if lang:
                languages[lang] = languages.get(lang, 0) + 1

    commands: list[str] = []
    if (workspace / "tests").is_dir() or (workspace / "pytest.ini").exists() or (workspace / "pyproject.toml").exists():
        commands.append("- Tests: `pytest -q`")
    if (workspace / "package.json").exists():
        commands.append("- JS tests: `npm test`")
    if (workspace / "pyproject.toml").exists() and (workspace / "rbit").is_dir():
        commands.append("- Lint: `ruff check .`")
    if not commands:
        commands.append("- Add your build/test commands here")

    langs_line = ", ".join(
        f"{lang} ({count})" for lang, count in sorted(languages.items(), key=lambda kv: kv[1], reverse=True)[:8]
    ) or "unknown"
    title = workspace.name or "project"
    rbit_md = (
        f"# {title}\n\n"
        "Project rules and context for `rbit` — loaded into every session.\n\n"
        "## Overview\n"
        f"- Files: {file_count}\n"
        f"- Languages: {langs_line}\n"
        "- Source root: `.`\n\n"
        "## Commands\n"
        + "\n".join(commands)
        + "\n\n## Conventions\n"
        "- Keep changes focused; prefer small edits over rewrites.\n"
        "- Run relevant tests/lint before finishing a task.\n\n"
        "## Notes\n"
        "- Add durable project facts with `/remember <rule>`.\n"
    )
    memory = (
        "# rbit Project Memory\n\n"
        f"- Project: {title}\n"
        "- Add persistent rules here or via `/remember`.\n"
    )

    rbit_path = workspace / "RBIT.md"
    storage = StorageManager(workspace)
    storage.ensure_dirs()
    written: list[str] = []
    skipped: list[str] = []
    for path, content in ((rbit_path, rbit_md), (storage.memory_path, memory)):
        if path.exists() and not force:
            skipped.append(str(path))
            continue
        path.write_text(content, encoding="utf-8")
        written.append(str(path))

    if as_json:
        print(json.dumps({"written": written, "skipped": skipped, "file_count": file_count}, indent=2))
    else:
        if written:
            console.print("[bold #7fd88f]Initialized:[/] " + ", ".join(f"`{Path(p).name}`" for p in written))
        if skipped:
            console.print("[#808080]Skipped existing (use --force to overwrite): " + ", ".join(skipped) + "[/]")
    return 0


COMMANDS = "run doctor stats sessions export map lint init commit update"

COMPLETION_SCRIPTS: dict[str, str] = {
    "bash": (
        "_rbit_completion() {\n"
        f"  COMPREPLY=( $(compgen -W \"{COMMANDS} --help --version --generate-completion\" "
        "-- \"${COMP_WORDS[COMP_CWORD]}\") )\n"
        "}\n"
        "complete -F _rbit_completion rbit\n"
    ),
    "zsh": (
        "#compdef rbit\n"
        f"_arguments '1:command:({COMMANDS})' '*::arg:->args'\n"
    ),
    "powershell": (
        "Register-ArgumentCompleter -Native -CommandName rbit -ScriptBlock {\n"
        "    param($wordToComplete, $commandAst, $cursorPosition)\n"
        f"    '{COMMANDS}'.Split(' ') | Where-Object {{ $_ -like \"$wordToComplete*\" }} | "
        "ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }\n"
        "}\n"
    ),
}


def _completion_script(shell: str) -> str:
    return COMPLETION_SCRIPTS.get(shell, COMPLETION_SCRIPTS["bash"])


def _run_commit(workspace: Path, apply: bool = False, message: str | None = None, as_json: bool = False) -> int:
    console = Console()

    def git(*git_args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *git_args], cwd=str(workspace), capture_output=True, text=True
        )

    status = git("status", "--short").stdout.strip()
    stat = "\n".join(
        part for part in (git("diff", "--stat").stdout.strip(), git("diff", "--staged", "--stat").stdout.strip()) if part
    )
    diff = "\n".join(
        part for part in (git("diff").stdout.strip(), git("diff", "--staged").stdout.strip()) if part
    )
    if not status and not diff:
        if as_json:
            print(json.dumps({"ok": False, "error": "nothing to commit"}, ensure_ascii=False))
        else:
            console.print("[#808080]Nothing to commit — working tree is clean.[/]")
        return 1

    from .agent import AgentEngine

    draft = (message or "").strip() or AgentEngine._heuristic_commit_message(stat or status, diff or status)

    if not apply:
        if as_json:
            print(json.dumps({"ok": True, "applied": False, "message": draft}, ensure_ascii=False, indent=2))
        else:
            console.print("[bold #fab283]Proposed commit message[/] [#808080](use --apply to commit)[/]")
            console.print(draft)
        return 0

    add = git("add", "-A")
    if add.returncode != 0:
        console.print(f"[bold #e06c75]git add failed:[/] {add.stderr.strip()}")
        return 1
    commit = git("commit", "-m", draft)
    if commit.returncode != 0:
        console.print(f"[bold #e06c75]git commit failed:[/] {commit.stderr.strip()}")
        return 1

    subject = draft.splitlines()[0]
    if as_json:
        print(json.dumps({"ok": True, "applied": True, "message": draft}, ensure_ascii=False, indent=2))
    else:
        console.print(f"[bold #7fd88f]Committed:[/] {subject}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    log_level: int | None = None
    for flag, level in (
        ("--quiet", logging.WARNING),
        ("--verbose", logging.INFO),
        ("--trace", logging.DEBUG),
    ):
        if flag in args_list:
            args_list = [item for item in args_list if item != flag]
            log_level = level
    if log_level is not None:
        logging.basicConfig(
            stream=sys.stderr,
            level=log_level,
            format="%(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger().setLevel(log_level)

    if "--profile" in args_list:
        idx = args_list.index("--profile")
        if idx + 1 >= len(args_list) or args_list[idx + 1].startswith("-"):
            Console(stderr=True).print("--profile requires a name (e.g. work, oss, local)")
            return 2
        os.environ["RBIT_PROFILE"] = args_list[idx + 1]
        args_list = args_list[:idx] + args_list[idx + 2:]

    allow_outside = "--allow-outside" in args_list
    if allow_outside:
        args_list = [arg for arg in args_list if arg != "--allow-outside"]

    if args_list and args_list[0] == "--generate-completion":
        shell = args_list[1].lower() if len(args_list) > 1 else "bash"
        if shell not in COMPLETION_SCRIPTS:
            Console(stderr=True).print(f"Unsupported shell '{shell}'. Use bash, zsh, or powershell.")
            return 2
        print(_completion_script(shell), end="")
        return 0

    # Ensure UTF-8 stdout/stderr on Windows consoles
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    subcommands = {"run", "doctor", "stats", "sessions", "export", "map", "lint", "init", "commit", "update", "backup", "describe", "serve"}
    if args_list and args_list[0] in subcommands:
        sub = args_list[0]
        rest = args_list[1:]

        if sub == "run":
            p = argparse.ArgumentParser(prog="rbit run", description="Run rbit non-interactively in headless mode")
            p.add_argument("prompt", nargs="*", help="Prompt to execute")
            p.add_argument("-m", "--model", help="Model ID")
            p.add_argument(
                "--prompt-profile",
                choices=["default", "fast", "careful", "reviewer", "docs"],
                default=None,
                help="System prompt profile: fast | careful | reviewer | docs",
            )
            p.add_argument("-a", "--agent", choices=["build", "plan"], default="build", help="Agent mode")
            p.add_argument("-c", "--continue", dest="continue_last", action="store_true", help="Continue latest session")
            p.add_argument("-s", "--session", help="Continue specific session ID")
            p.add_argument("-d", "--dir", help="Target workspace directory")
            p.add_argument("--stdin", action="store_true", help="Read the prompt (or extra context) from stdin")
            p.add_argument(
                "--allow-outside",
                action="store_true",
                help="Allow file tools to touch paths outside the workspace",
            )
            p.add_argument(
                "--readonly",
                action="store_true",
                help="Read-only session: mutating tools and bash are blocked",
            )
            p.add_argument(
                "--output-format",
                choices=["text", "json", "jsonl"],
                default="text",
                help="Output mode: text, json, or event stream jsonl",
            )
            p.add_argument("--json", action="store_true", help="Emit structured JSON output")
            parsed = p.parse_args(rest)
            if allow_outside:
                parsed.allow_outside = True
            return asyncio.run(_run_headless(parsed))

        if sub == "doctor":
            p = argparse.ArgumentParser(prog="rbit doctor")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            parsed = p.parse_args(rest)
            return asyncio.run(_run_doctor(_resolve_workspace(parsed.dir)))

        if sub == "serve":
            p = argparse.ArgumentParser(prog="rbit serve", description="Local HTTP endpoint exposing rbit turns as NDJSON")
            p.add_argument("--port", type=int, default=8333)
            p.add_argument("-d", "--dir", help="Workspace directory")
            p.add_argument("--allow-outside", action="store_true")
            parsed = p.parse_args(rest)
            return _run_serve(parsed)

        if sub == "backup":
            p = argparse.ArgumentParser(prog="rbit backup", description="Archive the .rbit state directory")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("-o", "--output", help="Output zip path (default rbit-backup-<ts>.zip)")
            parsed = p.parse_args(rest)
            workspace = _resolve_workspace(parsed.dir)
            import zipfile

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out = Path(parsed.output) if parsed.output else workspace / f"rbit-backup-{stamp}.zip"
            state_dir = workspace / ".rbit"
            if not state_dir.exists():
                Console().print("No .rbit directory to back up.")
                return 1
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(state_dir.rglob("*")):
                    if f.is_file():
                        zf.write(f, f.relative_to(workspace))
            Console().print(f"Backed up .rbit → {out}")
            return 0

        if sub == "describe":
            p = argparse.ArgumentParser(prog="rbit describe", description="Describe the project structure and README")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            parsed = p.parse_args(rest)
            workspace = _resolve_workspace(parsed.dir)
            from rbit.tools import ToolExecutor as _TE
            from rbit.models import Session as _Session

            executor = _TE(workspace=workspace, session=_Session())
            files = executor.list_workspace_files(limit=60)
            exts: dict[str, int] = {}
            for f in files:
                exts[Path(f).suffix.lower() or "(none)"] = exts.get(Path(f).suffix.lower() or "(none)", 0) + 1
            top = sorted(exts.items(), key=lambda kv: -kv[1])[:6]
            readme = next(
                (workspace / n for n in ("README.md", "readme.md", "README.rst", "README.txt") if (workspace / n).exists()),
                None,
            )
            print(f"Project: {workspace.name}")
            print(f"Files (top {min(60, len(files))}): {len(files)}")
            print("Languages:", ", ".join(f"{k or 'plain'}×{v}" for k, v in top) or "n/a")
            if readme is not None:
                head = readme.read_text(encoding="utf-8", errors="replace").splitlines()
                head = [ln for ln in head if ln.strip()][:6]
                print(f"--- {readme.name} (head) ---")
                print("\n".join(head))
            else:
                print("No README found.")
            return 0

        if sub == "stats":
            p = argparse.ArgumentParser(prog="rbit stats")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_stats(_resolve_workspace(parsed.dir))

        if sub == "sessions":
            p = argparse.ArgumentParser(prog="rbit sessions")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--prune", metavar="AGE", help="Delete sessions/checkpoints older than e.g. 30d, 12h, 2w")
            parsed = p.parse_args(rest)
            workspace = _resolve_workspace(parsed.dir)
            if parsed.prune:
                return _run_sessions_prune(workspace, parsed.prune)
            return _run_sessions_list(workspace)

        if sub == "export":
            p = argparse.ArgumentParser(prog="rbit export")
            p.add_argument("session_id", nargs="?", help="Session ID (defaults to latest)")
            p.add_argument("-o", "--output", help="Output Markdown path")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_export(_resolve_workspace(parsed.dir), parsed.session_id, parsed.output)

        if sub == "map":
            p = argparse.ArgumentParser(prog="rbit map", description="Show a token-budgeted repository map")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--max-tokens", type=int, default=None, help="Token budget for listed files")
            p.add_argument("--json", action="store_true", help="Emit structured JSON output")
            parsed = p.parse_args(rest)
            return _run_map(_resolve_workspace(parsed.dir), parsed.max_tokens, parsed.json)

        if sub == "lint":
            p = argparse.ArgumentParser(prog="rbit lint", description="Lint a workspace with ruff or built-in diagnostics")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--json", action="store_true", help="Emit structured JSON output")
            parsed = p.parse_args(rest)
            return _run_lint(_resolve_workspace(parsed.dir), parsed.json)

        if sub == "init":
            p = argparse.ArgumentParser(prog="rbit init", description="Create RBIT.md and .rbit/memory.md from project analysis")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--force", action="store_true", help="Overwrite existing RBIT.md / memory.md")
            p.add_argument("--json", action="store_true", help="Emit structured JSON output")
            parsed = p.parse_args(rest)
            return _run_init(_resolve_workspace(parsed.dir), parsed.force, parsed.json)

        if sub == "commit":
            p = argparse.ArgumentParser(prog="rbit commit", description="Draft a commit message from git diff")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("-m", "--message", help="Override the generated message")
            p.add_argument("--apply", action="store_true", help="Stage all changes and create the commit")
            p.add_argument("--json", action="store_true", help="Emit structured JSON output")
            parsed = p.parse_args(rest)
            return _run_commit(_resolve_workspace(parsed.dir), parsed.apply, parsed.message, parsed.json)

        if sub == "update":
            p = argparse.ArgumentParser(prog="rbit update", description="Check PyPI and install a newer rbit")
            p.add_argument("--check", action="store_true", help="Only report whether an update exists")
            p.add_argument("--json", action="store_true", help="Emit structured JSON output")
            parsed = p.parse_args(rest)
            return _run_update(parsed.check, parsed.json)

    # Default: Interactive Full-Screen TUI mode
    parser = argparse.ArgumentParser(
        prog="rbit",
        description="rbit — Next-Gen Full-Screen Terminal AI Coding Agent & CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "subcommands:\n"
            "  rbit run [prompt]      Run non-interactively in headless CLI/CI mode (--json, --stdin)\n"
            "  rbit doctor [dir]      Diagnose environment, LLM API, Ollama & workspace health\n"
            "  rbit stats [dir]       Show token, cost & tool analytics across sessions\n"
            "  rbit sessions [dir]    List saved sessions in workspace\n"
            "  rbit sessions --prune 30d   Delete sessions & checkpoints older than AGE\n"
            "  rbit export [id]       Export session transcript to Markdown\n"
            "  rbit map [dir]         Repository map with sizes, languages & token budget\n"
            "  rbit lint [dir]        Lint workspace; exit code 1 on issues (--json)\n"
            "  rbit init [dir]        Create RBIT.md + .rbit/memory.md from project scan\n"
            "  rbit commit [dir]      Draft a commit message from git diff (--apply to commit)\n"
            "  rbit update            Check PyPI for a newer release (--check to skip install)\n"
            "  rbit --generate-completion bash|zsh|powershell   Print shell completion script\n"            "\n"
            "Global flags: --quiet (warnings only), --verbose (info), --trace (debug)\n"
            "              --profile <name> applies a named overlay from config.json\n"
            "              --allow-outside permits file access outside the workspace"
        ),
    )
    parser.add_argument("target", nargs="?", help="Workspace directory path (defaults to current directory)")
    parser.add_argument(
        "--allow-outside",
        action="store_true",
        help="Allow file tools to read/write paths outside the workspace",
    )
    parser.add_argument("-p", "--prompt", help="Start TUI and immediately submit this prompt")
    parser.add_argument("-c", "--continue", dest="continue_last", action="store_true", help="Continue most recent session")
    parser.add_argument("-s", "--session", help="Resume specific session by ID")
    parser.add_argument("-m", "--model", help="Override active model ID")
    parser.add_argument("-a", "--agent", choices=["build", "plan"], default="build", help="Initial agent mode")
    parser.add_argument(
        "-e",
        "--effort",
        choices=["auto", "low", "medium", "high"],
        help="Reasoning effort for reasoning models (o-series, DeepSeek R, QwQ)",
    )
    parser.add_argument("-v", "--version", action="store_true", help="Print rbit version and exit")

    parsed = parser.parse_args(args_list)
    if parsed.version:
        print(f"rbit v{__version__}")
        return 0

    workspace = _resolve_workspace(parsed.target)
    storage = StorageManager(workspace)

    init_session: Session | None = None
    if parsed.continue_last:
        sessions = storage.list_sessions()
        if sessions:
            init_session = sessions[0]
    elif parsed.session:
        init_session = storage.find_session(parsed.session)

    app = RbitApp(
        workspace=workspace,
        initial_session=init_session,
        initial_prompt=parsed.prompt,
        initial_agent=parsed.agent,
        initial_model=parsed.model,
        initial_effort=parsed.effort,
        allow_outside_paths=allow_outside or bool(getattr(parsed, "allow_outside", False)),
    )
    app.run()
    return 0




def _run_serve(args: argparse.Namespace) -> int:
    """Local HTTP endpoint: POST /v1/turns streams a full agent turn as NDJSON."""
    import json as _json
    import queue as _queue
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from rbit.agent import AgentEngine
    from rbit.models import ChatMessage, Session, ToolCallRecord, short_id
    from rbit.tools import ToolExecutor

    workspace = _resolve_workspace(args.dir)
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    model_id = str(cfg.get("model", "rbit/smart-local"))
    sessions: dict[str, Session] = {}

    def run_turn_events(prompt_text: str, session: Session, sink: " _queue.Queue"):  # type: ignore[name-defined]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            executor = ToolExecutor(workspace=workspace, session=session, storage=storage, get_config=lambda: cfg)
            executor.always_allow_bash = True
            executor.allow_outside_paths = bool(getattr(args, "allow_outside", False))
            engine = AgentEngine(workspace=workspace, executor=executor, storage=storage, get_config=lambda: cfg)

            async def on_status(text: str) -> None:
                if text:
                    sink.put({"type": "status", "text": text})

            async def on_tool_start(rec: ToolCallRecord) -> None:
                sink.put({"type": "tool_call", "name": rec.name, "arguments": rec.arguments})

            async def on_tool_end(rec: ToolCallRecord) -> None:
                sink.put({"type": "tool_result", "name": rec.name, "summary": rec.summary})

            async def on_stream_chunk(partial: str) -> None:
                sink.put({"type": "delta", "text": partial})

            async def on_thinking(active: bool) -> None:
                sink.put({"type": "thinking", "active": active})

            async def on_reasoning_chunk(piece: str) -> None:
                sink.put({"type": "reasoning", "text": piece})

            async def _turn() -> None:
                user = ChatMessage(id=short_id(), role="user", content=prompt_text)
                session.messages.append(user)
                reply = await engine.run_turn(
                    session=session,
                    user_msg=user,
                    agent_mode="build",
                    model_id=model_id,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    on_status=on_status,
                    on_stream_chunk=on_stream_chunk,
                    on_thinking=on_thinking,
                    on_reasoning_chunk=on_reasoning_chunk,
                )
                sink.put({"type": "message", "content": reply.content, "session_id": session.id})

            loop.run_until_complete(_turn())
        except Exception as exc:  # noqa: BLE001 - surfaced to the HTTP client
            sink.put({"type": "error", "text": str(exc)})
        finally:
            sink.put(None)
            loop.close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # quiet
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                body = _json.dumps({"status": "ok", "workspace": str(workspace)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/turns":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                req = _json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self.send_response(400)
                self.end_headers()
                return
            prompt_text = str(req.get("prompt") or "").strip()
            if not prompt_text:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "prompt is required"}')
                return
            sid = str(req.get("session_id") or "")
            session = sessions.get(sid) or Session()
            sessions[session.id] = session

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            events: _queue.Queue = _queue.Queue()
            worker = _threading.Thread(target=run_turn_events, args=(prompt_text, session, events), daemon=True)
            worker.start()
            while True:
                item = events.get()
                if item is None:
                    break
                self.wfile.write((_json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    Console().print(f"rbit serve on http://127.0.0.1:{args.port}  (POST /v1/turns, GET /health)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
