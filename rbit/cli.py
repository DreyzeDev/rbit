"""CLI entry point & subcommands for rbit: TUI, `run`, `doctor`, `stats`, `sessions`, `export`, `map`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
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
    console = Console(stderr=args.json)
    workspace = _resolve_workspace(args.dir)
    storage = StorageManager(workspace)
    cfg = storage.load_config()

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
    if not prompt_text and not sys.stdin.isatty():
        prompt_text = sys.stdin.read().strip()

    if not prompt_text:
        console.print("[bold #e06c75]Error:[/] Prompt is required for `rbit run`.")
        return 1

    if len(session.messages) == 0 and session.title == "New Session":
        session.title = prompt_text[:44]

    executor = ToolExecutor(workspace=workspace, session=session, storage=storage, get_config=lambda: cfg)
    executor.always_allow_bash = True  # Non-interactive CLI run
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

    async def on_tool_start(rec: ToolCallRecord) -> None:
        if not args.json:
            console.print(f"[#808080]  ~ {rec.name}[/] [dim]{json.dumps(rec.arguments, ensure_ascii=False)}[/]")

    async def on_tool_end(rec: ToolCallRecord) -> None:
        if not args.json:
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

    if args.json:
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


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)

    # Ensure UTF-8 stdout/stderr on Windows consoles
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    subcommands = {"run", "doctor", "stats", "sessions", "export", "map"}
    if args_list and args_list[0] in subcommands:
        sub = args_list[0]
        rest = args_list[1:]

        if sub == "run":
            p = argparse.ArgumentParser(prog="rbit run", description="Run rbit non-interactively in headless mode")
            p.add_argument("prompt", nargs="*", help="Prompt to execute")
            p.add_argument("-m", "--model", help="Model ID")
            p.add_argument("-a", "--agent", choices=["build", "plan"], default="build", help="Agent mode")
            p.add_argument("-c", "--continue", dest="continue_last", action="store_true", help="Continue latest session")
            p.add_argument("-s", "--session", help="Continue specific session ID")
            p.add_argument("-d", "--dir", help="Target workspace directory")
            p.add_argument("--json", action="store_true", help="Emit structured JSON output")
            parsed = p.parse_args(rest)
            return asyncio.run(_run_headless(parsed))

        if sub == "doctor":
            p = argparse.ArgumentParser(prog="rbit doctor")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            parsed = p.parse_args(rest)
            return asyncio.run(_run_doctor(_resolve_workspace(parsed.dir)))

        if sub == "stats":
            p = argparse.ArgumentParser(prog="rbit stats")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_stats(_resolve_workspace(parsed.dir))

        if sub == "sessions":
            p = argparse.ArgumentParser(prog="rbit sessions")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_sessions_list(_resolve_workspace(parsed.dir))

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

    # Default: Interactive Full-Screen TUI mode
    parser = argparse.ArgumentParser(
        prog="rbit",
        description="rbit — Next-Gen Full-Screen Terminal AI Coding Agent & CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "subcommands:\n"
            "  rbit run [prompt]      Run non-interactively in headless CLI/CI mode (--json supported)\n"
            "  rbit doctor [dir]      Diagnose environment, LLM API, Ollama & workspace health\n"
            "  rbit stats [dir]       Show token, cost & tool analytics across sessions\n"
            "  rbit sessions [dir]    List saved sessions in workspace\n"
            "  rbit export [id]       Export session transcript to Markdown\n"
            "  rbit map [dir]         Repository map with sizes, languages & token budget"
        ),
    )
    parser.add_argument("target", nargs="?", help="Workspace directory path (defaults to current directory)")
    parser.add_argument("-p", "--prompt", help="Start TUI and immediately submit this prompt")
    parser.add_argument("-c", "--continue", dest="continue_last", action="store_true", help="Continue most recent session")
    parser.add_argument("-s", "--session", help="Resume specific session by ID")
    parser.add_argument("-m", "--model", help="Override active model ID")
    parser.add_argument("-a", "--agent", choices=["build", "plan"], default="build", help="Initial agent mode")
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
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
