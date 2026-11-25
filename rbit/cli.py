"""CLI entry point & subcommands for rbit: TUI, `run`, `doctor`, `stats`, `sessions`, `export`, `map`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .agent import AgentEngine, discover_ollama_models
from .models import _migrate_session_dict
from .app import RbitApp
from .models import ChatMessage, Session, ToolCallRecord, get_model_info, short_id
from .storage import StorageManager
from .tools import MUTATING_TOOLS, ToolExecutor, detect_test_command
from .widgets import detect_git_branch


def _resolve_workspace(raw_dir: str | None) -> Path:
    return Path(raw_dir).resolve() if raw_dir else Path.cwd().resolve()


def _confirm(console: Console, question: str, assume_yes: bool = False) -> bool:
    """Ask for confirmation; refuse (False) in a non-interactive session without --yes."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        console.print(f"[red]Refusing to {markup_escape(question)} without --yes in a non-interactive session.[/]")
        return False
    try:
        answer = input(f"{question}? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def _read_stdin_text(timeout: float | None = None) -> str:
    """Read all of stdin as UTF-8; with a timeout, return '' when nothing arrives in time."""
    import threading

    box: list[str] = []

    def _reader() -> None:
        try:
            buffer = getattr(sys.stdin, "buffer", None)
            if buffer is not None:
                box.append(buffer.read().decode("utf-8", errors="replace"))
            else:  # text-only stream (e.g. an in-memory replacement)
                box.append(sys.stdin.read())
        except Exception:  # noqa: BLE001 - stdin may be closed/odd; treat as empty
            box.append("")

    if timeout is None:
        _reader()
        return box[0].strip() if box else ""
    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    thread.join(timeout)
    return box[0].strip() if box else ""


def _nonneg_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {number})")
    return number


#: Subcommands whose directory is a positional; `-d/--dir DIR` is accepted as an alias.
_DIR_POSITIONAL_SUBS = frozenset(
    {"doctor", "watch", "describe", "status", "risk", "sessions", "stats", "map", "lint", "init", "commit", "backup", "test"}
)
#: Subcommands with sub-subparsers: `-d DIR` after the verb is hoisted in front of it.
_DIR_HOIST_SUBS = frozenset({"schedule", "agent", "debug", "session", "plugin", "auth"})


def _pop_dir_option(rest: list[str]) -> tuple[str | None, list[str]]:
    """Remove `-d X` / `--dir X` / `--dir=X` from `rest`; returns (value, remaining)."""
    value: str | None = None
    out: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            out.extend(rest[i:])
            break
        if tok in ("-d", "--dir") and i + 1 < len(rest):
            value = rest[i + 1]
            i += 2
            continue
        if tok.startswith("--dir="):
            value = tok.split("=", 1)[1]
            i += 1
            continue
        out.append(tok)
        i += 1
    return value, out


def _normalize_subcommand_args(sub: str, rest: list[str]) -> list[str]:
    if sub in _DIR_POSITIONAL_SUBS:
        value, remaining = _pop_dir_option(rest)
        return remaining + [value] if value is not None else remaining
    if sub in _DIR_HOIST_SUBS:
        value, remaining = _pop_dir_option(rest)
        return ["-d", value, *remaining] if value is not None else remaining
    if sub == "run":
        # `-w` / `--title` take an optional value; never let them swallow the prompt.
        mapping = {"-w": "--worktree=", "--worktree": "--worktree=", "--title": "--title="}
        return [mapping.get(tok, tok) for tok in rest]
    return rest


def _split_mcp_args(rest: list[str]) -> tuple[list[str], list[str]]:
    """Split `rbit mcp` args into (own options + action/name, local-server command)."""
    value_flags = {"--url", "--token", "--header", "--env", "-d", "--dir"}
    head: list[str] = []
    positionals = 0
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            return head, rest[i + 1 :]
        flag = tok.split("=", 1)[0]
        if flag in value_flags:
            head.append(tok)
            if "=" not in tok and i + 1 < len(rest):
                head.append(rest[i + 1])
                i += 2
            else:
                i += 1
            continue
        if tok in ("-h", "--help"):
            head.append(tok)
            i += 1
            continue
        if positionals < 2:  # action, name
            head.append(tok)
            positionals += 1
            i += 1
            continue
        return head, rest[i:]
    return head, []


def _port_arg(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port: {value!r}") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between 0 and 65535 (got {port})")
    return port


def _create_worktree(workspace: Path, name: str = "") -> Path | None:
    """Creates a sibling git worktree for an isolated session."""
    import subprocess
    import time as _time

    console = Console()
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=str(workspace), capture_output=True, text=True, check=False
    )
    if root.returncode != 0:
        console.print(f"[red]Not a git repository:[/] {markup_escape(str(workspace))}")
        return None
    repo = Path(root.stdout.strip())
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")
    slug = re.sub(r"\.\.+", ".", slug) or _time.strftime("%Y%m%d-%H%M%S")
    target = repo.parent / f"{repo.name}-{slug}"
    branch = f"rbit/{slug}"
    cmd = ["git", "worktree", "add", "-b", branch, str(target)]
    proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, check=False)
    if proc.returncode != 0 and "already exists" in (proc.stderr or ""):
        proc = subprocess.run(
            ["git", "worktree", "add", str(target), branch], cwd=str(repo), capture_output=True, text=True, check=False
        )
    if proc.returncode != 0:
        console.print(f"[red]git worktree failed:[/] {markup_escape((proc.stderr or proc.stdout).strip()[:300])}")
        return None
    console.print(f"[#7fd88f]Worktree:[/] {markup_escape(str(target))} [dim]({markup_escape(branch)})[/]")
    return target


async def _run_headless(args: argparse.Namespace) -> int:
    output_format = getattr(args, "output_format", "text")
    jsonl_mode = output_format == "jsonl"
    json_mode = args.json or output_format == "json"
    if getattr(args, "auto_approve", False):
        os.environ["RBIT_AUTO_APPROVE"] = "1"
    console = Console(stderr=json_mode or jsonl_mode)
    workspace = _resolve_workspace(args.dir)
    if getattr(args, "worktree", None) is not None:
        created = _create_worktree(workspace, args.worktree or "")
        if created is None:
            return 1
        workspace = created
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    if storage.untrusted_keys:
        console.print("[yellow]" + markup_escape(storage.untrusted_notice()) + "[/]")
    if getattr(args, "prompt_profile", None):
        cfg["prompt_profile"] = args.prompt_profile

    model_id = args.model or cfg.get("model", "rbit/smart-local")
    agent_mode = args.agent or "build"
    if agent_mode not in ("build", "plan"):
        from rbit.subagents import load_subagents, subagent_names

        definitions = load_subagents(workspace, cfg)
        definition = definitions.get(agent_mode)
        if definition is None:
            console.print(f"[red]Unknown agent:[/] {markup_escape(agent_mode)}. Available: build, plan, " + markup_escape(", ".join(subagent_names(definitions))))
            return 1
        cfg["agent_prompt"] = definition.prompt
        if definition.tools is not None:
            cfg["agent_tools"] = sorted(definition.tools)
        if definition.model and not args.model:
            model_id = definition.model

    if getattr(args, "fork", False) and not (args.continue_last or args.session):
        console.print("[red]--fork requires -c or -s.[/]")
        return 2
    if args.continue_last:
        existing = storage.list_sessions()
        if not existing:
            console.print("[red]No sessions to continue in this workspace.[/] Run `rbit run \"prompt\"` first.")
            return 1
        session = existing[0]
    elif args.session:
        found_session = storage.find_session(args.session)
        if found_session is None:
            console.print(f"[red]Session not found:[/] {markup_escape(str(args.session))}")
            return 1
        session = found_session
    else:
        session = Session()

    prompt_text = " ".join(args.prompt).strip()
    if getattr(args, "stdin", False):
        stdin_text = _read_stdin_text()
        prompt_text = (prompt_text + "\n\n" + stdin_text).strip()
    elif not prompt_text and not sys.stdin.isatty():
        # No prompt given: wait only briefly for piped input instead of hanging forever.
        prompt_text = _read_stdin_text(timeout=float(os.environ.get("RBIT_STDIN_TIMEOUT", "5")))
    if getattr(args, "files", None):
        prompt_text = prompt_text + "".join(" @" + f for f in args.files)

    if not prompt_text:
        console.print("[bold #e06c75]Error:[/] Prompt is required for `rbit run`.")
        return 1

    if getattr(args, "fork", False) and (getattr(args, "continue_last", False) or getattr(args, "session", None)):
        forked = Session(title=f"{session.title} (fork)")
        forked.messages = list(session.messages)
        storage.save_session(forked)
        session = forked
    if getattr(args, "title", None) is not None:
        session.title = args.title or prompt_text[:44]
    elif len(session.messages) == 0 and session.title in ("New Session", "Новая сессия", ""):
        session.title = prompt_text[:44]
    if getattr(args, "archived", False):
        session.archived = True

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

    if cfg.get("auto_compact", True) and len(session.messages) >= 8:
        info = get_model_info(model_id, engine.extra_models)
        if info.context_window and session.total_tokens / info.context_window * 100 >= int(cfg.get("auto_compact_at", 85)):
            older = list(session.messages[:-2])
            if older:
                summary_text = await engine.summarize_session(older, model_id)
                summary_msg = ChatMessage(
                    id=short_id(),
                    role="assistant",
                    content=f"**[Compacted]** {len(older)} earlier message(s) → handoff summary\n\n{summary_text}",
                    agent=agent_mode,
                    model=model_id,
                )
                session.messages = [summary_msg] + session.messages[-2:]
                storage.save_session(session)

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
            console.print(f"[#808080]  ~ {markup_escape(rec.name)}[/] [dim]{markup_escape(json.dumps(rec.arguments, ensure_ascii=False))}[/]")

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
            console.print(f"[{color}]  ✓ {markup_escape(rec.name)}[/] [#808080]{markup_escape(str(rec.summary))}[/]")

    async def on_status(_txt: str) -> None:
        return

    reasoning_parts: list[str] = []
    show_thinking = bool(getattr(args, "thinking", False))

    async def on_reasoning_chunk(piece: str) -> None:
        reasoning_parts.append(piece)
        if jsonl_mode:
            _jsonl({"event": "reasoning", "text": piece})
        elif show_thinking and not json_mode:
            console.print(piece, end="", style="dim", highlight=False)

    try:
        reply = await engine.run_turn(
            session=session,
            user_msg=user_msg,
            agent_mode=agent_mode,
            model_id=model_id,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            on_status=on_status,
            on_reasoning_chunk=on_reasoning_chunk if (show_thinking or jsonl_mode) else None,
        )
    except Exception as exc:  # noqa: BLE001 - report as a failed run with a non-zero exit code
        message = f"{type(exc).__name__}: {exc}"
        try:
            storage.save_session(session)
        except Exception:  # noqa: BLE001 - best effort
            pass
        if jsonl_mode:
            _jsonl({"event": "done", "ok": False, "error": message, "session_id": session.id})
        elif json_mode:
            sys.stdout.write(json.dumps({"ok": False, "error": message, "session_id": session.id}, ensure_ascii=False, indent=2) + "\n")
        else:
            console.print(f"[bold #e06c75]Run failed:[/] {markup_escape(message)}")
        return 1
    session.messages.append(reply)
    storage.save_session(session)

    if (
        cfg.get("test_gate", True)
        and cfg.get("self_check", True)
        and not executor.session_readonly
        and any(tc.name in MUTATING_TOOLS for tc in reply.tool_calls)
    ):
        mutated_files: list[str] = []
        for tc in reply.tool_calls:
            if tc.name not in MUTATING_TOOLS:
                continue
            raw_path = (tc.arguments or {}).get("filePath") or (tc.arguments or {}).get("path")
            if raw_path:
                mutated_files.append(str(raw_path))
        code_exts = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java")
        if any(p.endswith(code_exts) for p in mutated_files):
            test_cmd = detect_test_command(workspace)
            if test_cmd:
                if jsonl_mode:
                    _jsonl({"event": "test_gate", "command": test_cmd})
                elif not json_mode:
                    console.print(f"[#808080]  ~ test_gate: {markup_escape(test_cmd)}[/]")
                gate_res = await executor.execute("bash", {"command": test_cmd, "timeout": 300}, agent_mode)
                if not gate_res.ok:
                    tail = "\n".join(gate_res.output.splitlines()[-25:])
                    fix_msg = ChatMessage(
                        id=short_id(),
                        role="user",
                        content=(
                            f"[auto] Test gate failed (`{test_cmd}`):\n\n{tail}\n\n"
                            "Fix the failing tests. Do not touch unrelated files."
                        ),
                        agent=agent_mode,
                    )
                    session.messages.append(fix_msg)
                    storage.save_session(session)
                    try:
                        reply = await engine.run_turn(
                            session=session,
                            user_msg=fix_msg,
                            agent_mode=agent_mode,
                            model_id=model_id,
                            on_tool_start=on_tool_start,
                            on_tool_end=on_tool_end,
                            on_status=on_status,
                            on_reasoning_chunk=on_reasoning_chunk if (show_thinking or jsonl_mode) else None,
                        )
                        session.messages.append(reply)
                        storage.save_session(session)
                    except Exception as exc:  # noqa: BLE001 - keep initial reply if fix round fails
                        if not json_mode and not jsonl_mode:
                            console.print(f"[yellow]Test gate auto-fix failed:[/] {markup_escape(str(exc))}")

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
        if reasoning_parts:
            out["reasoning"] = "".join(reasoning_parts)
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    else:
        console.print()
        console.print(RichMarkdown(reply.content))
        console.print(
            f"\n[#fab283]▣[/] [#808080]{markup_escape(reply.agent.capitalize())} · {markup_escape(str(reply.model))} · "
            f"{reply.duration_s:.2f}s · session {markup_escape(str(session.id))}[/]"
        )
    return 0


def _run_sandbox(workspace: Path, action: str) -> int:
    """`rbit sandbox status|check|cleanup`."""
    from . import sandbox

    cfg = StorageManager(workspace).load_config()
    if action == "cleanup":
        if sys.platform != "win32":
            print("Nothing to clean up: this platform's sandbox changes no permissions.")
            return 0
        removed = sandbox.windows.revoke_all()
        print(f"Removed {removed} permission entries and the rbit sandbox profile.")
        return 0
    info = sandbox.status(cfg)
    print(f"platform: {info['platform']}   backend: {info['backend'] or 'none'}   mode: {info['mode']}   network: {'allowed' if info['network'] else 'blocked'}")
    if action == "status":
        if info["backend"] is None:
            print("No sandbox backend on this machine: " + sandbox.unavailable_message())
            return 1 if info["mode"] == "strict" else 0
        print("Turn it on with `shell_sandbox: \"auto\"` (or \"strict\") in .rbit/config.json. Run `rbit sandbox check` to prove the isolation here.")
        return 0
    if info["backend"] is None:
        print("Cannot check: " + sandbox.unavailable_message())
        return 1
    from .sandbox.selftest import run_checks

    results = run_checks(cfg)
    for check in results:
        mark = "PASS" if check.passed else "FAIL"
        extra = "" if check.passed else f"  -> {check.detail}"
        print(f"  {mark}  [{check.expect:6}] {check.name}{extra}")
    failed = [c for c in results if not c.passed]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


def _run_trust(workspace: Path, *, yes: bool = False, status: bool = False, revoke: bool = False) -> int:
    """`rbit trust`: show the project's risky settings and trust them after confirmation."""
    from .trust import describe

    storage = StorageManager(workspace)
    if revoke:
        print("Trust revoked for this workspace." if storage.revoke_trust() else "Nothing was trusted for this workspace.")
        return 0
    pending = storage.pending_trust()
    if not pending:
        print("Nothing to trust: this project's config has no untrusted settings.")
        return 0
    print(f"Untrusted settings in {storage.config_path}:")
    for key, value in pending.items():
        print(f"  - {key}: {describe(key, value)}")
    if status:
        return 1
    if not yes:
        if not (sys.stdin and sys.stdin.isatty()):
            print("Not a terminal: review the list above and re-run with --yes to trust it.", file=sys.stderr)
            return 1
        try:
            answer = input("Trust these settings for this workspace? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Not trusted.")
            return 1
    trusted = storage.trust_keys()
    print("Trusted: " + ", ".join(trusted))
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
    tbl.add_row("Storage Backend", "OK", str(cfg.get("storage_backend") or "json (sessions)"))
    tbl.add_row("Bash Sandbox", "ON" if cfg.get("bash_sandbox") else "OFF", "config bash_sandbox")
    tbl.add_row("Session Security", "ENCRYPTED" if cfg.get("encrypt_sessions") else "PLAIN", "config encrypt_sessions (+RBIT_PASSPHRASE)")
    keyring_ok = bool(storage.resolve_api_key(cfg)) or bool((cfg.get("api_key") or "").strip())
    tbl.add_row("API Key Source", "OK" if keyring_ok else "MISSING", "keyring" if str(cfg.get("api_key")) == "keyring" else "config")
    try:
        from rbit.tools import detect_language_servers

        lsp = detect_language_servers()
        found = [k for k, v in lsp.items() if v]
        tbl.add_row("Language Servers", "FOUND" if found else "NONE", ", ".join(found) or "pylsp/pyright/clangd/tsls not on PATH (grep/lint still work)")
    except Exception as exc:
        tbl.add_row("Language Servers", "ERR", str(exc)[:60])
    rg = shutil.which("rg")
    tbl.add_row("ripgrep (fast grep)", "OK" if rg else "FALLBACK", rg or "python fallback in use")
    ruff = shutil.which("ruff")
    tbl.add_row("ruff (lint tool)", "OK" if ruff else "FALLBACK", ruff or "built-in syntax diagnostics only")

    console.print(tbl)
    return 0


def _run_models(workspace: Path, provider: str | None, verbose: bool, refresh: bool, as_json: bool = False) -> int:
    from rbit.models import ModelInfo, get_model_info
    from rbit.agent import discover_ollama_models

    console = Console()
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    rows = []
    if refresh:
        engine = AgentEngine(workspace=workspace, executor=ToolExecutor(workspace=workspace, session=Session(), get_config=lambda: cfg), get_config=lambda: cfg)
        key = storage.resolve_api_key(cfg).strip()
        base = str(cfg.get("api_base") or "")
        if base and key:
            ok, message, fetched = asyncio.run(engine.fetch_provider_models(base, key))
            if ok and fetched:
                engine.add_provider_models(fetched)
                cfg["provider_models"] = [m.to_dict() for m in engine.extra_models]
                storage.save_config(cfg)
                console.print(f"[dim]cache refreshed: {markup_escape(str(message))}[/]")
    for m in cfg.get("provider_models") or []:
        try:
            rows.append(ModelInfo.from_dict(m) if isinstance(m, dict) else m)
        except Exception:
            continue
    try:
        rows.extend(asyncio.run(discover_ollama_models()))
    except Exception:
        pass
    known = get_model_info(cfg.get("model", ""), rows)
    if known and not any(r.id == known.id for r in rows):
        rows.insert(0, known)
    if provider:
        rows = [r for r in rows if provider.lower() in r.id.lower() or provider.lower() in r.provider.lower()]
    if not rows:
        console.print("No models found. Configure /connect or run with --refresh.")
        return 1
    if as_json:
        print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False, indent=2))
        return 0
    for r in sorted(rows, key=lambda x: x.id):
        meta = f"  | {r.context_window:,} ctx | ${r.cost_per_1k}/1k" if verbose else ""
        console.print(f"{markup_escape(str(r.id))} [dim]({markup_escape(str(r.provider))})[/]{meta}")
    return 0


def _credential_label(base: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(base).hostname or "default"
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "default"


def _mask_secret(key: str) -> str:
    return (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("set" if key else "MISSING")


def _run_auth(
    workspace: Path,
    cmd: str,
    url: str | None,
    *,
    name: str | None = None,
    file: str | None = None,
    fmt: str = "default",
    key: str | None = None,
    model: str | None = None,
) -> int:
    from rbit.agent import AgentEngine
    from rbit.models import Session
    from rbit.tools import ToolExecutor

    console = Console()
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    credentials = cfg.get("credentials")
    if not isinstance(credentials, dict):
        credentials = {}
    active = str(cfg.get("active_credential") or "")
    if cmd == "ls":  # argparse stores the alias the user typed
        cmd = "list"
    if cmd == "list":
        entries = []
        base = str(cfg.get("api_base") or "(not set)")
        current_key = storage.resolve_api_key(cfg)
        entries.append(
            {
                "name": active or "(current config)",
                "active": True,
                "api_base": base,
                "api_key": _mask_secret(current_key),
                "model": cfg.get("model"),
            }
        )
        for cred_name, credential in sorted(credentials.items()):
            if not isinstance(credential, dict):
                continue
            entries.append(
                {
                    "name": cred_name,
                    "active": cred_name == active,
                    "api_base": credential.get("api_base"),
                    "api_key": _mask_secret(
                        (
                            storage.load_secret(f"credential:{cred_name}") or ""
                            if credential.get("api_key") == "keyring"
                            else str(credential.get("api_key") or "")
                        )
                    ),
                    "model": credential.get("model"),
                }
            )
        if fmt == "json":
            print(json.dumps(entries, ensure_ascii=False, indent=2))
            return 0
        table = Table(title="rbit credentials", border_style="#282828")
        table.add_column("Name")
        table.add_column("Active")
        table.add_column("Base URL")
        table.add_column("Key")
        table.add_column("Model")
        for entry in entries:
            table.add_row(
                markup_escape(str(entry["name"])),
                "*" if entry["active"] else "",
                markup_escape(str(entry["api_base"] or "")),
                markup_escape(str(entry["api_key"])),
                markup_escape(str(entry["model"] or "")),
            )
        console.print(table)
        return 0
    if cmd == "login":
        target = (name or "").strip()
        base = (url or (target if target.startswith(("http://", "https://")) else "") or str(cfg.get("api_base") or "")).rstrip("/")
        if not base:
            base = console.input("API base URL: ").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        cred_name = target if target and not target.startswith(("http://", "https://")) else _credential_label(base)
        if key is None:
            import getpass

            key = getpass.getpass("API key (input hidden): ").strip()
        key = (key or "").strip()
        if not key:
            console.print("[red]No key entered.[/]")
            return 1
        keyring_saved = storage.store_secret(f"credential:{cred_name}", key)
        credential = {
            "api_base": base,
            "api_key": "keyring" if keyring_saved else key,
            "model": model or cfg.get("model"),
        }
        credentials[cred_name] = credential
        cfg["credentials"] = credentials
        cfg["active_credential"] = cred_name
        cfg["api_base"] = base
        if storage.store_secret("api_key", key):
            cfg["api_key"] = "keyring"
        else:
            cfg["api_key"] = key
        if model:
            cfg["model"] = model
        storage.save_config(cfg)
        engine = AgentEngine(
            workspace=workspace,
            executor=ToolExecutor(workspace=workspace, session=Session(), get_config=lambda: cfg),
            get_config=lambda: cfg,
        )
        ok, message = asyncio.run(engine.validate_api_key(base, key))
        console.print(("[bold #7fd88f]OK[/] " if ok else "[bold #e06c75]FAIL[/] ") + f"{cred_name}: {message}")
        return 0 if ok else 1
    if cmd == "logout":
        cred_name = name or active
        if cred_name and cred_name in credentials:
            del credentials[cred_name]
            storage.delete_secret(f"credential:{cred_name}")
            cfg["credentials"] = credentials
            if cred_name == active:
                cfg["active_credential"] = ""
                cfg["api_key"] = ""
                storage.delete_secret("api_key")
            storage.save_config(cfg)
            console.print(f"credential removed: {markup_escape(str(cred_name))}")
            return 0
        if name:
            # An unknown name must not fall through to wiping the active API key.
            console.print(f"[red]Unknown credential:[/] {markup_escape(name)}")
            return 1
        cfg["api_key"] = ""
        cfg["active_credential"] = ""
        storage.save_config(cfg)
        storage.delete_secret("api_key")
        console.print("API key removed.")
        return 0
    if cmd == "switch":
        target = (name or "").strip()
        credential = credentials.get(target)
        if not isinstance(credential, dict):
            console.print(f"[red]Unknown credential:[/] {markup_escape(str(target or '(missing)'))}")
            return 1
        cfg["active_credential"] = target
        cfg["api_base"] = credential.get("api_base") or cfg.get("api_base")
        source_key = (
            storage.load_secret(f"credential:{target}") or ""
            if credential.get("api_key") == "keyring"
            else str(credential.get("api_key") or "")
        )
        if source_key and storage.store_secret("api_key", source_key):
            cfg["api_key"] = "keyring"
        else:
            cfg["api_key"] = source_key
        if credential.get("model"):
            cfg["model"] = credential["model"]
        storage.save_config(cfg)
        console.print(f"active credential: {markup_escape(str(target))}")
        return 0
    if cmd == "export":
        resolved_credentials: dict[str, Any] = {}
        for cred_name, credential in credentials.items():
            if not isinstance(credential, dict):
                continue
            resolved = dict(credential)
            if resolved.get("api_key") == "keyring":
                resolved["api_key"] = storage.load_secret(f"credential:{cred_name}") or ""
            resolved_credentials[cred_name] = resolved
        if name and name not in resolved_credentials:  # check BEFORE indexing: an unknown name was a KeyError
            console.print(f"[red]Unknown credential:[/] {markup_escape(str(name))}")
            return 1
        data = {name: resolved_credentials[name]} if name else resolved_credentials
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if file:
            Path(file).write_text(text + "\n", encoding="utf-8")
            console.print(f"credentials written to {markup_escape(str(file))}")
        else:
            print(text)
        return 0
    if cmd == "import":
        try:
            if file:
                text = Path(file).read_text(encoding="utf-8")
            else:
                text = sys.stdin.read()
            data = json.loads(text)
        except (OSError, ValueError) as exc:
            console.print(f"[red]Import failed:[/] {markup_escape(str(exc))}")
            return 1
        if not isinstance(data, dict):
            console.print("[red]Import failed:[/] expected a JSON object")
            return 1
        for cred_name, credential in data.items():
            if isinstance(credential, dict):
                credentials[str(cred_name)] = credential
        cfg["credentials"] = credentials
        storage.save_config(cfg)
        console.print(f"imported {len(data)} credential(s)")
        return 0
    return 1


def _run_pr(workspace: Path, number: int) -> int:
    import subprocess

    def git(*args: str):
        return subprocess.run(["git", *args], cwd=str(workspace), capture_output=True, text=True)

    number = int(number)
    fetch = git("fetch", "origin", f"+pull/{number}/head:pr-{number}")
    if fetch.returncode != 0:
        Console().print(f"[red]git fetch failed:[/] {markup_escape(fetch.stderr.strip()[:300])}")
        return 1
    co = git("checkout", f"pr-{number}")
    if co.returncode != 0:
        Console().print(f"[red]git checkout failed:[/] {markup_escape(co.stderr.strip()[:300])}")
        return 1
    Console().print(f"[bold #7fd88f]OK[/] Checked out pr-{number}. Run `rbit` here to work on the PR.")
    return 0


def _run_agent(workspace: Path, cmd: str, name: str | None) -> int:
    console = Console()
    if cmd == "list":
        console.print("[bold #eeeeee]Built-in agents[/]")
        console.print("  - build: full access (default)")
        console.print("  - plan: read-only planning")
        console.print("[bold #eeeeee]Skill agents[/] (.rbit/skills/*.md)")
        skills_dir = workspace / ".rbit" / "skills"
        found = sorted(skills_dir.glob("*.md")) if skills_dir.exists() else []
        if found:
            for f in found:
                console.print(f"  - {markup_escape(f.stem)}")
        else:
            console.print("  (none - rbit agent create <name>)")
        return 0
    if cmd == "create" and name:
        # The name becomes a file name under .rbit/skills: `../../x` or `a/b` must not escape it.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or ".." in name:
            console.print(
                f"[red]Invalid agent name:[/] {markup_escape(name)} "
                "(start with a letter or digit; letters, digits, dot, dash and underscore only)"
            )
            return 2
        skills_dir = workspace / ".rbit" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        target = skills_dir / f"{name}.md"
        if target.exists():
            console.print(f"[red]Skill already exists:[/] {markup_escape(str(target))}")
            return 1
        body_lines = [
            "---",
            "description: Describe what this agent does",
            "mode: build",
            "---",
            f"You are the `{name}` agent.",
            "",
            "Describe the workflow, rules and output format here.",
        ]
        target.write_text(chr(10).join(body_lines) + chr(10), encoding="utf-8")
        console.print(f"OK Skill agent created: {markup_escape(str(target))}  -> run via /skill {markup_escape(name)}")
        return 0
    return 1


def _changed_files(workspace: Path) -> list[str]:
    """Changed (non-deleted) files relative to the workspace directory."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--relative", "--diff-filter=d", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return [item for item in files if (workspace / item).exists()]


def _is_test_path(path: Path) -> bool:
    stem = path.stem.lower()
    if stem.startswith("test_") or stem.endswith("_test") or stem in ("test", "tests", "conftest"):
        return True
    return bool(path.parts) and path.parts[0] in ("tests", "test")


def _impacted_tests(workspace: Path, changed: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for item in changed:
        path = Path(item)
        if _is_test_path(path):
            if item not in seen:
                selected.append(item)
                seen.add(item)
            continue
        stem = path.stem
        candidates = [
            workspace / "tests" / f"test_{stem}.py",
            workspace / "tests" / f"{stem}_test.py",
            workspace / "test" / f"test_{stem}.py",
        ]
        found = [str(path_item) for path_item in candidates if path_item.exists()]
        if not found:
            test_dirs = [workspace / "tests", workspace / "test"]
            for directory in test_dirs:
                if not directory.exists():
                    continue
                for test_file in directory.rglob("*.py"):
                    if test_file.stat().st_size > 500_000:
                        continue
                    try:
                        if stem in test_file.read_text(encoding="utf-8", errors="replace"):
                            found.append(str(test_file))
                            break
                    except OSError:
                        continue
        for item_found in found:
            normalized = (
                Path(item_found).relative_to(workspace).as_posix()
                if item_found.startswith(str(workspace))
                else Path(item_found).as_posix()
            )
            if normalized not in seen:
                selected.append(normalized)
                seen.add(normalized)
    return sorted(selected)


def _run_test(workspace: Path, changed_only: bool, run: bool, as_json: bool) -> int:
    console = Console()
    if changed_only and shutil.which("git") is None:
        console.print("[red]git was not found in PATH; `rbit test --changed` needs git.[/]")
        return 1
    changed = _changed_files(workspace)
    tests = _impacted_tests(workspace, changed) if changed_only else []
    command = detect_test_command(workspace)
    argv: list[str] | None = None  # explicit argv (no shell) when file names are involved
    if changed_only and tests:
        if command and command.lower().startswith("pytest"):
            argv = [*shlex.split(command, posix=(os.name != "nt")), *tests]
        elif shutil.which("pytest"):
            argv = [sys.executable, "-m", "pytest", *tests]
        if argv:
            command = subprocess.list2cmdline(argv)
    if as_json:
        print(
            json.dumps(
                {
                    "changed_files": changed,
                    "impacted_tests": tests,
                    "command": command if run or not changed_only else (command if tests else None),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if changed_only:
        console.print("[bold]Changed files[/]")
        console.print(markup_escape("\n".join(changed)) or "(none)")
        console.print("\n[bold]Impacted tests[/]")
        console.print(markup_escape("\n".join(tests)) or "(none detected)")
    if not run:
        return 0
    if not command:
        console.print("[red]No test command detected.[/]")
        return 1
    console.print(f"[bold]Running:[/] {markup_escape(command)}")
    if argv:
        proc = subprocess.run(argv, cwd=str(workspace), check=False)
    else:
        proc = subprocess.run(command, cwd=str(workspace), shell=True, check=False)
    return proc.returncode


def _run_github(workspace: Path, kind: str, number: int, as_json: bool) -> int:
    console = Console()
    if shutil.which("gh") is None:
        console.print("[red]GitHub CLI (`gh`) is not installed.[/] Install it or use the web URL.")
        return 1
    fields = (
        "number,title,body,author,state,comments,files"
        if kind == "pr"
        else "number,title,body,author,state,comments"
    )
    proc = subprocess.run(
        ["gh", f"{kind}", "view", str(number), "--json", fields],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        console.print(f"[red]gh {kind} view failed:[/] {markup_escape((proc.stderr or proc.stdout).strip()[:400])}")
        return 1
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        console.print("[red]gh returned malformed JSON.[/]")
        return 1
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    comments = data.pop("comments", [])
    files = data.pop("files", [])
    body = str(data.pop("body", "") or "")
    lines = [
        f"# {kind.upper()} #{data.get('number')}: {data.get('title')}",
        f"- state: {data.get('state')}",
        f"- author: {(data.get('author') or {}).get('login', '')}",
        "",
        body,
    ]
    if files:
        lines += ["", "## Files"] + [f"- {item.get('path')}" for item in files if isinstance(item, dict)]
    if comments:
        lines += ["", "## Comments"]
        for comment in comments:
            if isinstance(comment, dict):
                author = (comment.get("author") or {}).get("login", "")
                lines += [f"**{author}**", str(comment.get("body") or ""), ""]
    console.print(RichMarkdown("\n".join(lines)))
    return 0


def _run_status(workspace: Path, as_json: bool = False) -> int:
    from rbit.risk import analyze_risk
    from rbit.service import ServiceManager

    console = Console()
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    sessions = storage.list_sessions(include_archived=True)
    service = ServiceManager().status()
    risk = analyze_risk(workspace)
    mcp = cfg.get("mcp") if isinstance(cfg.get("mcp"), dict) else {}
    data = {
        "workspace": str(workspace),
        "git_branch": detect_git_branch(workspace),
        "model": cfg.get("model"),
        "api_base": cfg.get("api_base"),
        "api_key": bool(cfg.get("api_key")),
        "sessions": len(sessions),
        "checkpoints": len(storage.list_checkpoints()),
        "mcp_servers": len(mcp),
        "service": service,
        "risk": {"level": risk.level, "score": risk.score},
    }
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    body = (
        f"[bold #eeeeee]Workspace:[/]   {workspace}\n"
        f"[bold #eeeeee]Git:[/]         {data['git_branch'] or 'not a repo'}\n"
        f"[bold #eeeeee]Model:[/]       {data['model']}\n"
        f"[bold #eeeeee]API:[/]         {data['api_base']} ({'key set' if data['api_key'] else 'NO KEY'})\n"
        f"[bold #eeeeee]Sessions:[/]    {data['sessions']} ({data['checkpoints']} checkpoints)\n"
        f"[bold #eeeeee]MCP:[/]         {data['mcp_servers']} server(s)\n"
        f"[bold #eeeeee]Service:[/]     {'running ' + service['url'] if service.get('running') else 'stopped'}\n"
        f"[bold #eeeeee]Change risk:[/] {risk.level} ({risk.score}/100)"
    )
    console.print(Panel(body, title="[bold #fab283]rbit status[/]", border_style="#282828"))
    return 0


def _run_risk(workspace: Path, as_json: bool = False) -> int:
    from rbit.risk import analyze_risk, render_markdown

    report = analyze_risk(workspace)
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        Console().print(RichMarkdown(render_markdown(report)))
    return 0 if report.level in ("low", "medium") else 1


def _run_replay(workspace: Path, session_id: str | None = None, as_json: bool = False) -> int:
    console = Console()
    storage = StorageManager(workspace)
    session = storage.find_session(session_id) if session_id else next(iter(storage.list_sessions()), None)
    if session is None:
        console.print("[red]Session not found.[/]")
        return 1
    if as_json:
        print(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
        return 0
    console.print(f"[bold #fab283]{markup_escape(session.title)}[/] [dim]{markup_escape(session.id)}[/]")
    for message in session.messages:
        role = message.role.upper()
        console.print(f"\n[bold]{role}[/]: {markup_escape(message.content[:2000])}")
        for call in message.tool_calls:
            console.print(f"  [cyan]→ {markup_escape(str(call.name))}[/] {markup_escape(str(call.summary))} [dim]({call.duration_ms} ms)[/]")
            if call.output:
                console.print(f"[dim]{markup_escape(call.output[:400])}[/]")
    return 0


def _run_stats(
    workspace: Path,
    days: int | None = None,
    models: int | None = None,
    *,
    year: int | None = None,
    as_json: bool = False,
    tools: bool = False,
    cost: bool = False,
    full: bool = False,
    limit: int = 5,
    include_archived: bool = False,
) -> int:
    console = Console()
    storage = StorageManager(workspace)
    sessions = storage.list_sessions(include_archived=include_archived)
    checkpoints = storage.list_checkpoints()
    if year:
        sessions = [s for s in sessions if s.created_at[:4] == str(year)]
    elif days is not None:
        cutoff = datetime.now() - timedelta(days=days)
        sessions = [s for s in sessions if s.created_at[:10] >= cutoff.date().isoformat()]

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

    loc = 0
    files_n = 0
    lang_counts: dict[str, int] = {}
    complexity = 0
    complexity_n = 0
    import ast as _ast

    for f in workspace.rglob("*"):
        if f.is_file() and ".rbit" not in f.parts and ".venv" not in f.parts and "node_modules" not in f.parts:
            files_n += 1
            ext = f.suffix.lower()
            lang_counts[ext or "(none)"] = lang_counts.get(ext or "(none)", 0) + 1
            if ext == ".py":
                try:
                    tree = _ast.parse(f.read_text(encoding="utf-8", errors="replace"))
                    loc += sum(1 for _ in f.read_text(encoding="utf-8", errors="replace").splitlines())
                    for node in _ast.walk(tree):
                        if isinstance(node, (_ast.If, _ast.For, _ast.While, _ast.Try, _ast.BoolOp)):
                            complexity += 1
                    complexity_n += 1
                except (OSError, SyntaxError):
                    continue

    per_model: dict[str, int] = {}
    per_model_cost: dict[str, float] = {}
    tool_stats: dict[str, dict[str, int]] = {}
    for s in sessions:
        for m in s.messages:
            if m.role == "assistant" and m.model:
                per_model[m.model] = per_model.get(m.model, 0) + m.tokens
                info = get_model_info(m.model)
                per_model_cost[m.model] = per_model_cost.get(m.model, 0.0) + (m.tokens / 1000.0) * info.cost_per_1k
            for tc in m.tool_calls:
                bucket = tool_stats.setdefault(tc.name, {"ok": 0, "error": 0})
                if tc.status == "success":
                    bucket["ok"] += 1
                else:
                    bucket["error"] += 1

    payload = {
        "workspace": str(workspace),
        "sessions": len(sessions),
        "messages": total_msgs,
        "tool_calls": total_tools,
        "tokens": total_tokens,
        "cost": round(cost_est, 6),
        "files_touched": len(modified_set),
        "checkpoints": len(checkpoints),
        "workspace_files": files_n,
        "workspace_loc": loc,
        "languages": dict(sorted(lang_counts.items(), key=lambda kv: -kv[1])),
        "python_complexity": complexity,
        "models": {name: {"tokens": tok, "cost": round(per_model_cost.get(name, 0.0), 6)} for name, tok in per_model.items()},
        "tools": tool_stats,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    panel_body = (
        f"[bold #eeeeee]Sessions:[/]       {len(sessions)}\n"
        f"[bold #eeeeee]Messages:[/]       {total_msgs}\n"
        f"[bold #eeeeee]Tool Calls:[/]     {total_tools}\n"
        f"[bold #eeeeee]Total Tokens:[/]   {total_tokens:,}\n"
        f"[bold #eeeeee]Est. API Cost:[/]  ${cost_est:.4f}\n"
        f"[bold #eeeeee]Files Touched:[/]  {len(modified_set)}\n"
        f"[bold #eeeeee]Checkpoints:[/]    {len(checkpoints)}\n"
        + (
            f"[bold #eeeeee]Workspace:[/]      {files_n} files, {loc:,} LOC, top langs: "
            + ", ".join(f"{k}×{v}" for k, v in sorted(lang_counts.items(), key=lambda kv: -kv[1])[:5])
            + "\n"
            + f"[bold #eeeeee]Py complexity:[/]  {complexity} branches over {complexity_n} files"
        )
    )
    console.print(Panel(panel_body, title=f"[bold #fab283]rbit Workspace Stats[/] ({workspace.name})", border_style="#282828"))
    top_n = max(1, limit)
    if full:
        models = top_n
        tools = True
        cost = True
    if models:
        top = sorted(per_model.items(), key=lambda kv: -kv[1])
        if models > 0:
            top = top[:models]
        mtbl = Table(title="Models", border_style="#282828")
        mtbl.add_column("Model", style="bold #eeeeee")
        mtbl.add_column("Tokens", justify="right")
        mtbl.add_column("Cost", justify="right")
        for name, tok in top:
            mtbl.add_row(name, f"{tok:,}", f"${per_model_cost.get(name, 0.0):.4f}" if cost or full else "")
        console.print(mtbl)
    if tools:
        ttbl = Table(title="Tools", border_style="#282828")
        ttbl.add_column("Tool", style="bold #eeeeee")
        ttbl.add_column("OK", justify="right")
        ttbl.add_column("Errors", justify="right")
        ranked = sorted(tool_stats.items(), key=lambda kv: -(kv[1]["ok"] + kv[1]["error"]))[:top_n]
        for name, counts in ranked:
            ttbl.add_row(name, str(counts["ok"]), str(counts["error"]))
        console.print(ttbl)
    if cost and not models:
        ctbl = Table(title="Cost by model", border_style="#282828")
        ctbl.add_column("Model", style="bold #eeeeee")
        ctbl.add_column("Tokens", justify="right")
        ctbl.add_column("Cost", justify="right")
        for name, tok in sorted(per_model.items(), key=lambda kv: -kv[1])[:top_n]:
            ctbl.add_row(name, f"{tok:,}", f"${per_model_cost.get(name, 0.0):.4f}")
        console.print(ctbl)
    return 0


def _run_sessions_list(
    workspace: Path,
    max_count: int | None = None,
    as_json: bool = False,
    include_archived: bool = False,
) -> int:
    console = Console()
    storage = StorageManager(workspace)
    sessions = storage.list_sessions(include_archived=include_archived)
    if max_count is not None and max_count >= 0:
        sessions = sessions[:max_count]
    if not sessions:
        if as_json:
            print("[]")
        else:
            console.print("[#808080]No saved sessions in this workspace.[/]")
        return 0
    if as_json:
        payload = [
            {
                "id": s.id,
                "title": s.title,
                "messages": len(s.messages),
                "tokens": s.total_tokens,
                "created_at": s.created_at,
                "archived": s.archived,
            }
            for s in sessions
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    tbl = Table(title="Saved rbit Sessions", border_style="#282828")
    tbl.add_column("ID", style="bold #fab283")
    tbl.add_column("Title", style="#eeeeee")
    tbl.add_column("Messages", justify="right", style="#808080")
    tbl.add_column("Tokens", justify="right", style="#808080")
    tbl.add_column("Created", style="#808080")
    for s in sessions:
        tbl.add_row(markup_escape(s.id), markup_escape(s.title), str(len(s.messages)), f"{s.total_tokens:,}", markup_escape(s.created_at))
    console.print(tbl)
    return 0


def _run_import(workspace: Path, source: str, overwrite: bool = False) -> int:
    console = Console()
    storage = StorageManager(workspace)
    if source.startswith(("http://", "https://")):
        try:
            import urllib.request

            with urllib.request.urlopen(source, timeout=20) as response:
                text = response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - network/URL errors are user-facing
            console.print(f"[red]Download failed:[/] {markup_escape(str(exc))}")
            return 1
        name = Path(source.split("?", 1)[0]).name or "remote-session.json"
        fpath = Path(name if Path(name).suffix else "remote-session.json")
    else:
        fpath = Path(source)
        if not fpath.exists():
            console.print(f"[red]File not found:[/] {markup_escape(str(fpath))}")
            return 1
        text = fpath.read_text(encoding="utf-8", errors="replace")
    messages = []
    looks_json = fpath.suffix.lower() == ".json" or text.lstrip().startswith(("{", "["))
    if looks_json:
        try:
            data = json.loads(text)
        except ValueError:
            console.print("[red]Invalid JSON session file.[/]")
            return 1
        if isinstance(data, dict) and "messages" in data:
            if not isinstance(data.get("messages"), list) or not all(isinstance(m, dict) for m in data["messages"]):
                console.print("[red]Invalid session file:[/] `messages` must be a list of objects.")
                return 1
            try:
                data = _migrate_session_dict(data)
                sess = Session.from_dict(data)
            except Exception as exc:  # noqa: BLE001 - malformed external data
                console.print(f"[red]Invalid session file:[/] {markup_escape(str(exc))}")
                return 1
            if not overwrite and any(existing.id == sess.id for existing in storage.list_sessions(include_archived=True)):
                console.print(
                    f"[red]Session `{markup_escape(sess.id)}` already exists.[/] Use --overwrite to replace it."
                )
                return 1
            storage.save_session(sess)
            console.print(f"✓ Imported rbit session `{sess.id}` ({len(sess.messages)} messages)")
            return 0
        for m in data if isinstance(data, list) else []:
            if not isinstance(m, dict):
                console.print("[red]Invalid message list:[/] every entry must be an object with role/content.")
                return 1
            role = str(m.get("role") or "user")
            if role in ("system", "user", "assistant"):
                messages.append(ChatMessage(id=short_id(), role=role, content=str(m.get("content") or "")))
    else:
        role = "user"
        buf: list[str] = []
        for line in text.splitlines():
            low = line.strip().lower()
            if low.startswith("## user"):
                if buf:
                    messages.append(ChatMessage(id=short_id(), role=role, content="\n".join(buf).strip()))
                    buf = []
                role = "user"
            elif low.startswith("## assistant"):
                if buf:
                    messages.append(ChatMessage(id=short_id(), role=role, content="\n".join(buf).strip()))
                    buf = []
                role = "assistant"
            else:
                buf.append(line)
        if buf and any(b.strip() for b in buf):
            messages.append(ChatMessage(id=short_id(), role=role, content="\n".join(buf).strip()))
    if not messages:
        console.print("[red]No messages recognized in the file.[/]")
        return 1
    sess = Session(title=f"Imported {fpath.name[:32]}", messages=messages)
    storage.save_session(sess)
    console.print(f"✓ Imported {len(messages)} messages as session `{sess.id}`")
    return 0


def _run_export(workspace: Path, session_id: str | None, out_file: str | None, fmt: str | None = None) -> int:
    console = Console()
    storage = StorageManager(workspace)
    sessions = storage.list_sessions()
    if not sessions:
        console.print("[bold #e06c75]No sessions available to export.[/]")
        return 1
    target = storage.find_session(session_id) if session_id else sessions[0]
    if not target:
        console.print(f"[bold #e06c75]Session '{markup_escape(str(session_id))}' not found.[/]")
        return 1
    fmt = str(fmt or "markdown")
    if fmt == "openai":
        out = Path(out_file) if out_file else workspace / f"rbit-{target.id}-openai.json"
        from .tools import redact_secrets

        messages = []
        for m in target.messages:
            messages.append({"role": m.role, "content": redact_secrets(m.content)})  # as the Markdown export does
        out.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[bold #7fd88f]✓ OpenAI-format export →[/] {markup_escape(str(out))}")
        return 0
    if fmt == "html":
        out = Path(out_file) if out_file else workspace / f"rbit-{target.id}.html"
        import html as _html

        from .tools import redact_secrets

        parts = [
            "<!doctype html><meta charset='utf-8'><title>rbit ",
            _html.escape(target.title),
            "</title><style>body{background:#141414;color:#eee;font-family:ui-monospace,monospace;max-width:960px;margin:2rem auto}",
            ".msg{border-left:3px solid #444;padding:.4rem 1rem;margin:.6rem 0;white-space:pre-wrap}",
            ".user{border-color:#fab283}.assistant{border-color:#7fd88f}.system{color:#808080}",
            ".diff-add{color:#7fd88f}.diff-del{color:#e06c75}</style>",
        ]
        for m in target.messages:
            cls = _html.escape(m.role, quote=True)
            parts.append(f"<div class='msg {cls}'><b>{_html.escape(m.role)}</b>")
            body = _html.escape(redact_secrets(m.content))
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    d = _html.escape(redact_secrets(tc.diff or ""))
                    if d:
                        lines = [
                            f"<span class='diff-add'>{ln}</span>" if ln.startswith("+") and not ln.startswith("+++") else
                            f"<span class='diff-del'>{ln}</span>" if ln.startswith("-") and not ln.startswith("---") else ln
                            for ln in d.splitlines()
                        ]
                        body += "<pre>" + "\n".join(lines) + "</pre>"
            parts.append(body + "</div>")
        parts.append("</div>")
        out.write_text("".join(parts), encoding="utf-8")
        console.print(f"[bold #7fd88f]✓ HTML export →[/] {markup_escape(str(out))}")
        return 0
    out_path = Path(out_file).resolve() if out_file else None
    saved = storage.export_session_markdown(target, out_path)
    console.print(f"[bold #7fd88f]✓ Exported session `{markup_escape(target.id)}` to:[/] {markup_escape(str(saved))}")
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
                markup_escape(item["path"]),
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
        res = executor.builtin_diagnostics({"filePath": "."})
        # "ran" is not "clean": a syntax error must give a failing exit code, like the ruff branch
        ok = res.ok and res.summary.startswith("0 issues")
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


_VERSION_RE = re.compile(r"\d+(\.\d+){0,3}((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?")


def _run_update(
    check_only: bool = False, as_json: bool = False, target: str | None = None, assume_yes: bool = False
) -> int:
    """Checks PyPI for a newer release and optionally upgrades the installed package."""
    console = Console()
    if target and not check_only:
        version = target.strip().lstrip("v")
        if not _VERSION_RE.fullmatch(version):
            console.print(f"[red]Invalid version:[/] {markup_escape(version)} (expected e.g. 1.2.3)")
            return 2
        if not _confirm(console, f"install package rbit=={version} from PyPI (runs pip)", assume_yes):
            return 1
        cmd = [sys.executable, "-m", "pip", "install", f"rbit=={version}"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        ok = proc.returncode == 0
        payload = {
            "status": "updated" if ok else "failed",
            "target": version,
            "message": f"Installed rbit v{version}." if ok else (proc.stderr.strip()[-500:] or "pip install failed."),
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            console.print(
                f"[bold #7fd88f]✓[/] Installed rbit v{version}." if ok else f"[bold #e06c75]✗[/] {payload['message']}"
            )
        return 0 if ok else 1
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

    # Bare `rbit update` is itself the request to upgrade; only an interactive session asks again.
    if sys.stdin.isatty() and not _confirm(console, f"upgrade package rbit {current} -> {latest} from PyPI (runs pip)", assume_yes):
        return 1
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
    # The case of the unit matters: `m` is minutes, `M` is months (the spec used to be lower-cased,
    # so `prune 1M` meant "older than one minute" and silently deleted recent sessions).
    match = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]?)\s*", spec)
    unit = (match.group(2) if match else "") or "d"
    if match and unit not in "M":
        unit = unit.lower()
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 30 * 86400, "y": 365 * 86400}
    if not match or unit not in factors:
        raise ValueError(f"invalid age '{spec}' (use e.g. 30d, 12h, 2w, 1M for a month, 5m for minutes)")
    return float(int(match.group(1)) * factors[unit])


def _session_last_activity(session: Session) -> float:
    """Epoch seconds of the newest message (fallback: creation time)."""
    stamps: list[float] = []
    for raw in [m.timestamp for m in session.messages[-1:]] + [session.created_at]:
        try:
            stamps.append(datetime.fromisoformat(str(raw)).timestamp())
        except ValueError:
            continue
    return max(stamps) if stamps else time.time()


def _run_sessions_prune(workspace: Path, age_spec: str, assume_yes: bool = False) -> int:
    console = Console()
    storage = StorageManager(workspace)
    storage.ensure_dirs()
    try:
        seconds = _parse_age_spec(age_spec)
    except ValueError as exc:
        console.print(f"[bold #e06c75]{markup_escape(str(exc))}[/]")
        return 2

    if seconds == 0 and not _confirm(console, "delete ALL sessions and checkpoints", assume_yes):
        return 1
    cutoff = time.time() - seconds
    removed_sessions = 0
    if storage._use_sqlite():
        # SQLite backend: there are no per-session files; go through the storage API.
        for session in storage.list_sessions(include_archived=True):
            if _session_last_activity(session) < cutoff and storage.delete_session(session.id):
                removed_sessions += 1
    for path in storage.sessions_dir.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                if storage._use_sqlite():
                    path.unlink()
                elif not storage.delete_session(path.stem):
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
    agents_path = workspace / "AGENTS.md"
    storage = StorageManager(workspace)
    storage.ensure_dirs()
    written: list[str] = []
    skipped: list[str] = []
    for path, content in ((rbit_path, rbit_md), (agents_path, rbit_md), (storage.memory_path, memory)):
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


from .completion import COMMANDS, COMPLETION_SCRIPTS, completion_script as _completion_script




_COMMIT_ADD_PATHSPEC = (
    ".",
    ":(exclude,glob)**/.env",
    ":(exclude,glob)**/.env.*",
    ":(exclude,glob).rbit/**",
    ":(exclude,glob)**/.rbit/**",
)


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

    add = git("add", "-A", "--", *_COMMIT_ADD_PATHSPEC)
    if add.returncode != 0:
        console.print(f"[bold #e06c75]git add failed:[/] {markup_escape(add.stderr.strip())}")
        return 1
    commit = git("commit", "-m", draft)
    if commit.returncode != 0:
        console.print(f"[bold #e06c75]git commit failed:[/] {markup_escape(commit.stderr.strip())}")
        return 1

    subject = draft.splitlines()[0]
    if as_json:
        print(json.dumps({"ok": True, "applied": True, "message": draft}, ensure_ascii=False, indent=2))
    else:
        console.print(f"[bold #7fd88f]Committed:[/] {markup_escape(subject)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    log_level: int | None = None
    for flag, level in (
        ("--quiet", logging.WARNING),
        ("--verbose", logging.INFO),
        ("--trace", logging.DEBUG),
    ):
        if flag in args_list and not (flag == "--verbose" and args_list[:1] == ["models"]):
            args_list = [item for item in args_list if item != flag]
            log_level = level
    if log_level is not None:
        logging.basicConfig(
            stream=sys.stderr,
            level=log_level,
            format="%(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger().setLevel(log_level)

    if "--no-color" in args_list:
        os.environ["NO_COLOR"] = "1"
        args_list = [item for item in args_list if item != "--no-color"]

    if "--log-file" in args_list:
        idx = args_list.index("--log-file")
        if idx + 1 >= len(args_list) or args_list[idx + 1].startswith("-"):
            Console(stderr=True).print("--log-file requires a path")
            return 2
        log_path = Path(args_list[idx + 1]).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        if log_level is None:
            logging.getLogger().setLevel(logging.INFO)
        args_list = args_list[:idx] + args_list[idx + 2:]

    if "--profile" in args_list:
        idx = args_list.index("--profile")
        if idx + 1 >= len(args_list) or args_list[idx + 1].startswith("-"):
            Console(stderr=True).print("--profile requires a name (e.g. work, oss, local)")
            return 2
        os.environ["RBIT_PROFILE"] = args_list[idx + 1]
        args_list = args_list[:idx] + args_list[idx + 2:]

    allow_outside = "--allow-outside" in args_list
    if allow_outside and args_list[:1] not in (["run"], ["serve"], ["mobile"], ["web"]):
        # those subcommands parse --allow-outside themselves; stripping it here would lose it
        args_list = [arg for arg in args_list if arg != "--allow-outside"]

    auto_approve = any(flag in args_list for flag in ("--auto", "--yolo", "--dangerously-skip-permissions"))
    if auto_approve:
        args_list = [arg for arg in args_list if arg not in ("--auto", "--yolo", "--dangerously-skip-permissions")]
        os.environ["RBIT_AUTO_APPROVE"] = "1"

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

    subcommands = {"run", "doctor", "stats", "sessions", "session", "export", "map", "lint", "init", "commit", "update", "upgrade", "uninstall", "backup", "describe", "serve", "mobile", "acp", "import", "watch", "voice", "models", "auth", "web", "pr", "agent", "mcp", "service", "api", "debug", "plugin", "config", "permissions", "completion", "logs", "audit", "usage", "selftest", "skill", "migrate", "diag", "status", "test", "gh", "schedule", "risk", "replay", "reload", "pair", "mini", "trust", "sandbox"}
    if args_list and args_list[0] in subcommands:
        sub = args_list[0]
        rest = _normalize_subcommand_args(sub, args_list[1:])

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
            p.add_argument("-a", "--agent", default="build", help="Agent mode or custom agent name")
            p.add_argument("-c", "--continue", dest="continue_last", action="store_true", help="Continue latest session")
            p.add_argument("-s", "--session", help="Continue specific session ID")
            p.add_argument("--fork", action="store_true", help="Fork the session before continuing (requires -c/-s)")
            p.add_argument(
                "-w",
                "--worktree",
                nargs="?",
                const="",
                default=None,
                help="Run in a new git worktree; give a name as --worktree=NAME",
            )
            p.add_argument("--archived", action="store_true", help="Create the session archived")
            p.add_argument("--thinking", action="store_true", help="Show reasoning/thinking blocks")
            p.add_argument(
                "-f", "--file", dest="files", action="append", default=None, help="File(s) to attach to the message"
            )
            p.add_argument("--title", nargs="?", const="", default=None, help="Session title (truncated prompt if no value)")
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
            p.add_argument("--auto", "--yolo", dest="auto_approve", action="store_true", help="Auto-approve permissions")
            parsed = p.parse_args(rest)
            if allow_outside:
                parsed.allow_outside = True
            return asyncio.run(_run_headless(parsed))

        if sub == "doctor":
            p = argparse.ArgumentParser(prog="rbit doctor")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            parsed = p.parse_args(rest)
            return asyncio.run(_run_doctor(_resolve_workspace(parsed.dir)))

        if sub == "sandbox":
            p = argparse.ArgumentParser(
                prog="rbit sandbox", description="OS-level sandbox for shell commands, MCP servers and tools"
            )
            p.add_argument("action", nargs="?", choices=["status", "check", "cleanup"], default="status")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_sandbox(_resolve_workspace(parsed.dir), parsed.action)

        if sub == "trust":
            p = argparse.ArgumentParser(
                prog="rbit trust",
                description="Review and trust the risky settings of this project's .rbit/config.json",
            )
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("-y", "--yes", action="store_true", help="Trust without asking")
            p.add_argument("--status", action="store_true", help="Only show what is untrusted")
            p.add_argument("--revoke", action="store_true", help="Forget everything trusted for this workspace")
            parsed = p.parse_args(rest)
            return _run_trust(_resolve_workspace(parsed.dir), yes=parsed.yes, status=parsed.status, revoke=parsed.revoke)

        if sub == "watch":
            p = argparse.ArgumentParser(prog="rbit watch", description="Watch workspace files and react to changes")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--run", metavar="PROMPT", help="Send a prompt to the agent when files change")
            p.add_argument("--interval", type=float, default=2.0)
            parsed = p.parse_args(rest)
            return _run_watch(_resolve_workspace(parsed.dir), parsed.run, max(0.5, parsed.interval))

        if sub == "voice":
            p = argparse.ArgumentParser(prog="rbit voice", description="Speech-to-text prompt (optional deps)")
            p.add_argument("--seconds", type=float, default=5.0)
            p.add_argument("--model", default="base", help="whisper model size")
            parsed = p.parse_args(rest)
            return _run_voice(parsed.seconds, parsed.model)

        if sub == "serve":
            p = argparse.ArgumentParser(prog="rbit serve", description="Local HTTP endpoint exposing rbit turns as NDJSON")
            p.add_argument("--port", type=_port_arg, default=8333)
            p.add_argument("--hostname", default="127.0.0.1", help="Hostname to listen on")
            p.add_argument("-d", "--dir", help="Workspace directory")
            p.add_argument("--allow-outside", action="store_true")
            p.add_argument("--cors", action="append", default=None, help="Additional allowed CORS origin")
            p.add_argument("--service", action="store_true", help="Start the background service instead")
            p.add_argument("--stdio", action="store_true", help="Serve JSON-RPC over stdio (ACP-compatible)")
            parsed = p.parse_args(rest)
            if parsed.stdio:
                from rbit.acp import run_acp

                return asyncio.run(run_acp(_resolve_workspace(parsed.dir)))
            if parsed.service:
                from rbit.service import ServiceManager

                from rbit.service import ServiceStartError

                try:
                    state = ServiceManager().start(
                        _resolve_workspace(parsed.dir), host=parsed.hostname, port=parsed.port
                    )
                except ServiceStartError as exc:
                    Console(stderr=True).print(f"[red]{markup_escape(str(exc))}[/]")
                    return 1
                Console().print(f"service started pid={state.pid} url={state.url}")
                return 0
            return _run_serve(parsed, hostname=parsed.hostname, cors_origins=parsed.cors)

        if sub == "mobile":
            p = argparse.ArgumentParser(
                prog="rbit mobile",
                description="Start a token-protected rbit server for the rbit iPhone app on your local network",
            )
            p.add_argument("--port", type=_port_arg, default=8333)
            p.add_argument("--hostname", default="0.0.0.0", help="Hostname to listen on")
            p.add_argument("-d", "--dir", help="Workspace directory")
            p.add_argument("--allow-outside", action="store_true")
            p.add_argument(
                "--no-approvals",
                action="store_true",
                help="Don't ask the iPhone app to approve bash commands",
            )
            parsed = p.parse_args(rest)
            if parsed.no_approvals:
                os.environ["RBIT_APPROVALS"] = "0"
            else:
                os.environ.setdefault("RBIT_APPROVALS", "1")
            return _run_serve(parsed, hostname=parsed.hostname, mobile=True)

        if sub == "acp":
            p = argparse.ArgumentParser(prog="rbit acp", description="Agent Client Protocol server over stdio (for Zed)")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            from rbit.acp import run_acp

            return asyncio.run(run_acp(_resolve_workspace(parsed.dir)))

        if sub == "schedule":
            p = argparse.ArgumentParser(prog="rbit schedule", description="Manage scheduled prompts")
            sub2 = p.add_subparsers(dest="schedule_cmd")
            sub2.add_parser("list", help="List schedules")
            add_p = sub2.add_parser("add", help="Add a schedule")
            add_p.add_argument("prompt")
            add_p.add_argument("schedule", help="every 30m | daily 09:30 | ISO datetime")
            add_p.add_argument("--name", default="")
            add_p.add_argument("-m", "--model", default="")
            add_p.add_argument("-a", "--agent", default="build")
            remove_p = sub2.add_parser("remove", help="Remove a schedule")
            remove_p.add_argument("target", help="Schedule id or name")
            sub2.add_parser("run-due", help="Run all schedules that are due")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_schedule(parsed)

        if sub == "config":
            p = argparse.ArgumentParser(prog="rbit config", description="Inspect and edit configuration")
            p.add_argument("action", nargs="?", default="list", choices=["list", "show", "get", "set", "unset", "validate", "edit", "schema"])
            p.add_argument("key", nargs="?", help="Dotted key, e.g. permissions or websearch.provider")
            p.add_argument("value", nargs="?", help="Value (JSON or string)")
            p.add_argument("-d", "--dir", help="Workspace directory")
            p.add_argument("--json", action="store_true", help="JSON output where supported")
            parsed = p.parse_args(rest)
            return _run_config(_resolve_workspace(parsed.dir), parsed.action, parsed.key, parsed.value, parsed.json)

        if sub == "permissions":
            p = argparse.ArgumentParser(prog="rbit permissions", description="Inspect permission rules")
            p.add_argument("action", nargs="?", default="list", choices=["list", "show", "explain", "check", "mode"])
            p.add_argument("resource", nargs="*", help="For explain: <action> <resource>; for mode: <mode id>")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            resource = " ".join(parsed.resource).strip() or None
            return _run_permissions(_resolve_workspace(parsed.dir), parsed.action, resource)

        if sub == "usage":
            p = argparse.ArgumentParser(prog="rbit usage", description="Token and cost accounting")
            p.add_argument(
                "action",
                nargs="?",
                default="show",
                choices=["show", "today", "forecast", "export"],
            )
            p.add_argument("--days", type=int, default=7, help="Window for show/export")
            p.add_argument("--format", default="json", choices=["json", "csv"], help="Export format")
            p.add_argument("--out", help="Write the export to this path instead of stdout")
            p.add_argument("--json", action="store_true", help="Machine-readable output")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_usage(
                _resolve_workspace(parsed.dir),
                parsed.action,
                parsed.days,
                parsed.format,
                parsed.out,
                parsed.json,
            )

        if sub == "selftest":
            p = argparse.ArgumentParser(
                prog="rbit selftest", description="Verify the install after an upgrade"
            )
            p.add_argument("--json", action="store_true", help="Machine-readable output")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_selftest(_resolve_workspace(parsed.dir), parsed.json)

        if sub == "skill":
            p = argparse.ArgumentParser(
                prog="rbit skill", description="Install and list skills"
            )
            p.add_argument(
                "action",
                nargs="?",
                default="list",
                choices=["list", "install", "verify"],
            )
            p.add_argument("source", nargs="?", help="Git URL or local path (for install)")
            p.add_argument("--name", help="Override the installed skill name")
            p.add_argument("--force", action="store_true", help="Replace a differing install")
            p.add_argument("--json", action="store_true")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_skill(
                _resolve_workspace(parsed.dir),
                parsed.action,
                parsed.source,
                parsed.name,
                parsed.force,
                parsed.json,
            )

        if sub == "plugin":
            p = argparse.ArgumentParser(
                prog="rbit plugin", description="Install, enable and remove plugins"
            )
            p.add_argument(
                "action",
                nargs="?",
                default="list",
                choices=["list", "install", "enable", "disable", "remove"],
            )
            p.add_argument("name", nargs="?", help="Plugin name or install source")
            p.add_argument("--force", action="store_true", help="Replace an existing install")
            p.add_argument(
                "--approve",
                action="store_true",
                help="Enable immediately (default: installed but disabled)",
            )
            p.add_argument("--json", action="store_true")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_plugin(
                _resolve_workspace(parsed.dir),
                parsed.action,
                parsed.name,
                parsed.force,
                parsed.approve,
                parsed.json,
            )

        if sub == "migrate":
            p = argparse.ArgumentParser(
                prog="rbit migrate", description="Upgrade the config with a backup"
            )
            p.add_argument("--dry-run", action="store_true", help="Show changes, write nothing")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_migrate(_resolve_workspace(parsed.dir), parsed.dry_run)

        if sub == "diag":
            p = argparse.ArgumentParser(
                prog="rbit diag", description="Collect a redacted diagnostic bundle"
            )
            p.add_argument("--out", help="Write the bundle here (default: printed)")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_diag(_resolve_workspace(parsed.dir), parsed.out)

        if sub == "audit":
            p = argparse.ArgumentParser(prog="rbit audit", description="Inspect the agent action log")
            p.add_argument("action", nargs="?", default="summary", choices=["summary", "tail", "path", "clear"])
            p.add_argument("--day", help="Restrict to YYYY-MM-DD")
            p.add_argument("-n", "--lines", type=int, default=40, help="Entries for `tail`")
            p.add_argument("--json", action="store_true", help="Machine-readable output")
            p.add_argument("-y", "--yes", action="store_true", help="Confirm `clear` without asking")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_audit(
                _resolve_workspace(parsed.dir), parsed.action, parsed.day, parsed.lines, parsed.json, parsed.yes
            )

        if sub == "completion":
            p = argparse.ArgumentParser(prog="rbit completion", description="Install shell completion")
            p.add_argument("action", nargs="?", default="install", choices=["install", "print"])
            p.add_argument("shell", nargs="?", default="bash", choices=["bash", "zsh", "powershell"])
            parsed = p.parse_args(rest)
            if parsed.action == "print":
                print(_completion_script(parsed.shell), end="")
                return 0
            return _run_completion_install(parsed.shell)

        if sub == "logs":
            p = argparse.ArgumentParser(prog="rbit logs", description="Show the service log")
            p.add_argument("-n", "--lines", type=int, default=200)
            p.add_argument("-d", "--dir", help="Workspace directory")
            p.add_argument("--json", action="store_true")
            parsed = p.parse_args(rest)
            return _run_logs(_resolve_workspace(parsed.dir), parsed.lines, parsed.json)

        if sub == "service":
            p = argparse.ArgumentParser(prog="rbit service", description="Manage the background rbit server")
            p.add_argument("service_cmd", nargs="?", default="status", choices=["start", "stop", "status", "restart", "get", "set", "unset"])
            p.add_argument("key", nargs="?", help="Setting key (get/set/unset)")
            p.add_argument("value", nargs="?", help="Setting value (set)")
            p.add_argument("--port", type=_port_arg, help="Port for start/restart")
            p.add_argument("--hostname", default=None, help="Host for start/restart")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            if parsed.service_cmd == "set" and (not parsed.key or parsed.value is None):
                p.error("service set requires KEY VALUE")
            if parsed.service_cmd in ("get", "unset") and not parsed.key:
                p.error(f"service {parsed.service_cmd} requires KEY")
            return _run_service(parsed)

        if sub == "api":
            p = argparse.ArgumentParser(prog="rbit api", description="Call the running server via OpenAPI")
            p.add_argument("request", nargs="+", help="Operation ID, or METHOD PATH")
            p.add_argument("-d", "--data", help="JSON request body")
            p.add_argument("-H", "--header", action="append", default=None, help="name:value header")
            p.add_argument("--param", action="append", default=None, help="path/query parameter key=value")
            p.add_argument("--server", help="Server URL (defaults to the background service)")
            parsed = p.parse_args(rest)
            return _run_api(parsed)

        if sub == "plugin":
            p = argparse.ArgumentParser(prog="rbit plugin", description="Manage package plugins")
            sub2 = p.add_subparsers(dest="plugin_cmd")
            sub2.add_parser("list", help="List installed plugins")
            add_p = sub2.add_parser("add", help="Install a local path, git repository, or package")
            add_p.add_argument("package")
            check_p = sub2.add_parser("check", help="Check package plugins for updates")
            check_p.add_argument("target", nargs="?", default=None)
            update_p = sub2.add_parser("update", help="Update package plugins")
            update_p.add_argument("target", nargs="?", default=None)
            remove_p = sub2.add_parser("remove", help="Remove a plugin")
            remove_p.add_argument("package")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_plugin(parsed)

        if sub == "debug":
            p = argparse.ArgumentParser(prog="rbit debug", description="Debugging and troubleshooting tools")
            sub2 = p.add_subparsers(dest="debug_cmd")
            sub2.add_parser("agents", help="List all agents")
            sub2.add_parser("config", help="List configuration sources")
            sub2.add_parser("paths", help="Show global paths")
            heap_p = sub2.add_parser("heap-dump", help="Write a Python heap snapshot")
            heap_p.add_argument("-o", "--output", help="Output file")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_debug(parsed)

        if sub in ("update", "upgrade"):
            p = argparse.ArgumentParser(prog=f"rbit {sub}", description="Upgrade rbit to the latest PyPI release")
            p.add_argument("target", nargs="?", help="Version to install")
            p.add_argument("--check", action="store_true")
            p.add_argument("--json", action="store_true")
            p.add_argument("-y", "--yes", action="store_true", help="Install without asking for confirmation")
            parsed = p.parse_args(rest)
            return _run_update(parsed.check, parsed.json, target=parsed.target, assume_yes=parsed.yes)

        if sub == "uninstall":
            p = argparse.ArgumentParser(prog="rbit uninstall", description="Uninstall rbit, keeping user data")
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("-f", "--force", "-y", "--yes", dest="force", action="store_true", help="Do not ask for confirmation")
            parsed = p.parse_args(rest)
            return _run_uninstall(parsed.dry_run, parsed.force)

        if sub == "reload":
            p = argparse.ArgumentParser(prog="rbit reload", description="Reload configuration in the background service")
            p.add_argument("--server", help="Server URL")
            parsed = p.parse_args(rest)
            return _run_reload(parsed.server)

        if sub == "pair":
            p = argparse.ArgumentParser(prog="rbit pair", description="Print a one-time link to connect a browser/app")
            p.add_argument("--url", help="External server URL for pairing links")
            p.add_argument("--remote", action="store_true", help="Expose through the background service")
            p.add_argument("-y", "--yes", action="store_true")
            parsed = p.parse_args(rest)
            return _run_pair(parsed.url, parsed.remote, parsed.yes)

        if sub == "mini":
            p = argparse.ArgumentParser(prog="rbit mini", description="Start the minimal interactive interface")
            p.add_argument("-c", "--continue", dest="continue_last", action="store_true")
            p.add_argument("-s", "--session")
            p.add_argument("-m", "--model")
            p.add_argument("-a", "--agent", default="build")
            p.add_argument("-d", "--dir")
            parsed = p.parse_args(rest)
            return asyncio.run(_run_mini(parsed))

        if sub == "mcp":
            p = argparse.ArgumentParser(prog="rbit mcp", description="Manage and test MCP servers")
            p.add_argument("action", nargs="?", default="list", choices=["list", "test", "add", "remove", "auth", "logout"])
            p.add_argument("name", nargs="?", default="")
            p.add_argument("command", nargs="*", help="Command for a local server (put it after `--`)")
            p.add_argument("--url", help="Remote streamable-HTTP endpoint")
            p.add_argument("--token", help="OAuth/bearer token for a remote server")
            p.add_argument("--header", action="append", default=None, help="Header name=value")
            p.add_argument("--env", action="append", default=None, help="Environment name=value for local servers")
            p.add_argument("-d", "--dir", help="Workspace directory")
            mcp_head, mcp_command = _split_mcp_args(rest)
            parsed = p.parse_args(mcp_head)
            parsed.command = mcp_command
            return _run_mcp(parsed)

        if sub == "import":
            p = argparse.ArgumentParser(prog="rbit import", description="Import a session from JSON (rbit or OpenAI messages), Markdown, or a URL")
            p.add_argument("file", help="Path or URL to .json/.md")
            p.add_argument("--overwrite", action="store_true", help="Replace an existing session with the same id")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_import(_resolve_workspace(parsed.dir), parsed.file, parsed.overwrite)

        if sub == "test":
            p = argparse.ArgumentParser(prog="rbit test", description="Detect and optionally run tests")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--changed", action="store_true", help="Select tests impacted by the current diff")
            p.add_argument("--run", action="store_true", help="Run the selected tests")
            p.add_argument("--json", action="store_true", help="Output JSON")
            parsed = p.parse_args(rest)
            return _run_test(_resolve_workspace(parsed.dir), parsed.changed, parsed.run, parsed.json)

        if sub == "gh":
            p = argparse.ArgumentParser(prog="rbit gh", description="Fetch GitHub issue/PR context via gh")
            p.add_argument("kind", choices=["issue", "pr"])
            p.add_argument("number", type=int)
            p.add_argument("-d", "--dir", help="Workspace directory")
            p.add_argument("--json", action="store_true")
            parsed = p.parse_args(rest)
            return _run_github(_resolve_workspace(parsed.dir), parsed.kind, parsed.number, parsed.json)

        if sub == "models":
            p = argparse.ArgumentParser(prog="rbit models", description="List available models")
            p.add_argument("provider", nargs="?", help="Filter by provider prefix (e.g. glm, am)")
            p.add_argument("--verbose", action="store_true", help="Include cost and context metadata")
            p.add_argument("--refresh", action="store_true", help="Refresh the provider model cache")
            p.add_argument("--json", action="store_true", help="Output JSON")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_models(_resolve_workspace(parsed.dir), parsed.provider, parsed.verbose, parsed.refresh, parsed.json)

        if sub == "auth":
            p = argparse.ArgumentParser(prog="rbit auth", description="Manage provider credentials")
            sub2 = p.add_subparsers(dest="auth_cmd")
            list_p = sub2.add_parser("list", aliases=["ls"], help="List configured provider credentials")
            list_p.add_argument("--format", choices=["default", "json"], default="default")
            login_p = sub2.add_parser("login", help="Log in to a provider (base URL + API key)")
            login_p.add_argument("target", nargs="?", help="Credential name or API base URL")
            login_p.add_argument("--url", help="API base URL")
            login_p.add_argument("--key", help="API key (omit to prompt securely)")
            login_p.add_argument("--model", help="Default model for this credential")
            logout_p = sub2.add_parser("logout", help="Remove a stored credential (defaults to active)")
            logout_p.add_argument("name", nargs="?", default=None)
            switch_p = sub2.add_parser("switch", help="Switch the active credential")
            switch_p.add_argument("name")
            export_p = sub2.add_parser("export", help="Print stored credentials as JSON")
            export_p.add_argument("name", nargs="?", default=None)
            export_p.add_argument("--file", help="Write to a file instead of stdout")
            import_p = sub2.add_parser("import", help="Import credentials exported by auth export")
            import_p.add_argument("file", nargs="?", default=None, help="JSON file (stdin when omitted)")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_auth(
                _resolve_workspace(parsed.dir),
                parsed.auth_cmd or "list",
                getattr(parsed, "url", None),
                name=getattr(parsed, "target", None) or getattr(parsed, "name", None),
                file=getattr(parsed, "file", None),
                fmt=getattr(parsed, "format", "default"),
                key=getattr(parsed, "key", None),
                model=getattr(parsed, "model", None),
            )

        if sub == "session":
            p = argparse.ArgumentParser(prog="rbit session", description="Manage sessions")
            sub2 = p.add_subparsers(dest="session_cmd")
            sub2.add_parser("list", help="List sessions")
            del_p = sub2.add_parser("delete", help="Delete a session by id or prefix")
            del_p.add_argument("session_id", help="Session id or prefix")
            imp_p = sub2.add_parser("import", help="Import a session from JSON/Markdown/URL")
            imp_p.add_argument("file", help="Path or URL")
            imp_p.add_argument("--overwrite", action="store_true", help="Replace an existing session with the same id")
            exp_p = sub2.add_parser("export", help="Export a session")
            exp_p.add_argument("session", nargs="?", help="Session id (defaults to latest)")
            exp_p.add_argument("-o", "--output", help="Output path")
            p.add_argument("-d", "--dir", help="Workspace directory")
            p.add_argument("-n", "--max-count", type=_nonneg_int, default=None, help="Limit to N sessions")
            p.add_argument("--json", action="store_true", help="Output JSON")
            p.add_argument("--all", action="store_true", help="Include archived sessions")
            parsed = p.parse_args(rest)
            workspace = _resolve_workspace(parsed.dir)
            if parsed.session_cmd == "import":
                return _run_import(workspace, parsed.file, getattr(parsed, "overwrite", False))
            if parsed.session_cmd == "export":
                return _run_export(workspace, parsed.session, parsed.output)
            if parsed.session_cmd == "delete":
                storage = StorageManager(workspace)
                target = storage.find_session(parsed.session_id)
                if not target:
                    Console().print(f"[red]Session not found:[/] {markup_escape(parsed.session_id)}")
                    return 1
                storage.delete_session(target.id)
                Console().print(f"OK Deleted session `{markup_escape(target.id)}` ({markup_escape(target.title)})")
                return 0
            return _run_sessions_list(
                workspace,
                max_count=parsed.max_count,
                as_json=parsed.json,
                include_archived=parsed.all,
            )

        if sub == "web":
            p = argparse.ArgumentParser(prog="rbit web", description="Start the rbit backend and open the desktop UI in a browser")
            p.add_argument("--port", type=_port_arg, default=8333)
            p.add_argument("-d", "--dir", help="Workspace directory")
            p.add_argument("--allow-outside", action="store_true")
            parsed = p.parse_args(rest)
            return _run_serve(parsed, hostname="127.0.0.1", open_browser=True)

        if sub == "pr":
            p = argparse.ArgumentParser(prog="rbit pr", description="Fetch and checkout a GitHub PR, then start rbit there")
            p.add_argument("number", type=int, help="PR number")
            p.add_argument("-d", "--dir", help="Repository directory")
            parsed = p.parse_args(rest)
            return _run_pr(_resolve_workspace(parsed.dir), parsed.number)

        if sub == "agent":
            p = argparse.ArgumentParser(prog="rbit agent", description="Manage agents (profiles and skills)")
            sub2 = p.add_subparsers(dest="agent_cmd")
            sub2.add_parser("list", help="List available agents")
            create_p = sub2.add_parser("create", help="Create a skill agent (.rbit/skills/<name>.md)")
            create_p.add_argument("name", help="Agent/skill name")
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_agent(_resolve_workspace(parsed.dir), parsed.agent_cmd or "list", getattr(parsed, "name", None))

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
            Console().print(f"Backed up .rbit → {markup_escape(str(out))}")
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

        if sub == "status":
            p = argparse.ArgumentParser(prog="rbit status", description="One-screen workspace/runtime status")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--json", action="store_true")
            parsed = p.parse_args(rest)
            return _run_status(_resolve_workspace(parsed.dir), parsed.json)

        if sub == "risk":
            p = argparse.ArgumentParser(prog="rbit risk", description="Score the risk of the current git diff")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--json", action="store_true", help="Output JSON")
            parsed = p.parse_args(rest)
            return _run_risk(_resolve_workspace(parsed.dir), parsed.json)

        if sub == "replay":
            p = argparse.ArgumentParser(prog="rbit replay", description="Replay a saved session timeline")
            p.add_argument("session_id", nargs="?", help="Session id or prefix")
            p.add_argument("-d", "--dir", help="Workspace directory")
            p.add_argument("--json", action="store_true")
            parsed = p.parse_args(rest)
            return _run_replay(_resolve_workspace(parsed.dir), parsed.session_id, parsed.json)

        if sub == "stats":
            p = argparse.ArgumentParser(prog="rbit stats")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--days", type=int, default=None, help="Stats for the last N days (0 = today)")
            p.add_argument("--year", type=int, default=None, help="Stats for a calendar year")
            p.add_argument("--all", action="store_true", help="Include archived sessions/lifetime")
            p.add_argument("--project", default=None, help="Project filter ('.' for current)")
            p.add_argument("--models", nargs="?", const=5, default=None, type=int, help="Show per-model stats (top N)")
            p.add_argument("--tools", action="store_true", help="Show tool reliability")
            p.add_argument("--cost", action="store_true", help="Show cost details")
            p.add_argument("--full", action="store_true", help="Show every detailed section")
            p.add_argument("--limit", type=int, default=5, help="Rows in detailed sections")
            p.add_argument("--json", action="store_true", help="Output statistics as JSON")
            parsed = p.parse_args(rest)
            return _run_stats(
                _resolve_workspace(parsed.dir),
                parsed.days,
                parsed.models,
                year=parsed.year,
                as_json=parsed.json,
                tools=parsed.tools,
                cost=parsed.cost,
                full=parsed.full,
                limit=parsed.limit,
                include_archived=parsed.all,
            )

        if sub == "sessions":
            p = argparse.ArgumentParser(prog="rbit sessions")
            p.add_argument("dir", nargs="?", help="Workspace directory")
            p.add_argument("--prune", metavar="AGE", help="Delete sessions/checkpoints older than e.g. 30d, 12h, 2w")
            p.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation (needed for --prune 0)")
            p.add_argument("-n", "--max-count", type=_nonneg_int, default=None, help="Limit to N most recent sessions")
            p.add_argument("--search", metavar="QUERY", help="Search past sessions by content/title")
            p.add_argument("--json", action="store_true", help="Output JSON")
            p.add_argument("--all", action="store_true", help="Include archived sessions")
            parsed = p.parse_args(rest)
            workspace = _resolve_workspace(parsed.dir)
            if parsed.prune:
                return _run_sessions_prune(workspace, parsed.prune, parsed.yes)
            if parsed.search:
                storage = StorageManager(workspace)
                hits = storage.search_sessions(
                    parsed.search, limit=parsed.max_count or 10, include_archived=parsed.all
                )
                if parsed.json:
                    payload = [
                        {
                            "id": h["session"].id,
                            "title": h["session"].title,
                            "score": h["score"],
                            "snippet": h["snippet"],
                        }
                        for h in hits
                    ]
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                    return 0
                c = Console()
                if not hits:
                    c.print("[#808080]No matching sessions found.[/]")
                    return 0
                for h in hits:
                    s = h["session"]
                    c.print(f"[bold]{s.id}[/] ({h['score']}) — {markup_escape(s.title)}  [dim]{markup_escape(h['snippet'])}[/]")
                return 0
            return _run_sessions_list(
                workspace,
                max_count=parsed.max_count,
                as_json=parsed.json,
                include_archived=parsed.all,
            )

        if sub == "export":
            p = argparse.ArgumentParser(prog="rbit export")
            p.add_argument("session_id", nargs="?", help="Session ID (defaults to latest)")
            p.add_argument("-o", "--output", help="Output path (markdown/html/openai)")
            p.add_argument("--format", choices=["markdown", "html", "openai"], default=None)
            p.add_argument("-d", "--dir", help="Workspace directory")
            parsed = p.parse_args(rest)
            return _run_export(_resolve_workspace(parsed.dir), parsed.session_id, parsed.output, fmt=parsed.format)

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
            "  rbit init [dir]        Create RBIT.md + AGENTS.md + .rbit/memory.md from project scan\n"
            "  rbit commit [dir]      Draft a commit message from git diff (--apply to commit)\n"
            "  rbit service start     Start/stop/status the background server (start|stop|status|restart)\n"
            "  rbit api <op|METHOD path>   Call the running server (OpenAPI operations)\n"
            "  rbit plugin add|list|check|update|remove   Manage package plugins\n"
            "  rbit debug agents|config|paths|heap-dump   Debugging tools\n"
            "  rbit schedule list|add|remove|run-due   Manage scheduled prompts\n"
            "  rbit risk [dir]       Score risky patterns in the current git diff\n"
            "  rbit replay [id]      Replay a session timeline\n"
            "  rbit auth login|list|switch|export|import   Multi-credential management\n"
            "  rbit test --changed --run    Select and run tests impacted by the current diff\n"
            "  rbit gh issue|pr <number>   Fetch GitHub issue/PR context via gh\n"
            "  rbit mini              Minimal interactive interface\n"
            "  rbit pair              Pair a browser/app with the running server\n"
            "  rbit serve             Local HTTP API (NDJSON turns, OpenAPI at /openapi.json)\n"
            "  rbit mobile            LAN server for the native rbit iPhone app (prints rbit:// link)\n"
            "  rbit acp               Agent Client Protocol server over stdio (Zed integration)\n"
            "  rbit mcp list|test     List or test MCP servers; add/remove/auth manage config\n"
            "  rbit update|upgrade    Check PyPI for a newer release (--check to skip install)\n"
            "  rbit uninstall         Uninstall rbit, keeping ~/.rbit data\n"
            "  rbit reload            Reload config in the background service\n"
            "  rbit --generate-completion bash|zsh|powershell   Print shell completion script\n"
            "\n"
            "Global flags: --quiet (warnings only), --verbose (info), --trace (debug)\n"
            "              --profile <name> applies a named overlay from config.json\n"
            "              --allow-outside permits file access outside the workspace\n"
            "              --auto/--yolo auto-approve permission requests"
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
    parser.add_argument("--auto", "--yolo", dest="auto_approve", action="store_true", help="Auto-approve permission requests")

    parsed = parser.parse_args(args_list)
    if parsed.version:
        print(f"rbit v{__version__}")
        return 0

    if parsed.target and not Path(parsed.target).expanduser().is_dir():
        Console(stderr=True).print(
            f"[red]Unknown command or directory:[/] {markup_escape(parsed.target)}. Run `rbit --help` for the list of commands."
        )
        return 2
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        Console(stderr=True).print(
            "[red]The interactive UI needs a terminal.[/] Use `rbit run \"prompt\"` for non-interactive use."
        )
        return 2
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




def _run_mcp(args: argparse.Namespace) -> int:
    """Implements `rbit mcp list|test|add|remove`."""
    from .mcp import MCPManager

    workspace = _resolve_workspace(getattr(args, "dir", None))
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    configs = cfg.get("mcp")
    if not isinstance(configs, dict):
        configs = {}
    legacy = cfg.get("mcp_servers")
    if not isinstance(legacy, dict):
        legacy = {}
    console = Console()
    action = args.action

    if action == "add":
        name = (args.name or "").strip()
        if not name:
            console.print("[bold #e06c75]Usage:[/] rbit mcp add <name> <command…> | --url <url>")
            return 2
        if args.url:
            configs[name] = {"type": "remote", "url": args.url}
        else:
            command = [part for part in args.command if part != "--"]
            if not command:
                console.print("[bold #e06c75]Missing command or --url.[/]")
                return 2
            configs[name] = {"type": "local", "command": command}
        headers = _parse_pairs(getattr(args, "header", None))
        if headers:
            configs[name]["headers"] = headers
        environment = _parse_pairs(getattr(args, "env", None))
        if environment:
            configs[name]["environment"] = environment
        cfg["mcp"] = configs
        storage.save_config(cfg)
        console.print(f"[bold #7fd88f]✓[/] MCP server added: {markup_escape(name)}")
        return 0

    if action == "auth":
        name = (args.name or "").strip()
        if not name:
            console.print("[bold #e06c75]Usage:[/] rbit mcp auth <name> [--token TOKEN]")
            return 2
        token = getattr(args, "token", None)
        if not token:
            import getpass

            token = getpass.getpass("MCP OAuth/bearer token: ").strip()
        if not token:
            console.print("[red]No token entered.[/]")
            return 1
        tokens = cfg.get("mcp_oauth")
        if not isinstance(tokens, dict):
            tokens = {}
        tokens[name] = token
        cfg["mcp_oauth"] = tokens
        storage.save_config(cfg)
        console.print(f"stored MCP token for {markup_escape(name)}")
        return 0

    if action == "logout":
        name = (args.name or "").strip()
        tokens = cfg.get("mcp_oauth")
        if not isinstance(tokens, dict):
            tokens = {}
        existed = name in tokens
        tokens.pop(name, None)
        cfg["mcp_oauth"] = tokens
        storage.save_config(cfg)
        console.print(("removed MCP token for " if existed else "no stored token for ") + (markup_escape(name) or "(all)"))
        return 0 if existed else 1

    if action == "remove":
        name = (args.name or "").strip()
        if not name:
            console.print("[bold #e06c75]Usage:[/] rbit mcp remove <name>")
            return 2
        existed = name in configs or name in legacy
        configs.pop(name, None)
        legacy.pop(name, None)
        cfg["mcp"] = configs
        cfg["mcp_servers"] = legacy
        storage.save_config(cfg)
        console.print(("[bold #7fd88f]✓[/] Removed " if existed else "[dim]Not found:[/] ") + markup_escape(name))
        return 0 if existed else 1

    async def _run() -> int:
        manager = MCPManager.from_config(cfg, workspace)
        await manager.start()
        try:
            if not manager.clients:
                console.print("[dim]No MCP servers configured. Add one with `rbit mcp add <name> <command…>`.[/]")
                return 0
            table = Table(title="MCP servers", show_lines=False)
            table.add_column("Server")
            table.add_column("Transport")
            table.add_column("Tools")
            table.add_column("Status")
            for name, status in manager.status.items():
                if status.get("connected"):
                    table.add_row(markup_escape(name), str(status.get("transport")), str(status.get("tools")), "[#7fd88f]connected[/]")
                else:
                    table.add_row(
                        markup_escape(name),
                        str(status.get("transport")),
                        "0",
                        f"[#e06c75]{markup_escape(str(status.get('error') or 'not connected'))}[/]",
                    )
            console.print(table)
            if action == "test" and manager.tools:
                console.print("\n[bold]Tools[/]")
                for exposed, tool in manager.tools.items():
                    console.print(f"  • [bold]{markup_escape(exposed)}[/] — {markup_escape(tool.description[:100])}")
            failed = [name for name, status in manager.status.items() if not status.get("connected")]
            return 1 if failed else 0
        finally:
            await manager.close()

    return asyncio.run(_run())


API_OPERATIONS: dict[str, tuple[str, str]] = {
    "health": ("GET", "/health"),
    "status.get": ("GET", "/v1/status"),
    "models.list": ("GET", "/v1/models"),
    "agents.list": ("GET", "/v1/agents"),
    "sessions.list": ("GET", "/v1/sessions"),
    "sessions.create": ("POST", "/v1/sessions"),
    "sessions.get": ("GET", "/v1/sessions/{id}"),
    "sessions.delete": ("DELETE", "/v1/sessions/{id}"),
    "sessions.rename": ("POST", "/v1/sessions/{id}/rename"),
    "sessions.abort": ("POST", "/v1/sessions/{id}/abort"),
    "turns.create": ("POST", "/v1/turns"),
    "approvals.create": ("POST", "/v1/approvals"),
    "config.reload": ("POST", "/v1/reload"),
    "artifacts.list": ("GET", "/v1/artifacts"),
    "artifacts.create": ("POST", "/v1/artifacts"),
    "artifacts.get": ("GET", "/v1/artifacts/{name}"),
    "artifacts.delete": ("DELETE", "/v1/artifacts/{name}"),
    "schedules.list": ("GET", "/v1/schedules"),
    "schedules.create": ("POST", "/v1/schedules"),
    "schedules.delete": ("DELETE", "/v1/schedules/{id}"),
    "fs.read": ("GET", "/v1/fs"),
    "usage.get": ("GET", "/v1/usage"),
    "config.get": ("GET", "/v1/config"),
    "skills.list": ("GET", "/v1/skills"),
    "vcs.status": ("GET", "/v1/vcs/status"),
}


def _parse_pairs(values: list[str] | None) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in values or []:
        if "=" in item:
            key, value = item.split("=", 1)
        elif ":" in item:
            key, value = item.split(":", 1)
        else:
            continue
        pairs[key.strip()] = value.strip()
    return pairs


def _run_service(args: argparse.Namespace) -> int:
    from rbit.service import ServiceManager, ServiceStartError

    console = Console()
    manager = ServiceManager()
    workspace = _resolve_workspace(getattr(args, "dir", None))
    cmd = args.service_cmd or "status"
    settings = manager.get_settings()
    host = getattr(args, "hostname", None) or str(settings.get("host") or "127.0.0.1")
    port = getattr(args, "port", None) or (int(settings["port"]) if str(settings.get("port") or "").isdigit() else None)
    if cmd == "start":
        try:
            state = manager.start(workspace, host=host, port=port)
        except ServiceStartError as exc:
            console.print(f"[red]{markup_escape(str(exc))}[/]")
            return 1
        console.print(f"[#7fd88f]service started[/] pid={state.pid} url={state.url} log={markup_escape(state.log)}")
        return 0
    if cmd == "stop":
        stopped = manager.stop()
        console.print("service stopped" if stopped else "service is not running")
        return 0
    if cmd == "restart":
        state = manager.restart(workspace, host=host, port=port)
        console.print(f"[#7fd88f]service restarted[/] pid={state.pid} url={state.url}")
        return 0
    if cmd == "status":
        status = manager.status()
        if not status.get("running"):
            console.print("service: stopped")
            return 1
        console.print(
            f"service: running pid={status['pid']} url={status['url']} "
            f"healthy={'yes' if status.get('healthy') else 'no'} workspace={status.get('workspace')}"
        )
        return 0 if status.get("healthy") else 1
    if cmd == "get":
        key = getattr(args, "key", None)
        if key:
            console.print(json.dumps({key: manager.get_setting(key)}, ensure_ascii=False))
        else:
            console.print(json.dumps(settings, ensure_ascii=False, indent=2))
        return 0
    if cmd == "set":
        manager.set_setting(args.key, args.value)
        console.print(f"service setting saved: {markup_escape(str(args.key))}={markup_escape(str(args.value))}")
        return 0
    if cmd == "unset":
        removed = manager.unset_setting(args.key)
        console.print(f"service setting removed: {markup_escape(str(args.key))}" if removed else f"not set: {markup_escape(str(args.key))}")
        return 0 if removed else 1
    console.print(f"unknown service command: {cmd}")
    return 2


def _run_api(args: argparse.Namespace) -> int:
    import httpx

    from rbit.service import ServiceManager, api_request

    console = Console()
    request = args.request
    if len(request) >= 2:
        method, path = request[0].upper(), request[1]
    elif len(request) == 1:
        needle = request[0]
        if needle.startswith("/"):
            method, path = "GET", needle
        elif needle.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            console.print("[red]Path is required with an HTTP method.[/]")
            return 2
        elif needle in API_OPERATIONS:
            method, path = API_OPERATIONS[needle]
        else:
            console.print(f"[red]Unknown operation:[/] {markup_escape(str(needle))}")
            console.print("Operations: " + ", ".join(sorted(API_OPERATIONS)))
            return 2
    else:
        console.print("[red]Usage:[/] rbit api <operation | METHOD path>")
        return 2
    server = getattr(args, "server", None)
    if server:
        url = server.rstrip("/")
    else:
        state = ServiceManager().running()
        if state is None:
            console.print("[red]No running service.[/] Start one with `rbit service start` or pass --server URL.")
            return 1
        url = state.url
    params = _parse_pairs(getattr(args, "param", None))
    for key, value in list(params.items()):
        placeholder = "{" + key + "}"
        if placeholder in path:
            path = path.replace(placeholder, value)
            params.pop(key)
    headers = _parse_pairs(getattr(args, "header", None))
    if not server:
        state = ServiceManager().running()
        if state is not None and state.token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {state.token}"
    raw_data = getattr(args, "data", None)
    data: Any = None
    if raw_data:
        try:
            data = json.loads(raw_data)
        except ValueError:
            data = raw_data
    try:
        status_code, body = api_request(url, method, path, data=data, headers=headers, params=params)
    except httpx.HTTPError as exc:
        console.print(f"[red]Request failed:[/] {markup_escape(str(exc))}")
        return 1
    try:
        parsed = json.loads(body)
        console.print_json(data=parsed)
    except ValueError:
        print(body)
    return 0 if status_code < 400 else 1


def _run_plugin(args: argparse.Namespace) -> int:
    from rbit.plugin_manager import PluginManager

    console = Console()
    workspace = _resolve_workspace(getattr(args, "dir", None))
    manager = PluginManager(workspace)
    cmd = args.plugin_cmd or "list"
    if cmd == "list":
        entries = manager.list_plugins()
        if not entries:
            console.print("No package plugins configured.")
            return 0
        table = Table(title="rbit plugins", border_style="#282828")
        table.add_column("Target", style="bold #fabeee")
        table.add_column("Source")
        table.add_column("Status")
        for entry in entries:
            table.add_row(markup_escape(str(entry["target"])), markup_escape(str(entry["source"])), markup_escape(str(entry.get("status") or "configured")))
        console.print(table)
        return 0
    if cmd == "add":
        ok, message = manager.add(args.package)
        console.print(("[#7fd88f]OK[/] " if ok else "[#e06c75]FAIL[/] ") + message)
        return 0 if ok else 1
    if cmd == "check":
        updates = manager.check(args.target)
        if not updates:
            console.print("No updates available.")
            return 0
        for item in updates:
            console.print(markup_escape(f"{item['target']}: {item.get('current') or '?'} -> {item.get('latest') or '?'}"))
        return 0
    if cmd == "update":
        result = manager.update(args.target)
        for line in result:
            console.print(line)
        return 0
    if cmd == "remove":
        ok, message = manager.remove(args.package)
        console.print(("[#7fd88f]OK[/] " if ok else "[#e06c75]FAIL[/] ") + message)
        return 0 if ok else 1
    return 2


def _run_debug(args: argparse.Namespace) -> int:
    console = Console()
    workspace = _resolve_workspace(getattr(args, "dir", None))
    storage = StorageManager(workspace)
    cmd = args.debug_cmd or "paths"
    if cmd == "agents":
        from rbit.skills import load_skills
        from rbit.subagents import load_subagents

        agents = load_subagents(workspace, storage.load_config())
        console.print("[bold #eeeeee]Primary agents[/]")
        console.print("  - build: full access (default)")
        console.print("  - plan: read-only planning")
        console.print("[bold #eeeeee]Subagents[/]")
        for name, definition in sorted(agents.items()):
            console.print(f"  - {markup_escape(str(name))}: {markup_escape(str(definition.description or '(no description)'))}")
        skills = load_skills(workspace)
        if skills:
            console.print("[bold #eeeeee]Skills[/]")
            for name, meta in sorted(skills.items()):
                console.print(f"  - {markup_escape(str(name))}: {markup_escape(str(meta.get('description') or '(no description)'))}")
        return 0
    if cmd == "config":
        cfg = storage.load_config()
        redacted = dict(cfg)
        if redacted.get("api_key"):
            redacted["api_key"] = "***"
        for profile in (redacted.get("profiles") or {}).values():
            if isinstance(profile, dict) and profile.get("api_key"):
                profile["api_key"] = "***"
        console.print(f"[bold]Config file:[/] {markup_escape(str(storage.config_path))}")
        console.print(f"[bold]Workspace:[/] {markup_escape(str(workspace))}")
        console.print(f"[bold]Profile env:[/] {markup_escape(os.environ.get('RBIT_PROFILE') or '(none)')}")
        console.print_json(data=redacted)
        return 0
    if cmd == "paths":
        home = Path.home()
        paths = {
            "home": str(home),
            "data": str(home / ".rbit"),
            "config": str(storage.config_path),
            "workspace": str(workspace),
            "sessions": str(storage.sessions_dir),
            "checkpoints": str(storage.checkpoints_dir),
            "service": str(home / ".rbit" / "service.json"),
            "tmp": str(Path(tempfile.gettempdir()) / "rbit"),
            "log": str(home / ".rbit" / "service.log"),
        }
        console.print_json(data=paths)
        return 0
    if cmd == "heap-dump":
        import gc
        import tracemalloc

        out = Path(args.output) if getattr(args, "output", None) else storage.state_dir / "heap.snapshot"
        out.parent.mkdir(parents=True, exist_ok=True)
        tracemalloc.start(10)
        gc.collect()
        snapshot = tracemalloc.take_snapshot()
        snapshot.dump(str(out))
        tracemalloc.stop()
        console.print(f"heap snapshot: {out}")
        return 0
    console.print(f"unknown debug command: {cmd}")
    return 2


def _run_uninstall(dry_run: bool = False, force: bool = False) -> int:
    console = Console()
    try:
        from importlib.metadata import distribution

        dist = distribution("rbit")
        location = Path(dist.locate_file(""))
    except Exception:
        location = None
    console.print("[bold #fab283]rbit uninstall[/]")
    console.print(f"  package location: {location or '(unknown)'}")
    console.print(f"  data directory:   {Path.home() / '.rbit'} (kept)")
    if dry_run:
        console.print("[dim]dry run - nothing removed[/]")
        return 0
    if not _confirm(console, "Uninstall rbit", force):
        console.print("cancelled")
        return 1
    proc = subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "rbit"], capture_output=True, text=True)
    if proc.returncode == 0:
        console.print("[#7fd88f]rbit uninstalled. Session data was kept in ~/.rbit.[/]")
        return 0
    console.print(f"[#e06c75]uninstall failed:[/] {markup_escape((proc.stderr or proc.stdout).strip()[-500:])}")
    return 1


def _run_reload(server: str | None = None) -> int:
    import httpx

    from rbit.service import ServiceManager

    console = Console()
    token = os.environ.get("RBIT_SERVER_TOKEN", "")
    if server:
        url = server.rstrip("/")
    else:
        state = ServiceManager().running()
        if state is None:
            console.print("[red]No running service.[/] Start one with `rbit service start`.")
            return 1
        url = state.url
        token = state.token
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.post(f"{url}/v1/reload", headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        console.print(f"[red]reload failed:[/] {markup_escape(str(exc))}")
        return 1
    console.print(f"reload: HTTP {response.status_code}")
    return 0 if response.status_code < 400 else 1


def _run_pair(url: str | None, remote: bool, yes: bool) -> int:
    import secrets
    import urllib.parse

    from rbit.service import ServiceManager

    console = Console()
    manager = ServiceManager()
    state = manager.running()
    if state is None and not url:
        console.print("[red]No running service.[/] Start one with `rbit service start` or pass --url.")
        return 1
    bases: list[str]
    if url:
        bases = [url.rstrip("/")]
    elif state is not None and state.host in ("0.0.0.0", "::"):
        bases = [f"http://{ip}:{state.port}" for ip in _lan_ips()] or [state.url]
    else:
        bases = [state.url]  # type: ignore[union-attr]
    if remote and not url and not os.environ.get("RBIT_PUBLIC_URL"):
        if not yes:
            console.print(
                "[red]Remote pairing requires --url or RBIT_PUBLIC_URL.[/] "
                "Expose the server with your tunnel/provider, then rerun with the public URL."
            )
            return 1
        bases = [os.environ.get("RBIT_PUBLIC_URL", bases[0])]
    token = secrets.token_urlsafe(24)
    manager.set_setting("pair_token", token)
    if state is not None and state.token:
        try:
            import httpx

            httpx.post(
                f"{state.url}/v1/reload",
                headers={"Authorization": f"Bearer {state.token}"},
                timeout=3.0,
            )
        except Exception:  # noqa: BLE001 - reload is best effort
            pass
    query = urllib.parse.urlencode({"pair": token})
    for base in bases:
        console.print(f"[bold #7fd88f]Pairing link:[/] {base}/?{query}")
        console.print(
            f"[dim]App link:[/] rbit://connect?{query}&url={urllib.parse.quote(base, safe='')}"
        )
    return 0


async def _run_mini(args: argparse.Namespace) -> int:
    console = Console()
    workspace = _resolve_workspace(getattr(args, "dir", None))
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    model_id = getattr(args, "model", None) or cfg.get("model", "rbit/smart-local")
    if getattr(args, "continue_last", False):
        sessions = storage.list_sessions()
        session = sessions[0] if sessions else Session()
    elif getattr(args, "session", None):
        session = storage.find_session(args.session) or Session()
    else:
        session = Session()
    executor = ToolExecutor(workspace=workspace, session=session, storage=storage, get_config=lambda: cfg)
    executor.always_allow_bash = True
    engine = AgentEngine(workspace=workspace, executor=executor, get_config=lambda: cfg, storage=storage)
    console.print("[bold #fab283]rbit mini[/] [dim]type /exit to quit[/]")

    async def _tool_start(rec: ToolCallRecord) -> None:
        console.print(f"[#808080]  ~ {markup_escape(rec.name)}[/] [dim]{markup_escape(json.dumps(rec.arguments, ensure_ascii=False))}[/]")

    async def _tool_end(rec: ToolCallRecord) -> None:
        console.print(f"[#808080]  ✓ {markup_escape(rec.name)}: {markup_escape(str(rec.summary))}[/]")

    while True:
        try:
            text = await asyncio.to_thread(input, "rbit> ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        text = text.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        user_msg = ChatMessage(id=short_id(), role="user", content=text, agent=getattr(args, "agent", "build"))
        session.messages.append(user_msg)
        try:
            reply = await engine.run_turn(
                session=session,
                user_msg=user_msg,
                agent_mode=getattr(args, "agent", "build") or "build",
                model_id=model_id,
                on_tool_start=_tool_start,
                on_tool_end=_tool_end,
                on_status=lambda _status: asyncio.sleep(0),
            )
        except KeyboardInterrupt:
            console.print("[dim]cancelled[/]")
            continue
        session.messages.append(reply)
        storage.save_session(session)
        console.print(RichMarkdown(reply.content))
    return 0


def _run_schedule(args: argparse.Namespace) -> int:
    from rbit.scheduler import Scheduler

    console = Console()
    workspace = _resolve_workspace(getattr(args, "dir", None))
    scheduler = Scheduler(workspace)
    cmd = args.schedule_cmd or "list"
    if cmd == "list":
        rows = scheduler.describe()
        console.print(markup_escape("\n".join(rows)) if rows else "No schedules.")
        return 0
    if cmd == "add":
        entry, error = scheduler.add(
            args.prompt,
            args.schedule,
            name=args.name or "",
            model=args.model or "",
            agent=args.agent or "build",
        )
        if entry is None:
            console.print(f"[red]schedule:[/] {markup_escape(str(error))}")
            return 1
        console.print(f"created {entry['id']} ({markup_escape(str(entry['schedule']))})")
        return 0
    if cmd == "remove":
        removed = scheduler.remove(args.target)
        console.print(("removed " if removed else "not found: ") + markup_escape(args.target))
        return 0 if removed else 1
    if cmd == "run-due":
        return asyncio.run(_run_schedule_due(workspace))
    console.print(f"unknown schedule command: {cmd}")
    return 2


async def _execute_schedule_entry(
    workspace: Path, storage: StorageManager, cfg: dict[str, Any], entry: dict[str, Any]
) -> str:
    """Run one scheduled prompt as an agent turn; returns the reply text."""
    session = Session(title=f"Schedule: {entry.get('name') or entry.get('id')}")
    executor = ToolExecutor(workspace=workspace, session=session, storage=storage, get_config=lambda: cfg)
    executor.always_allow_bash = True
    engine = AgentEngine(workspace=workspace, executor=executor, get_config=lambda: cfg, storage=storage)
    user = ChatMessage(id=short_id(), role="user", content=str(entry.get("prompt") or ""))
    session.messages.append(user)

    async def _noop(_record: Any) -> None:
        return None

    async def _status(_text: str) -> None:
        return None

    model_id = str(entry.get("model") or cfg.get("model", "rbit/smart-local"))
    reply = await engine.run_turn(
        session=session,
        user_msg=user,
        agent_mode=str(entry.get("agent") or "build"),
        model_id=model_id,
        on_tool_start=_noop,
        on_tool_end=_noop,
        on_status=_status,
    )
    session.messages.append(reply)
    storage.save_session(session)
    return reply.content


async def _run_schedule_due(workspace: Path) -> int:
    from rbit.scheduler import Scheduler

    console = Console()
    scheduler = Scheduler(workspace)
    from rbit.scheduler import acquire_lock

    lock_path = workspace / ".rbit" / "schedules.lock"
    if not acquire_lock(lock_path):
        console.print("Another schedule runner is active.")
        return 1
    try:
        entries = scheduler.due()
        if not entries:
            console.print("No scheduled prompts are due.")
            return 0
        storage = StorageManager(workspace)
        cfg = storage.load_config()
        ran = 0
        for entry in entries:
            label = markup_escape(str(entry.get("name") or entry.get("id")))
            recurring = str(entry.get("schedule") or "").lower().startswith(("every", "daily")) or len(
                str(entry.get("schedule") or "").split()
            ) == 5
            next_run = scheduler.next_run(str(entry.get("schedule") or ""), after=time.time() + 1) if recurring else None
            try:
                reply_text = await _execute_schedule_entry(workspace, storage, cfg, entry)
                console.print(f"ran {label}: {markup_escape(reply_text[:200])}")
                ran += 1
            except Exception as exc:  # noqa: BLE001 - one failing schedule must not stop the others
                console.print(f"[red]schedule {label} failed:[/] {markup_escape(f'{type(exc).__name__}: {exc}')}")
            try:
                scheduler.mark_run(str(entry.get("id")), next_run=next_run)
            except Exception as exc:  # noqa: BLE001 - never crash the runner on a bookkeeping write
                console.print(f"[red]could not record run for {label}:[/] {markup_escape(str(exc))}")
        return 0 if ran else 1
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _config_redacted(cfg: dict[str, Any]) -> dict[str, Any]:
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ("***" if key in ("api_key", "token", "server_token") and value[key] else scrub(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return scrub(dict(cfg))


def _config_get(cfg: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    current: Any = cfg
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _config_set(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    current = cfg
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


#: (min, max) bounds for numeric top-level keys (None = unbounded).
CONFIG_RANGES: dict[str, tuple[float | None, float | None]] = {
    "max_tokens": (1, None),
    "auto_compact_at": (1, 100),
    "budget_usd": (0, None),
}
CONFIG_ENUMS: dict[str, tuple[str, ...]] = {"effort": ("auto", "low", "medium", "high")}


def _config_value_error(key: str, value: Any) -> str | None:
    """Validate a top-level config value against the schema; returns an error or None."""
    expected = CONFIG_TYPES.get(key)
    if expected is not None and (
        not isinstance(value, expected) or (isinstance(value, bool) and bool not in _as_tuple(expected))
    ):
        names = "/".join(t.__name__ for t in _as_tuple(expected))
        return f"{key}: expected {names}, got {type(value).__name__}"
    if key in ("model", "api_base") and isinstance(value, str) and not value.strip():
        return f"{key}: must not be empty"
    if key in CONFIG_ENUMS and value not in CONFIG_ENUMS[key]:
        return f"{key}: must be one of {', '.join(CONFIG_ENUMS[key])}"
    if key in CONFIG_RANGES:
        low, high = CONFIG_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{key}: expected a number, got {type(value).__name__}"
        if low is not None and value < low:
            return f"{key}: must be >= {low}"
        if high is not None and value > high:
            return f"{key}: must be <= {high}"
    return None


def _as_tuple(expected: type | tuple[type, ...]) -> tuple[type, ...]:
    return expected if isinstance(expected, tuple) else (expected,)


CONFIG_TYPES: dict[str, type | tuple[type, ...]] = {
    "model": str,
    "api_base": str,
    "api_key": str,
    "theme": str,
    "effort": str,
    "auto_compact": bool,
    "auto_approve": bool,
    "auto_design_skill": bool,
    "formatter": (bool, dict),
    "lsp": (bool, dict),
    "websearch": (bool, dict),
    "permissions": list,
    "plugins": list,
    "instructions": list,
}


def _run_config(
    workspace: Path,
    action: str,
    key: str | None = None,
    value: str | None = None,
    as_json: bool = False,
) -> int:
    console = Console()
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    if action in ("list", "show"):
        print(json.dumps(_config_redacted(cfg), ensure_ascii=False, indent=2))
        return 0
    if action == "get":
        if not key:
            console.print("[red]config get requires KEY[/]")
            return 2
        found, item = _config_get(cfg, key)
        if not found:
            console.print(f"[red]Unknown key:[/] {markup_escape(str(key))}")
            return 1
        if key in ("api_key", "token", "server_token"):
            item = "***" if item else ""
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0
    if action == "set":
        if not key or value is None:
            console.print("[red]config set requires KEY VALUE[/]")
            return 2
        root = key.split(".")[0]
        words = {"true": True, "yes": True, "on": True, "1": True, "false": False, "no": False, "off": False, "0": False}
        if CONFIG_TYPES.get(root) is str and "." not in key and not value.lstrip().startswith('"'):
            parsed = value  # string-typed key: never reinterpret `123`/`true` as JSON
        elif CONFIG_TYPES.get(root) is bool and "." not in key and value.strip().lower() in words:
            parsed = words[value.strip().lower()]  # `config set auto_approve no` is False, not the text "no"
        else:
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = value
        if "." in key:
            root_type = CONFIG_TYPES.get(root)
            if root_type is not None and dict not in _as_tuple(root_type):
                console.print(f"[red]Invalid value:[/] {markup_escape(root)} does not accept nested keys")
                return 2
            problem = _config_value_error(key, parsed) if key in CONFIG_RANGES else None
        else:
            problem = _config_value_error(key, parsed)
        if problem:
            console.print(f"[red]Invalid value:[/] {markup_escape(problem)}")
            return 2
        _config_set(cfg, key, parsed)
        storage.save_config(cfg)
        console.print(f"config updated: {markup_escape(str(key))}")
        return 0
    if action == "unset":
        if not key:
            console.print("[red]config unset requires KEY[/]")
            return 2
        parts = key.split(".")
        current: Any = cfg
        for part in parts[:-1]:
            current = current.get(part) if isinstance(current, dict) else None
        if not isinstance(current, dict) or parts[-1] not in current:
            console.print(f"[red]Unknown key:[/] {markup_escape(str(key))}")
            return 1
        del current[parts[-1]]
        storage.save_config(cfg)
        console.print(f"config removed: {markup_escape(str(key))}")
        return 0
    if action == "validate":
        errors: list[str] = []
        for key_name in {*CONFIG_TYPES, *CONFIG_RANGES, *CONFIG_ENUMS}:
            if key_name not in cfg:
                continue
            problem = _config_value_error(key_name, cfg[key_name])
            if problem:
                errors.append(problem)
        if isinstance(cfg.get("permissions"), list):
            for index, rule in enumerate(cfg["permissions"]):
                if not isinstance(rule, dict) or not {"action", "resource", "effect"} <= set(rule):
                    errors.append(f"permissions[{index}]: action/resource/effect required")
        for message in errors:
            console.print(f"[red]✗[/] {markup_escape(str(message))}")
        if not errors:
            console.print("[#7fd88f]✓ config is valid[/]")
        return 1 if errors else 0
    if action == "schema":
        schema = {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "api_base": {"type": "string"},
                "api_key": {"type": "string"},
                "effort": {"type": "string", "enum": ["auto", "low", "medium", "high"]},
                "theme": {"type": "string"},
                "auto_compact": {"type": "boolean"},
                "auto_design_skill": {"type": "boolean"},
                "formatter": {"type": ["boolean", "object"]},
                "lsp": {"type": ["boolean", "object"]},
                "websearch": {"type": ["boolean", "object"]},
                "permissions": {"type": "array", "items": {"type": "object", "required": ["action", "resource", "effect"]}},
                "plugins": {"type": "array", "items": {"type": "string"}},
                "instructions": {"type": "array", "items": {"type": "string"}},
            },
        }
        print(json.dumps(schema, ensure_ascii=False, indent=2))
        return 0
    if action == "edit":
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or ("notepad" if os.name == "nt" else "vi")
        storage.state_dir.mkdir(parents=True, exist_ok=True)
        if not storage.config_path.exists():
            storage.save_config(cfg)
        try:
            return subprocess.run([*shlex.split(editor, posix=(os.name != "nt")), str(storage.config_path)], check=False).returncode
        except OSError as exc:
            console.print(f"[red]Cannot start editor:[/] {markup_escape(str(exc))}")
            return 1
    console.print(f"[red]Unknown config action:[/] {action}")
    return 2


def _run_selftest(workspace: Path, as_json: bool) -> int:
    """Runs the post-upgrade self-test (see `rbit.selftest`)."""
    from rbit.selftest import self_test

    report = self_test(workspace)
    if as_json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "checks": [
                        {"name": c.name, "ok": c.ok, "detail": c.detail, "fatal": c.fatal}
                        for c in report.checks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        Console().print(report.text())
    return 0 if report.ok else 1


def _run_skill(
    workspace: Path,
    action: str,
    source: str | None,
    name: str | None,
    force: bool,
    as_json: bool,
) -> int:
    """Installs or lists skills, recording where each came from."""
    from rbit.install import InstallError, install_skill, list_installed, verify_sources

    console = Console()
    if action == "install":
        if not source:
            console.print("[red]Usage:[/] rbit skill install <git-url|path> [--name NAME]")
            return 2
        try:
            result = install_skill(source, workspace=workspace, name=name or "", force=force)
        except InstallError as exc:
            console.print(f"[red]Install failed:[/] {markup_escape(str(exc))}")
            return 1
        console.print(result.describe())
        console.print("The skill is available immediately via `/skill`.")
        return 0

    if action == "verify":
        warnings = verify_sources(workspace)
        if as_json:
            print(json.dumps({"warnings": warnings}, ensure_ascii=False, indent=2))
            return 0
        if not warnings:
            console.print("All installed skills match their recorded source.")
            return 0
        for warning in warnings:
            console.print(f"[yellow]![/] {markup_escape(str(warning))}")
        return 0

    rows = [r for r in list_installed(workspace) if r["kind"] == "skill"]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        console.print("No skills installed. Add one: rbit skill install <git-url>")
        return 0
    table = Table(title="rbit skills", border_style="#282828")
    table.add_column("Name")
    table.add_column("Source")
    table.add_column("Remote")
    for row in rows:
        table.add_row(markup_escape(str(row["name"])), markup_escape(str(row["source"])[:60]), "yes" if row["remote"] else "no")
    console.print(table)
    return 0


def _run_plugin(
    workspace: Path,
    action: str,
    name: str | None,
    force: bool,
    approve: bool,
    as_json: bool,
) -> int:
    """Manages plugins; installed plugins stay disabled until approved."""
    from rbit.install import (
        InstallError,
        install_plugin,
        list_installed,
        remove_plugin,
        set_plugin_enabled,
    )

    console = Console()
    if action == "install":
        if not name:
            console.print("[red]Usage:[/] rbit plugin install <git-url|path> [--approve]")
            return 2
        try:
            result = install_plugin(name, workspace=workspace, force=force, approve=approve)
        except InstallError as exc:
            console.print(f"[red]Install failed:[/] {markup_escape(str(exc))}")
            return 1
        console.print(result.describe())
        if approve:
            console.print("Enabled — its hooks will run on the next tool call.")
        else:
            console.print(
                "Installed but [bold]disabled[/]. Review it, then run "
                f"`rbit plugin enable {result.name}`."
            )
        return 0

    if action in ("enable", "disable"):
        if not name:
            console.print(f"[red]Usage:[/] rbit plugin {action} <name>")
            return 2
        try:
            set_plugin_enabled(workspace, name, action == "enable")
        except InstallError as exc:
            console.print(f"[red]{markup_escape(str(exc))}[/]")
            return 1
        console.print(f"plugin `{name}` {action}d")
        return 0

    if action == "remove":
        if not name:
            console.print("[red]Usage:[/] rbit plugin remove <name>")
            return 2
        try:
            remove_plugin(workspace, name)
        except InstallError as exc:
            console.print(f"[red]{markup_escape(str(exc))}[/]")
            return 1
        console.print(f"plugin `{name}` removed")
        return 0

    rows = [r for r in list_installed(workspace) if r["kind"] == "plugin"]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        console.print("No plugins installed. Add one: rbit plugin install <git-url>")
        return 0
    table = Table(title="rbit plugins", border_style="#282828")
    table.add_column("Name")
    table.add_column("Source")
    table.add_column("Remote")
    for row in rows:
        table.add_row(markup_escape(str(row["name"])), markup_escape(str(row["source"])[:60]), "yes" if row["remote"] else "no")
    console.print(table)
    return 0


def _run_migrate(workspace: Path, dry_run: bool) -> int:
    """Upgrades `.rbit/config.json` to the current shape, keeping a backup."""
    from rbit.migrate import migrate_config

    console = Console()
    result = migrate_config(workspace, dry_run=dry_run)
    if not result.changes:
        console.print(result.summary())
        return 0
    for change in result.changes:
        console.print(f"  - {markup_escape(str(change))}")
    if result.backup is not None:
        console.print(f"backup: {result.backup}")
    console.print(result.summary())
    return 0


def _run_diag(workspace: Path, out: str | None) -> int:
    """Collects environment details with secrets removed."""
    from rbit.migrate import diagnostic_bundle

    console = Console()
    bundle = diagnostic_bundle(workspace)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bundle, encoding="utf-8")
        console.print(f"Wrote {path} ({len(bundle)} bytes, secrets redacted)")
    else:
        print(bundle)
    return 0


def _run_usage(
    workspace: Path,
    action: str,
    days: int,
    export_format: str,
    out: str | None,
    as_json: bool,
) -> int:
    """Reports or exports token/cost accounting from the usage ledger."""
    from rbit.usage import UsageLedger

    console = Console()
    ledger = UsageLedger(workspace)
    storage = StorageManager(workspace)
    config = storage.load_config()

    if action == "today":
        status = ledger.status(config)
        forecast = ledger.forecast(config)
        if as_json:
            print(
                json.dumps(
                    {
                        "day": status.day,
                        "spent_usd": round(status.spent_usd, 6),
                        "tokens": status.tokens,
                        "calls": status.calls,
                        "limit_usd": status.limit_usd,
                        "limit_tokens": status.limit_tokens,
                        "mode": status.mode,
                        "percent": round(status.percent * 100, 1),
                        "unpriced_calls": status.unpriced_calls,
                        "by_model": {k: round(v, 6) for k, v in status.by_model.items()},
                        "forecast": forecast,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        table = Table(title=f"rbit usage - {status.day}", border_style="#282828")
        table.add_column("Metric")
        table.add_column("Value")
        table.add_row("Spent", f"${status.spent_usd:.4f}")
        table.add_row("Tokens", f"{status.tokens:,}")
        table.add_row("Calls", str(status.calls))
        limit = f"${status.limit_usd:.2f}" if status.limit_usd else "none"
        table.add_row("Daily budget", f"{limit} ({status.percent * 100:.0f}% used)")
        table.add_row("Mode", status.mode)
        if status.unpriced_calls:
            table.add_row("Unpriced calls", str(status.unpriced_calls))
        hours = forecast.get("hours_left")
        if isinstance(hours, (int, float)):
            table.add_row("Budget lasts", f"~{hours:.1f}h at current rate")
        console.print(table)
        return 0

    if action == "show":
        breakdown = ledger.breakdown(days)
        if as_json:
            print(json.dumps(breakdown, ensure_ascii=False, indent=2))
            return 0
        table = Table(title=f"rbit usage - last {days} day(s)", border_style="#282828")
        table.add_column("Metric")
        table.add_column("Value")
        table.add_row("Calls", str(breakdown["calls"]))
        table.add_row("Cost", f"${breakdown['total_cost']:.4f}")
        table.add_row("Tokens", f"{breakdown['total_tokens']:,}")
        console.print(table)
        if breakdown["by_model"]:
            models = Table(title="By model", border_style="#282828")
            models.add_column("Model")
            models.add_column("Calls")
            models.add_column("Tokens")
            models.add_column("Cost")
            for name, stats in sorted(
                breakdown["by_model"].items(), key=lambda kv: -kv[1]["cost"]
            ):
                models.add_row(name, str(stats["calls"]), f"{stats['tokens']:,}", f"${stats['cost']:.4f}")
            console.print(models)
        return 0

    if action == "forecast":
        print(json.dumps(ledger.forecast(config), ensure_ascii=False, indent=2))
        return 0

    # export
    text = ledger.to_csv(days) if export_format == "csv" else ledger.to_json(days)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        console.print(f"Wrote {path} ({len(text)} bytes)")
    else:
        print(text)
    return 0


def _run_audit(
    workspace: Path,
    action: str,
    day: str | None,
    lines: int,
    as_json: bool,
    assume_yes: bool = False,
) -> int:
    """Reads the append-only agent action log (`.rbit/audit/*.jsonl`)."""
    from rbit.audit import AuditLog

    console = Console()
    log = AuditLog(workspace)
    if not log.dir.exists():
        if as_json:
            print(json.dumps({"entries": 0, "dir": str(log.dir)}))
        else:
            console.print(f"No audit log yet ({log.dir}). Every tool call is recorded once the agent runs.")
        return 0

    if action == "path":
        print(str(log.dir))
        return 0

    if action == "clear":
        if not _confirm(console, f"delete the audit log in {log.dir}", assume_yes):
            return 1
        removed = 0
        for path in log.dir.glob("*.jsonl"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        console.print(f"Removed {removed} audit file(s) from {log.dir}")
        return 0

    if action == "tail":
        entries = log.read_entries(day, limit=lines)
        if as_json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
            return 0
        if not entries:
            console.print("No entries.")
            return 0
        for entry in entries:
            ts = str(entry.get("ts") or "")[11:19]
            event = entry.get("event")
            if event == "tool":
                mark = "[green]ok[/]" if entry.get("ok") else "[red]fail[/]"
                console.print(
                    f"{ts} {mark} {entry.get('tool')} "
                    f"({entry.get('duration_ms', 0)} ms) {str(entry.get('summary') or '')[:60]}"
                )
            elif event == "permission":
                colour = "green" if entry.get("decision") == "allow" else "red"
                console.print(
                    f"{ts} perm [{colour}]{entry.get('decision')}[/] "
                    f"{markup_escape(str(entry.get('action')))} {markup_escape(str(entry.get('resource') or '')[:60])} "
                    f"({markup_escape(str(entry.get('reason')))})"
                )
            else:
                console.print(markup_escape(f"{ts} {event} {json.dumps({k: v for k, v in entry.items() if k not in ('ts', 'event')}, ensure_ascii=False)[:120]}"))
        return 0

    summary = log.summary(day)
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    table = Table(title="rbit audit log", border_style="#282828")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Entries", str(summary["entries"]))
    table.add_row("Failures", str(summary["failures"]))
    table.add_row("Location", summary["dir"])
    console.print(table)
    if summary["by_tool"]:
        tool_table = Table(title="By tool", border_style="#282828")
        tool_table.add_column("Tool")
        tool_table.add_column("Calls")
        for tool, count in sorted(summary["by_tool"].items(), key=lambda kv: -kv[1]):
            tool_table.add_row(tool, str(count))
        console.print(tool_table)
    return 0


def _run_permissions(workspace: Path, action: str, resource: str | None = None) -> int:
    from rbit.models import Session
    from rbit.permissions import (
        MODE_IDS,
        mode_table,
        normalize_permission_mode,
        permission_mode_from_config,
    )
    from rbit.tools import ToolExecutor

    console = Console()
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    executor = ToolExecutor(workspace=workspace, session=Session(), storage=storage, get_config=lambda: cfg)
    if action == "mode":
        current = permission_mode_from_config(cfg)
        if not resource:
            table = Table(title="rbit permission modes", border_style="#282828")
            table.add_column("Mode")
            table.add_column("Commands")
            table.add_column("Edits")
            table.add_column("Description")
            for mode_id, _label, description, cmd_auto, edit_auto in mode_table():
                marker = "-> " if mode_id == current else "   "
                table.add_row(
                    markup_escape(f"{marker}{mode_id}"),
                    "auto" if cmd_auto else "ask",
                    "auto" if edit_auto else "ask",
                    markup_escape(str(description)),
                )
            console.print(table)
            console.print(f"Current: [bold]{current}[/]")
            return 0
        requested = normalize_permission_mode(resource, default="")
        if not requested:
            console.print(f"[red]Unknown mode:[/] {markup_escape(str(resource))}. Use: {markup_escape(', '.join(MODE_IDS))}")
            return 2
        storage.save_config({**cfg, "permission_mode": requested})
        console.print(f"permission mode -> [bold]{requested}[/]")
        return 0
    if action in ("list", "show"):
        rules = cfg.get("permissions")
        if not rules:
            console.print("No explicit permissions. Default: allow unless a tool asks or denies.")
            return 0
        table = Table(title="rbit permissions", border_style="#282828")
        table.add_column("Action")
        table.add_column("Resource")
        table.add_column("Effect")
        for rule in rules if isinstance(rules, list) else []:
            if isinstance(rule, dict):
                table.add_row(markup_escape(str(rule.get("action"))), markup_escape(str(rule.get("resource"))), markup_escape(str(rule.get("effect"))))
        console.print(table)
        return 0
    if action in ("explain", "check"):
        if not resource:
            console.print("[red]permissions explain requires ACTION RESOURCE[/]")
            return 2
        parts = resource.split(maxsplit=1)
        if len(parts) != 2:
            console.print("[red]Use: rbit permissions explain <action> <resource>[/]")
            return 2
        action_name, resource_name = parts
        effect = executor.resolve_permission_effect(action_name, resource_name, "build")
        console.print(markup_escape(f"{action_name} {resource_name} -> {effect or 'no-rule (allow)'}"))
        return 0
    console.print(f"[red]Unknown permissions action:[/] {action}")
    return 2


def _run_completion_install(shell: str) -> int:
    console = Console()
    script = _completion_script(shell)
    if shell == "powershell":
        target = Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        if "Register-ArgumentCompleter -Native -CommandName rbit" in existing:
            console.print("completion already installed")
            return 0
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n" + script)
        console.print(f"completion installed: {target}")
        return 0
    if shell == "zsh":
        target = Path.home() / ".zshrc"
    else:
        target = Path.home() / ".bashrc"
    existing = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    marker = "_rbit_completion"
    if marker in existing:
        console.print("completion already installed")
        return 0
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n" + script)
    console.print(f"completion installed: {target}")
    return 0


def _run_logs(workspace: Path, lines: int = 200, as_json: bool = False) -> int:
    from rbit.service import ServiceManager

    console = Console()
    manager = ServiceManager()
    state = manager.read()
    path = Path(state.log) if state and state.log else (workspace / ".rbit" / "service.log")
    if not path.exists():
        console.print(f"[yellow]No log file:[/] {markup_escape(str(path))}")
        return 1
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = content[-max(1, lines) :]
    if as_json:
        print(json.dumps({"path": str(path), "lines": tail}, ensure_ascii=False, indent=2))
    else:
        console.print(f"[dim]{markup_escape(str(path))}[/]")
        console.print("\n".join(tail))
    return 0


def build_openapi_spec() -> dict:
    """OpenAPI 3.1 description of the local `rbit serve` API."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "rbit local API", "version": "1.3.1"},
        "servers": [{"url": "http://127.0.0.1:4096"}],
        "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearerAuth": []}],
        "paths": {
            "/health": {"get": {"operationId": "health", "summary": "Health probe", "responses": {"200": {"description": "OK"}}}},
            "/v1/status": {
                "get": {
                    "operationId": "status.get",
                    "summary": "Server, workspace and agent status",
                    "responses": {"200": {"description": "Status"}},
                }
            },
            "/v1/agents": {
                "get": {
                    "operationId": "agents.list",
                    "summary": "List primary agents (build, plan)",
                    "responses": {"200": {"description": "Agents"}},
                }
            },
            "/v1/models": {
                "get": {
                    "operationId": "models.list",
                    "summary": "List available models",
                    "responses": {"200": {"description": "Model list", "content": {"application/json": {}}}},
                }
            },
            "/v1/sessions": {
                "get": {
                    "operationId": "sessions.list",
                    "summary": "List sessions",
                    "responses": {"200": {"description": "Sessions"}},
                },
                "post": {
                    "operationId": "sessions.create",
                    "summary": "Create a session",
                    "requestBody": {"content": {"application/json": {}}},
                    "responses": {"200": {"description": "Session"}},
                },
            },
            "/v1/sessions/{id}": {
                "get": {
                    "operationId": "sessions.get",
                    "summary": "Read a session with messages, tool calls and todos",
                    "responses": {"200": {"description": "Session"}},
                },
                "delete": {
                    "operationId": "sessions.delete",
                    "summary": "Delete a session",
                    "responses": {"200": {"description": "Deleted"}},
                },
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
            },
            "/v1/sessions/{id}/rename": {
                "post": {
                    "operationId": "sessions.rename",
                    "summary": "Rename a session",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "requestBody": {"content": {"application/json": {}}},
                    "responses": {"200": {"description": "Renamed"}},
                }
            },
            "/v1/sessions/{id}/abort": {
                "post": {
                    "operationId": "sessions.abort",
                    "summary": "Abort a running turn",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "Abort requested"}},
                }
            },
            "/v1/reload": {
                "post": {
                    "operationId": "config.reload",
                    "summary": "Reload configuration from disk",
                    "responses": {"200": {"description": "Reloaded"}},
                }
            },
            "/v1/turns": {
                "post": {
                    "operationId": "turns.create",
                    "summary": "Run an agent turn, streaming NDJSON events",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "prompt": {"type": "string"},
                                        "session_id": {"type": "string"},
                                        "model": {"type": "string"},
                                        "agent": {"type": "string", "enum": ["build", "plan"]},
                                        "mode": {"type": "string", "enum": ["build", "plan"]},
                                        "effort": {"type": "string", "enum": ["auto", "low", "medium", "high"]},
                                    },
                                    "required": ["prompt"],
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "NDJSON stream of turn events",
                            "content": {"application/x-ndjson": {}},
                        }
                    },
                }
            },
            "/v1/approvals": {
                "post": {
                    "operationId": "approvals.create",
                    "summary": "Answer an interactive tool approval",
                    "requestBody": {"content": {"application/json": {}}},
                    "responses": {"200": {"description": "Recorded"}},
                }
            },
            "/v1/artifacts": {
                "get": {"operationId": "artifacts.list", "summary": "List artifacts", "responses": {"200": {"description": "Artifacts"}}},
                "post": {"operationId": "artifacts.create", "summary": "Create an artifact", "responses": {"201": {"description": "Created"}}},
            },
            "/v1/artifacts/{name}": {
                "get": {"operationId": "artifacts.get", "summary": "Read an artifact", "responses": {"200": {"description": "Artifact"}}},
                "delete": {"operationId": "artifacts.delete", "summary": "Delete an artifact", "responses": {"200": {"description": "Deleted"}}},
            },
            "/v1/schedules": {
                "get": {"operationId": "schedules.list", "summary": "List schedules", "responses": {"200": {"description": "Schedules"}}},
                "post": {"operationId": "schedules.create", "summary": "Create a schedule", "responses": {"201": {"description": "Created"}}},
            },
            "/v1/schedules/{id}": {
                "delete": {"operationId": "schedules.delete", "summary": "Delete a schedule", "responses": {"200": {"description": "Deleted"}}},
            },
            "/v1/fs": {
                "get": {"operationId": "fs.read", "summary": "Read a file or directory inside the workspace", "responses": {"200": {"description": "FS entry"}}},
            },
            "/v1/usage": {
                "get": {"operationId": "usage.get", "summary": "Aggregate usage", "responses": {"200": {"description": "Usage"}}},
            },
            "/v1/config": {
                "get": {"operationId": "config.get", "summary": "Effective redacted configuration", "responses": {"200": {"description": "Config"}}},
            },
            "/v1/skills": {
                "get": {"operationId": "skills.list", "summary": "List skills", "responses": {"200": {"description": "Skills"}}},
            },
            "/v1/vcs/status": {
                "get": {"operationId": "vcs.status", "summary": "Git status", "responses": {"200": {"description": "Status"}}},
            },
            "/openapi.json": {"get": {"summary": "This document", "responses": {"200": {"description": "OpenAPI"}}}},
        },
    }


def _lan_ips() -> list[str]:
    """Best-effort list of LAN IPv4 addresses, most likely first."""
    import socket as _socket

    ips: set[str] = set()
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ips.add(str(sock.getsockname()[0]))
    except OSError:
        pass
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None, _socket.AF_INET):
            ip = str(info[4][0])
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    def _score(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3

    return sorted(ips, key=lambda value: (_score(value), value))


def _mobile_links(port: int, token: str) -> list[tuple[str, str]]:
    """Returns (server URL, rbit:// connect URL) pairs for the LAN."""
    from urllib.parse import quote

    links: list[tuple[str, str]] = []
    for ip in _lan_ips():
        url = f"http://{ip}:{port}"
        app_link = (
            "rbit://connect"
            f"?url={quote(url, safe='')}"
            f"&token={quote(token, safe='')}"
        )
        links.append((url, app_link))
    return links


_SECRET_KEY_RE = re.compile(r"(api_?key|token$|secret|password|authorization|credential|oauth)", re.IGNORECASE)


def _mask_secret_values(value: Any, key: str = "") -> Any:
    """Deep copy of `value` with every secret-looking entry replaced by '***'.

    Keys are matched by name (api_key, *_token, authorization, credentials, mcp_oauth...)
    so nested MCP headers, per-profile keys and OAuth stores are covered too.
    """
    if key and _SECRET_KEY_RE.search(key):
        return "***" if value not in (None, "", {}, []) else value
    if isinstance(value, dict):
        return {k: _mask_secret_values(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_secret_values(v, key) for v in value]
    return value


def _run_serve(
    args: argparse.Namespace,
    hostname: str = "127.0.0.1",
    open_browser: bool = False,
    cors_origins: list[str] | None = None,
    mobile: bool = False,
) -> int:
    """Local HTTP endpoint: POST /v1/turns streams a full agent turn as NDJSON."""
    import json as _json
    import queue as _queue
    import re as _re
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from rbit.agent import EFFORT_LEVELS, AgentEngine
    from rbit.models import AVAILABLE_MODELS, ChatMessage, ModelInfo, Session, short_id
    from rbit.tools import ToolExecutor

    import time as _time
    from datetime import datetime as _datetime

    workspace = _resolve_workspace(args.dir)
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    model_id = str(cfg.get("model", "rbit/smart-local"))
    server_token = os.environ.get("RBIT_SERVER_TOKEN") or str(cfg.get("server_token") or "")
    pair_token = ""
    service_manager = None
    try:
        from rbit.service import ServiceManager

        service_manager = ServiceManager()
        pair_token = str(service_manager.get_setting("pair_token") or "")
    except Exception:  # noqa: BLE001 - pairing is optional
        service_manager = None
    if mobile and os.environ.get("RBIT_APPROVALS") is None:
        os.environ["RBIT_APPROVALS"] = "1"
    if mobile and not server_token:
        import secrets as _secrets

        try:
            if service_manager is not None:
                server_token = str(service_manager.get_setting("mobile_token") or "")
                if not server_token:
                    server_token = _secrets.token_urlsafe(24)
                    service_manager.set_setting("mobile_token", server_token)
            else:
                server_token = _secrets.token_urlsafe(24)
        except Exception:  # noqa: BLE001 - token persistence is optional
            server_token = _secrets.token_urlsafe(24)
    if not server_token and hostname not in ("127.0.0.1", "localhost", "::1"):
        # Listening beyond loopback without a token would hand the agent (and its
        # bash tool) to everyone on the network: always require one.
        import secrets as _secrets

        server_token = _secrets.token_urlsafe(24)

    def _token_matches(value: str, tokens: set[str]) -> bool:
        """Constant-time token comparison."""
        import hmac

        data = value.encode("utf-8", "replace")
        return any(hmac.compare_digest(data, token.encode("utf-8", "replace")) for token in tokens)

    sessions: dict[str, Session] = {}
    # Interactive tool approvals: with RBIT_APPROVALS=1 the desktop client gets
    # a `tool_approval` event for every non-read-only bash command and answers
    # via POST /v1/approvals. Without it everything runs unattended.
    approvals: dict[str, dict] = {}
    active_turns: dict[str, dict] = {}
    active_lock = _threading.Lock()
    session_lock = _threading.Lock()
    caution_re = _re.compile(
        r"\brm\s+-[a-z]*r[a-z]*f|-\w+f\w*r\b(?=\s)|\bgit\s+reset\s+--hard\b|"
        r"\bgit\s+push\b[^;\n]*--force|\bdrop\s+(table|database)\b|\bmkfs\b|\bformat\s+[a-z]:|"
        r"^\s*(del|rd|erase|remove-item)\s+\S",
        _re.IGNORECASE | _re.MULTILINE,
    )

    def _updated_at(session_id: str) -> str:
        try:
            stamp = (storage.sessions_dir / f"{session_id}.json").stat().st_mtime
        except OSError:
            stamp = _time.time()
        return _datetime.fromtimestamp(stamp).isoformat(timespec="seconds")

    def _session_summary(sess: Session) -> dict[str, Any]:
        last_assistant = next((m for m in reversed(sess.messages) if m.role == "assistant"), None)
        last_user = next((m for m in reversed(sess.messages) if m.role == "user"), None)
        preview_source = (last_assistant.content if last_assistant else "") or (last_user.content if last_user else "")
        preview = " ".join(preview_source.strip().split())[:180]
        return {
            "id": sess.id,
            "title": sess.title,
            "created_at": sess.created_at,
            "updated_at": _updated_at(sess.id),
            "messages": len(sess.messages),
            "finished": sess.finished,
            "archived": sess.archived,
            "tokens": sess.total_tokens,
            "preview": preview,
            "model": (last_assistant.model if last_assistant and last_assistant.model else model_id),
            "agent": (last_assistant.agent if last_assistant and last_assistant.agent else "build"),
        }

    def _session_payload(sess: Session, tool_limit: int = 20000) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for message in sess.messages:
            data = message.to_dict()
            for call in data.get("tool_calls") or []:
                for key in ("output", "diff"):
                    value = call.get(key)
                    if isinstance(value, str) and len(value) > tool_limit:
                        call[key] = value[:tool_limit] + "\n… [truncated]"
            messages.append(data)
        payload = _session_summary(sess)
        payload.update(
            {
                "workspace": str(workspace),
                "total_tokens": sess.total_tokens,
                "messages": messages,
                "todos": [todo.to_dict() for todo in sess.todos],
                "modified_files": {name: stat.to_dict() for name, stat in sess.modified_files.items()},
            }
        )
        payload["message_count"] = len(messages)
        payload["messages_count"] = len(messages)
        return payload

    def _settle_future(fut, value: bool) -> None:
        if not fut.done():
            fut.set_result(bool(value))

    def run_turn_events(
        prompt_text: str,
        session: Session,
        sink: "_queue.Queue",  # type: ignore[name-defined]
        model_override: str = "",
        agent_override: str = "",
        effort_override: str = "",
    ):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        turn_cfg = dict(cfg)  # per-turn copy: an `effort` override must not leak into other turns
        try:
            executor = ToolExecutor(workspace=workspace, session=session, storage=storage, get_config=lambda: turn_cfg)
            approvals_on = os.environ.get("RBIT_APPROVALS") == "1"
            executor.always_allow_bash = not approvals_on

            async def _permission(tool: str, payload: str) -> bool:
                """Asks the connected client to approve a tool call (bash)."""
                aid = short_id()
                fut = loop.create_future()
                approvals[aid] = {
                    "fut": fut,
                    "loop": loop,
                    "executor": executor,
                    "input": payload,
                }
                sink.put(
                    {
                        "type": "tool_approval",
                        "id": aid,
                        "tool": tool,
                        "input": payload,
                        "destructive": bool(caution_re.search(payload)),
                    }
                )
                try:
                    return await asyncio.wait_for(fut, timeout=180)
                except (asyncio.TimeoutError, TimeoutError):
                    return False
                finally:
                    approvals.pop(aid, None)

            if approvals_on:
                executor.permission_cb = _permission
            executor.allow_outside_paths = bool(getattr(args, "allow_outside", False))
            engine = AgentEngine(workspace=workspace, executor=executor, storage=storage, get_config=lambda: turn_cfg)

            async def on_status(text: str) -> None:
                if text:
                    sink.put({"type": "status", "text": text})

            async def on_tool_start(rec: ToolCallRecord) -> None:
                sink.put(
                    {
                        "type": "tool_call",
                        "id": rec.id,
                        "name": rec.name,
                        "arguments": rec.arguments,
                    }
                )

            async def on_tool_end(rec: ToolCallRecord) -> None:
                sink.put(
                    {
                        "type": "tool_result",
                        "id": rec.id,
                        "name": rec.name,
                        "status": rec.status,
                        "summary": rec.summary,
                        "output": rec.output,
                        "diff": rec.diff,
                        "duration_ms": rec.duration_ms,
                    }
                )

            async def on_stream_chunk(partial: str) -> None:
                sink.put({"type": "delta", "text": partial})

            async def on_thinking(active: bool) -> None:
                sink.put({"type": "thinking", "active": active})

            async def on_reasoning_chunk(piece: str) -> None:
                sink.put({"type": "reasoning", "text": piece})

            async def _turn() -> None:
                if len(session.messages) == 0 and session.title in ("", "Новая сессия", "New Session"):
                    session.title = " ".join(prompt_text.split())[:44] or "Новая сессия"
                user = ChatMessage(id=short_id(), role="user", content=prompt_text)
                session.messages.append(user)
                session.finished = False
                # Persist the user message immediately: a turn may take minutes
                # (tool approvals), and closing the app must not lose it.
                storage.save_session(session)
                sink.put(
                    {
                        "type": "session",
                        "session_id": session.id,
                        "title": session.title,
                        "created_at": session.created_at,
                    }
                )
                if effort_override in EFFORT_LEVELS:
                    turn_cfg["effort"] = effort_override
                agent_mode = agent_override if agent_override in ("build", "plan") else "build"
                reply = await engine.run_turn(
                    session=session,
                    user_msg=user,
                    agent_mode=agent_mode,
                    model_id=model_override or model_id,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    on_status=on_status,
                    on_stream_chunk=on_stream_chunk,
                    on_thinking=on_thinking,
                    on_reasoning_chunk=on_reasoning_chunk,
                )
                # run_turn returns the assistant reply without appending it to
                # the session — persist it, or history keeps only user messages.
                session.messages.append(reply)
                session.finished = True
                storage.save_session(session)
                sink.put(
                    {
                        "type": "message",
                        "content": reply.content,
                        "session_id": session.id,
                        "message": reply.to_dict(),
                    }
                )

            task = loop.create_task(_turn())
            with active_lock:
                active_turns[session.id] = {
                    "loop": loop,
                    "task": task,
                    "started": _time.time(),
                }
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                session.finished = True
                storage.save_session(session)
                sink.put({"type": "aborted", "session_id": session.id})
            except Exception as exc:  # noqa: BLE001 - surfaced to the HTTP client
                session.finished = True
                storage.save_session(session)
                sink.put({"type": "error", "text": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surfaced to the HTTP client
            sink.put({"type": "error", "text": str(exc)})
        finally:
            if not session.finished:
                session.finished = True
                try:
                    storage.save_session(session)
                except Exception:  # noqa: BLE001 - best effort
                    pass
            with active_lock:
                active_turns.pop(session.id, None)
            sink.put({"type": "done", "session_id": session.id})
            sink.put(None)
            loop.close()

    MAX_REQUEST_BODY = 10 * 1024 * 1024
    HEARTBEAT_SECONDS = 15.0
    BODY_READ_TIMEOUT = 30.0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # quiet
            return

        def _host_is_loopback(self) -> bool:
            """DNS-rebinding guard: a loopback-bound server only answers loopback Host names."""
            host = (self.headers.get("Host") or "").strip().lower()
            if host.startswith("["):
                name = host[1:].split("]", 1)[0]
            elif host.count(":") == 1:
                name = host.rsplit(":", 1)[0]
            else:
                name = host
            return name in ("127.0.0.1", "localhost", "::1")

        def _origin_trusted(self) -> bool:
            """A browser Origin is trusted only when it is this server or listed in --cors.

            Requests without an Origin (CLI, native apps) are not cross-site browser requests.
            """
            origin = self.headers.get("Origin", "")
            if not origin:
                return True
            if cors_origins and origin in cors_origins:
                return True
            host = self.headers.get("Host", "")
            return bool(host) and origin in (f"http://{host}", f"https://{host}")

        def _authorized(self) -> bool:
            tokens = {token for token in (server_token, pair_token) if token}
            if not tokens:
                # No token configured: only same-origin loopback clients may talk to
                # the server. Any website could otherwise drive the agent (bash!)
                # with a cross-origin fetch to 127.0.0.1.
                return self._host_is_loopback() and self._origin_trusted()
            if self.path.split("?", 1)[0] in ("/health", "/openapi.json", "/v1/openapi.json"):
                return True
            authorization = self.headers.get("Authorization", "")
            if authorization.startswith("Bearer ") and _token_matches(authorization[7:], tokens):
                return True
            explicit = self.headers.get("X-RBit-Token", "") or self.headers.get("X-RBit-Pair", "")
            if explicit and _token_matches(explicit, tokens):
                return True
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            for key in ("pair", "token"):
                value = (query.get(key) or [""])[0]
                if value and _token_matches(value, tokens):
                    return True
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                if part.strip().startswith("rbit_pair="):
                    value = part.strip().split("=", 1)[1]
                    if _token_matches(value, tokens):
                        return True
            return False

        def _begin(self) -> bool:
            """Authorize, then normalise `self.path` (query string stripped) once."""
            if not self._authorized():
                self._deny()
                return False
            from urllib.parse import urlsplit

            self.raw_path = self.path
            self.path = urlsplit(self.path).path or "/"
            return True

        def _read_json(self) -> dict[str, Any] | None:
            """Read the request body as a JSON object; on failure send a JSON error and return None."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length not in (None, "") else 0
            except ValueError:
                self._send_json({"error": "invalid Content-Length"}, 400)
                return None
            if length < 0:
                self._send_json({"error": "invalid Content-Length"}, 400)
                return None
            if length > MAX_REQUEST_BODY:
                self.close_connection = True  # the oversized body is left unread
                self._send_json({"error": "request body too large", "limit": MAX_REQUEST_BODY}, 413)
                return None
            try:
                self.connection.settimeout(BODY_READ_TIMEOUT)
                data = self.rfile.read(length) if length else b""
                self.connection.settimeout(None)
            except (OSError, TimeoutError):
                self.close_connection = True
                self._send_json({"error": "timed out reading request body"}, 408)
                return None
            if not data.strip():
                return {}
            try:
                obj = _json.loads(data)
            except (ValueError, UnicodeDecodeError):
                self._send_json({"error": "invalid JSON body"}, 400)
                return None
            if not isinstance(obj, dict):
                self._send_json({"error": "JSON body must be an object"}, 400)
                return None
            return obj

        def _not_found(self) -> None:
            self._send_json({"error": "not found", "path": self.path}, 404)

        def _method_not_allowed(self) -> None:
            if not self._begin():
                return
            body = _json.dumps({"error": "method not allowed", "method": self.command}).encode("utf-8")
            self.send_response(405)
            self._cors()
            self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_PUT = _method_not_allowed  # noqa: N815
        do_PATCH = _method_not_allowed  # noqa: N815
        do_HEAD = _method_not_allowed  # noqa: N815

        def _deny(self) -> None:
            self.send_response(401)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = _json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _cors(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin and self._origin_trusted():
                allowed = origin
            elif server_token or pair_token:
                allowed = "*"  # bearer-token protected: a foreign page cannot supply it
            else:
                return  # token-less server: never grant a foreign website access
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS, DELETE")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-RBit-Token, X-RBit-Pair")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._begin():
                return
            if self.path == "/health":
                self._send_json(
                    {
                        "status": "ok",
                        "ok": True,
                        "app": "rbit",
                        "version": __version__,
                        "api": 2,
                        "workspace": str(workspace),
                        "name": workspace.name,
                    }
                )
            elif self.path in ("/openapi.json", "/v1/openapi.json"):
                body = _json.dumps(build_openapi_spec(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.split("?", 1)[0] == "/v1/status":
                with active_lock:
                    busy = sorted(active_turns.keys())
                self._send_json(
                    {
                        "ok": True,
                        "app": "rbit",
                        "version": __version__,
                        "api": 2,
                        "workspace": {
                            "path": str(workspace),
                            "name": workspace.name,
                        },
                        "model": model_id,
                        "effort": str(cfg.get("effort") or "auto"),
                        "efforts": ["auto", "low", "medium", "high"],
                        "agents": [
                            {"name": "build", "title": "Build", "description": "Пишет код и запускает инструменты"},
                            {"name": "plan", "title": "Plan", "description": "Только читает и планирует"},
                        ],
                        "approvals": os.environ.get("RBIT_APPROVALS") == "1",
                        "auth": bool(server_token or pair_token),
                        "busy": busy,
                    }
                )
            elif self.path.split("?", 1)[0] == "/v1/agents":
                self._send_json(
                    {
                        "agents": [
                            {
                                "name": "build",
                                "title": "Build",
                                "description": "Пишет код и запускает инструменты",
                            },
                            {
                                "name": "plan",
                                "title": "Plan",
                                "description": "Только читает и планирует",
                            },
                        ]
                    }
                )
            elif self.path.startswith("/v1/artifacts"):
                from urllib.parse import unquote

                artifact_dir = storage.state_dir / "artifacts"
                name = unquote(self.path.split("/v1/artifacts", 1)[1].strip("/"))
                if not name:
                    entries = []
                    if artifact_dir.is_dir():
                        for path in sorted(artifact_dir.iterdir()):
                            if path.is_file():
                                entries.append({"name": path.name, "size": path.stat().st_size})
                    self._send_json({"artifacts": entries})
                else:
                    safe = Path(name).name
                    path = artifact_dir / safe
                    if not path.is_file():
                        self._send_json({"error": "artifact not found"}, 404)
                    else:
                        self._send_json({"name": safe, "content": path.read_text(encoding="utf-8", errors="replace")})
            elif self.path == "/v1/schedules":
                from rbit.scheduler import Scheduler

                self._send_json({"schedules": Scheduler(workspace).load()})
            elif self.path.startswith("/v1/fs"):
                from urllib.parse import parse_qs, unquote, urlparse

                query = parse_qs(urlparse(self.raw_path).query)
                raw = (query.get("path") or ["."])[0]  # parse_qs already decoded it once
                try:
                    target = (workspace / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
                    rel_parts = target.relative_to(workspace.resolve()).parts
                except (ValueError, OSError):
                    self._send_json({"error": "path outside workspace"}, 403)
                else:
                    from rbit.tools import is_secret_file

                    if ".rbit" in rel_parts or ".git" in rel_parts or is_secret_file(target):
                        self._send_json({"error": "forbidden path"}, 403)
                        return
                    if target.is_dir():
                        entries = [
                            {"name": child.name, "type": "dir" if child.is_dir() else "file"}
                            for child in sorted(target.iterdir())[:500]
                        ]
                        self._send_json({"path": str(target.relative_to(workspace)), "entries": entries})
                    elif target.is_file():
                        self._send_json(
                            {
                                "path": str(target.relative_to(workspace)),
                                "content": target.read_text(encoding="utf-8", errors="replace")[:200_000],
                            }
                        )
                    else:
                        self._send_json({"error": "not found"}, 404)
            elif self.path == "/v1/usage":
                stored = storage.list_sessions(include_archived=True)
                self._send_json(
                    {
                        "sessions": len(stored),
                        "messages": sum(len(session.messages) for session in stored),
                        "tokens": sum(session.total_tokens for session in stored),
                        "tool_calls": sum(
                            len(message.tool_calls) for session in stored for message in session.messages
                        ),
                    }
                )
            elif self.path == "/v1/config":
                self._send_json(_mask_secret_values(cfg))
            elif self.path == "/v1/skills":
                from rbit.skills import load_skills

                self._send_json({"skills": load_skills(workspace)})
            elif self.path == "/v1/vcs/status":
                import subprocess as _sp

                proc = _sp.run(
                    ["git", "status", "--porcelain"], cwd=str(workspace), capture_output=True, text=True, check=False
                )
                self._send_json({"ok": proc.returncode == 0, "status": proc.stdout})
            elif self.path in ("/", "/index.html"):
                renderer = Path(__file__).resolve().parent.parent / "desktop" / "renderer" / "index.html"
                if renderer.exists():
                    body = renderer.read_bytes()
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    if pair_token:
                        self.send_header("Set-Cookie", f"rbit_pair={pair_token}; HttpOnly; SameSite=Lax; Path=/")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
            elif self.path in ("/app.js", "/styles.css"):
                fname = "app.js" if self.path.endswith("app.js") else "styles.css"
                fpath = Path(__file__).resolve().parent.parent / "desktop" / "renderer" / fname
                if fpath.exists():
                    body = fpath.read_bytes()
                    ctype = "application/javascript; charset=utf-8" if fname == "app.js" else "text/css; charset=utf-8"
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
            elif self.path == "/v1/models":
                provider_models = [ModelInfo.from_dict(item) for item in cfg.get("provider_models") or []]
                provider_models = [m for m in provider_models if m.id]
                # With a configured provider, the built-in registry ids are not
                # valid for it (they would fall back to the canned reply) — show
                # the provider's real catalog plus the built-in Smart Agent.
                pool = provider_models if (provider_models and str(cfg.get("api_key") or "").strip()) else list(AVAILABLE_MODELS)
                if provider_models and str(cfg.get("api_key") or "").strip():
                    smart = next((m for m in AVAILABLE_MODELS if m.id == "rbit/smart-local"), None)
                    pool = ([smart] if smart else []) + provider_models
                listed = [
                    {
                        "id": m.id,
                        "name": m.name,
                        "provider": m.provider,
                        "context_window": m.context_window,
                        "cost_per_1k": m.cost_per_1k,
                    }
                    for m in pool
                ]
                seen = {m.id for m in pool}
                if not (provider_models and str(cfg.get("api_key") or "").strip()):
                    for item in cfg.get("provider_models") or []:
                        m = ModelInfo.from_dict(item)
                        if m.id and m.id not in seen:
                            listed.append(
                                {
                                    "id": m.id,
                                    "name": m.name,
                                    "provider": m.provider,
                                    "context_window": m.context_window,
                                    "cost_per_1k": m.cost_per_1k,
                                }
                            )
                            seen.add(m.id)
                body = _json.dumps(
                    {
                        "models": listed,
                        "current": model_id,
                        "effort": str(cfg.get("effort") or "auto"),
                        "efforts": ["auto", "low", "medium", "high"],
                        "workspace": str(workspace),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.split("?", 1)[0] == "/v1/sessions":
                with session_lock:
                    stored = storage.list_sessions()
                self._send_json({"sessions": [_session_summary(s) for s in stored]})
            elif self.path.startswith("/v1/sessions/"):
                from urllib.parse import unquote, urlparse

                sid = unquote(urlparse(self.path).path.rsplit("/", 1)[-1])
                with session_lock:
                    sess = storage.find_session(sid) if sid else None
                if not sess:
                    self._send_json({"error": "session not found", "id": sid}, 404)
                    return
                self._send_json(_session_payload(sess))
            else:
                self._not_found()

        def do_POST(self) -> None:  # noqa: N802
            if not self._begin():
                return
            if self.path == "/v1/reload":
                nonlocal cfg, model_id, pair_token
                cfg = storage.load_config()
                model_id = str(cfg.get("model", "rbit/smart-local"))
                try:
                    if service_manager is not None:
                        pair_token = str(service_manager.get_setting("pair_token") or pair_token)
                except Exception:  # noqa: BLE001 - pairing is optional
                    pass
                body = _json.dumps({"ok": True, "model": model_id, "pairing": bool(pair_token)}).encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/v1/artifacts":
                req = self._read_json()
                if req is None:
                    return
                name = str(req.get("name") or "").strip()
                if not name:
                    self._send_json({"error": "name is required"}, 422)
                    return
                # \w keeps non-ASCII letters (so "отчёт" and "план" stay distinct); a name
                # with no usable characters falls back to a hash of the original.
                safe = re.sub(r"[^\w.-]+", "-", name).strip("-.")
                if not safe:
                    import hashlib

                    safe = "artifact-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
                if not Path(safe).suffix:
                    safe += ".md"
                directory = storage.state_dir / "artifacts"
                directory.mkdir(parents=True, exist_ok=True)
                if (directory / safe).exists() and not req.get("overwrite"):
                    self._send_json({"error": "artifact already exists", "name": safe, "hint": "pass overwrite: true"}, 409)
                    return
                (directory / safe).write_text(str(req.get("content") or ""), encoding="utf-8")
                self._send_json({"name": safe, "size": (directory / safe).stat().st_size}, 201)
                return
            if self.path == "/v1/schedules":
                req = self._read_json()
                if req is None:
                    return
                from rbit.scheduler import Scheduler

                try:
                    entry, error = Scheduler(workspace).add(
                        str(req.get("prompt") or ""),
                        str(req.get("schedule") or ""),
                        name=str(req.get("name") or ""),
                        model=str(req.get("model") or ""),
                        agent=str(req.get("agent") or "build"),
                    )
                except Exception as exc:  # noqa: BLE001 - always answer with JSON, never drop the connection
                    self._send_json({"error": f"cannot create schedule: {exc}"}, 422)
                    return
                if entry is None:
                    self._send_json({"error": error}, 422)
                else:
                    self._send_json({"schedule": entry}, 201)
                return
            if self.path == "/v1/sessions":
                req = self._read_json()
                if req is None:
                    return
                s = Session()
                s.title = (str(req.get("title") or "").strip()[:80] or "Новая сессия")
                s.archived = bool(req.get("archived"))
                with session_lock:
                    sessions[s.id] = s
                    storage.save_session(s)
                self._send_json(_session_payload(s), 201)
                return
            if self.path.startswith("/v1/sessions/") and self.path.endswith("/rename"):
                from urllib.parse import unquote

                sid = unquote(self.path[len("/v1/sessions/") : -len("/rename")])
                req = self._read_json()
                if req is None:
                    return
                title = str(req.get("title") or "").strip()[:80]
                if not title:
                    self._send_json({"error": "title is required"}, 422)
                    return
                with session_lock:
                    sess = storage.find_session(sid) if sid else None
                    if sess:
                        sess.title = title
                        storage.save_session(sess)
                        cached = sessions.get(sess.id)
                        if cached is not None:
                            cached.title = title
                if not sess:
                    self._send_json({"error": "session not found", "id": sid}, 404)
                    return
                self._send_json({"ok": True, "id": sess.id, "title": title})
                return
            if self.path.startswith("/v1/sessions/") and self.path.split("?", 1)[0].endswith("/abort"):
                from urllib.parse import unquote, urlparse

                sid = unquote(urlparse(self.path).path[len("/v1/sessions/") : -len("/abort")])
                with active_lock:
                    entry = active_turns.get(sid)
                aborted = False
                if entry is not None and entry.get("loop") is not None and entry.get("task") is not None:
                    try:
                        entry["loop"].call_soon_threadsafe(entry["task"].cancel)
                        aborted = True
                    except RuntimeError:
                        aborted = False
                self._send_json({"ok": True, "aborted": aborted, "id": sid})
                return
            if self.path == "/v1/approvals":
                req = self._read_json()
                if req is None:
                    return
                aid = str(req.get("id") or "")
                entry = approvals.pop(aid, None) if aid else None
                approved = bool(req.get("approved"))
                if entry and not entry["fut"].done():
                    if approved and req.get("always") and entry.get("executor") is not None:
                        # "Don't ask again": allow the command's 2-word prefix for this turn.
                        try:
                            prefix = entry["executor"]._bash_prefix(str(entry.get("input") or ""))
                            if prefix and prefix not in entry["executor"].allowed_bash_prefixes:
                                entry["executor"].allowed_bash_prefixes.append(prefix)
                        except Exception:
                            pass
                    entry["loop"].call_soon_threadsafe(_settle_future, entry["fut"], approved)
                body = _json.dumps({"ok": True, "id": aid, "approved": approved}).encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path != "/v1/turns":
                self._not_found()
                return
            req = self._read_json()
            if req is None:
                return
            prompt_text = str(req.get("prompt") or "").strip()
            if not prompt_text:
                self._send_json({"error": "prompt is required"}, 400)
                return
            raw_sid = req.get("session_id")
            sid = "" if raw_sid is None else str(raw_sid).strip()
            if raw_sid is not None and not sid:
                self._send_json({"error": "session not found", "id": ""}, 404)
                return
            with session_lock:
                if sid:
                    session = sessions.get(sid) or storage.find_session(sid)
                    if session is None:
                        self._send_json({"error": "session not found", "id": sid}, 404)
                        return
                else:
                    session = Session()
                sessions[session.id] = session
            # Check-and-register atomically: two concurrent POSTs for one session
            # must not both start a turn (the loser gets 409).
            with active_lock:
                busy = session.id in active_turns
                if not busy:
                    active_turns[session.id] = {"loop": None, "task": None, "started": _time.time()}
            if busy:
                self._send_json(
                    {
                        "error": "session is busy",
                        "session_id": session.id,
                        "hint": "abort the current turn first",
                    },
                    409,
                )
                return
            requested_title = str(req.get("title") or "").strip()
            if requested_title and not session.messages:
                session.title = requested_title[:80]
            model_req = str(req.get("model") or "").strip()
            agent_req = str(req.get("agent") or req.get("mode") or "").strip()
            effort_req = str(req.get("effort") or "").strip().lower()

            events: _queue.Queue = _queue.Queue()
            worker = _threading.Thread(
                target=run_turn_events,
                args=(prompt_text, session, events, model_req, agent_req, effort_req),
                daemon=True,
                name=f"rbit-turn-{session.id}",
            )
            try:
                worker.start()
            except Exception:
                with active_lock:
                    active_turns.pop(session.id, None)
                raise
            disconnected = False
            try:
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, OSError):
                disconnected = True
            while True:
                try:
                    item = events.get(timeout=HEARTBEAT_SECONDS)
                except _queue.Empty:
                    item = {"type": "ping"}  # keepalive: also detects a vanished client
                if item is None:
                    break
                if disconnected:
                    continue
                try:
                    self.wfile.write((_json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    disconnected = True
                    if cfg.get("cancel_on_disconnect"):
                        with active_lock:
                            running = active_turns.get(session.id)
                        if running and running.get("loop") is not None and running.get("task") is not None:
                            try:
                                running["loop"].call_soon_threadsafe(running["task"].cancel)
                            except RuntimeError:
                                pass

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._begin():
                return
            if self.path.startswith("/v1/schedules/"):
                from rbit.scheduler import Scheduler

                from urllib.parse import unquote

                target = unquote(self.path.rsplit("/", 1)[-1])
                ok = bool(target) and Scheduler(workspace).remove(target)
                self._send_json({"deleted": ok, "id": target}, 200 if ok else 404)
                return
            if self.path.startswith("/v1/artifacts/"):
                from urllib.parse import unquote

                name = Path(unquote(self.path.rsplit("/", 1)[-1])).name
                path = storage.state_dir / "artifacts" / name
                ok = path.is_file()
                if ok:
                    path.unlink()
                self._send_json({"deleted": ok, "name": name}, 200 if ok else 404)
                return
            if not self.path.startswith("/v1/sessions/"):
                self._not_found()
                return
            from urllib.parse import unquote

            sid = unquote(self.path[len("/v1/sessions/") :].strip("/"))
            with session_lock:
                found = storage.find_session(sid) if sid else None
                ok = bool(found) and storage.delete_session(found.id)
                if found is not None:
                    sessions.pop(found.id, None)  # drop it from the in-memory cache too
            body = _json.dumps({"deleted": bool(ok), "id": found.id if found else sid}).encode("utf-8")
            self.send_response(200 if ok else 404)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = ThreadingHTTPServer((hostname, args.port), Handler)
    except OSError as exc:
        Console(stderr=True).print(
            f"[red]Cannot listen on {markup_escape(hostname)}:{args.port}:[/] {markup_escape(str(exc))}. "
            "The port may already be in use; pick another with --port."
        )
        return 1
    args.port = server.server_address[1]  # resolves --port 0
    if open_browser:
        import webbrowser
        import threading as _threading2

        _threading2.Timer(1.2, lambda: webbrowser.open(f"http://{hostname}:{args.port}/")).start()
    console = Console()
    if mobile:
        from urllib.parse import quote

        show_host = hostname not in ("0.0.0.0", "::", "")
        if show_host:
            base = f"http://{hostname}:{args.port}"
            app_link = (
                "rbit://connect"
                f"?url={quote(base, safe='')}"
                f"&token={quote(server_token or '', safe='')}"
            )
            links = [(base, app_link)]
        else:
            links = _mobile_links(args.port, server_token or "")
        console.print()
        console.print("[bold #fab283]rbit mobile[/]  [dim]сервер для iPhone[/]")
        console.print(f"[#808080]Проект:[/] {markup_escape(str(workspace))}")
        console.print(f"[#808080]Адрес:[/]  http://{hostname}:{args.port}")
        console.print(f"[#808080]Токен:[/]  {server_token or '(без токена)'}")
        if links:
            console.print()
            console.print("[#808080]Ссылки для iPhone:[/]")
            for url, app_link in links:
                console.print(f"  [#5c9cf5]{url}[/]", soft_wrap=True)
                console.print(f"  [dim]{app_link}[/]", soft_wrap=True)
            console.print()
            console.print("[#808080]В приложении: Настройки → Добавить ПК → вставьте rbit:// ссылку.[/]")
            console.print("[#808080]Или введите адрес и токен вручную. Телефон и ПК — в одной Wi-Fi сети.[/]")
            try:
                import qrcode

                qr = qrcode.QRCode(border=1)
                qr.add_data(links[0][1])
                qr.make(fit=True)
                console.print("[#808080]QR для сканирования в приложении:[/]")
                qr.print_ascii(invert=True)
            except Exception:  # noqa: BLE001 - QR is optional
                console.print('[dim]QR-код: pip install "rbit[mobile]"[/]')
        else:
            console.print("[#e06c75]Не найдены LAN-адреса.[/] Укажите --hostname с IP этого ПК.")
        console.print("[#808080]Остановить: Ctrl+C[/]")
        console.print()
    else:
        console.print(
            f"rbit serve on http://{hostname}:{args.port}  "
            "(POST /v1/turns, GET /health, GET /v1/models, GET /v1/sessions)"
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _scan_tree(workspace: Path, snapshot: dict[str, float], report: bool = True) -> list[str]:
    """Update `snapshot` with file mtimes and return new/modified files.

    Walks the tree pruning heavy directories (never descends into .git/node_modules).
    """
    skip = {".rbit", ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
    changed: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            full = Path(root) / name
            try:
                mtime = full.stat().st_mtime
            except OSError:
                continue
            rel = str(full.relative_to(workspace))
            if report and snapshot.get(rel) != mtime:
                changed.append(rel)
            snapshot[rel] = mtime
    return changed


def _run_watch(workspace: Path, run_prompt: str | None, interval: float) -> int:
    """Polls workspace mtimes; prints changes, optionally triggering an agent turn."""
    import asyncio

    snapshot: dict[str, float] = {}
    console = Console()
    console.print(f"[bold #7fd88f]Watching[/] {markup_escape(str(workspace))} (Ctrl+C to stop)")
    storage = StorageManager(workspace)
    cfg = storage.load_config()
    engine_holder: dict[str, object] = {}

    async def react(changed: list[str]) -> None:
        from rbit.agent import AgentEngine
        from rbit.models import ChatMessage, Session, short_id
        from rbit.tools import ToolExecutor

        session = Session(title=f"watch {changed[0]}")
        executor = ToolExecutor(workspace=workspace, session=session, storage=storage, get_config=lambda: cfg)
        engine = AgentEngine(workspace=workspace, executor=executor, storage=storage, get_config=lambda: cfg)
        # Tell the agent WHAT changed: with a fixed prompt alone it has to rediscover the change.
        shown = [name.replace("\\", "/") for name in changed[:20]]
        listing = "\n".join(f"- {name}" for name in shown) + ("\n- ..." if len(changed) > len(shown) else "")
        user = ChatMessage(id=short_id(), role="user", content=f"{run_prompt}\n\nChanged files:\n{listing}")
        session.messages.append(user)

        async def _noop(_rec) -> None:
            return None

        async def _noop_s(_s: str) -> None:
            return None

        try:
            reply = await engine.run_turn(
                session=session, user_msg=user, agent_mode="build",
                model_id=str(cfg.get("model", "rbit/smart-local")),
                on_tool_start=_noop, on_tool_end=_noop, on_status=_noop_s,
            )
        except BaseException:
            storage.save_session(session)  # keep what was asked even if the turn failed
            raise
        # run_turn returns the reply without appending it: persist it, or the run leaves no trace.
        session.messages.append(reply)
        storage.save_session(session)
        console.print(f"[bold #7fd88f]rbit[/] {markup_escape(reply.content[:600])}")

    def scan(report: bool = True) -> list[str]:
        return _scan_tree(workspace, snapshot, report)

    scan(report=False)  # baseline
    try:
        while True:
            time.sleep(interval)
            changed = scan()
            if changed:
                time.sleep(min(max(interval, 0.2), 1.0))  # debounce: let a burst of saves settle
                changed = sorted(set(changed) | set(scan()))
                console.print("[bold #fab283]changed:[/]", markup_escape(", ".join(changed[:8])))
                if run_prompt:
                    try:
                        asyncio.run(react(changed))
                    except Exception as exc:  # noqa: BLE001 - keep watching after a failed turn
                        console.print(f"[bold #e06c75]watch turn failed:[/] {markup_escape(str(exc))}")
                    scan(report=False)  # ignore edits made by the agent itself
    except KeyboardInterrupt:
        console.print("\nStopped.")
    return 0


def _run_voice(seconds: float, model_size: str) -> int:
    """Records microphone audio and transcribes it (optional dependencies)."""
    console = Console()
    missing = []
    try:
        import sounddevice  # noqa: F401
    except ImportError:
        missing.append("sounddevice")
    try:
        import whisper  # noqa: F401
    except ImportError:
        missing.append("openai-whisper")
    if missing:
        console.print("[bold #e06c75]Voice input needs optional packages:[/] pip install " + " ".join(missing))
        console.print("Then run `rbit voice` again.")
        return 1
    import numpy as np

    import sounddevice as sd
    import whisper

    console.print(f"Recording {seconds:.0f}s — speak now…")
    sr = 16000
    audio = sd.rec(int(seconds * sr), samplerate=sr, channels=1, dtype="float32")
    sd.wait()
    console.print("Transcribing…")
    result = whisper.load_model(model_size).transcribe(np.squeeze(audio))
    text = str(result.get("text") or "").strip()
    console.print(f"[bold #7fd88f]Prompt:[/] {markup_escape(text)}")
    try:
        import pyperclip

        pyperclip.copy(text)
        console.print("(copied to clipboard — paste into rbit)")
    except Exception:
        pass
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
