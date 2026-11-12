"""Agent tools for rbit: read, write, edit, multi_edit, glob, grep, bash, todowrite, webfetch + Checkpoints."""

from __future__ import annotations

import ast
import asyncio
import difflib
import html
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

import httpx

from .models import CheckpointMeta, FileChangeStat, FileSnapshot, Session, TodoItem, short_id
from .storage import StorageManager

IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".opencode-clone",
    ".rbit",
    "dist",
    "build",
}

READONLY_BASH_PREFIXES = (
    "dir",
    "ls",
    "pwd",
    "echo",
    "cat",
    "type",
    "git status",
    "git diff",
    "git log",
    "git branch",
    "python --version",
    "node -v",
    "npm -v",
    "Get-ChildItem",
    "Get-Location",
    "Test-Path",
    "pytest",
)

READONLY_TOOLS: frozenset[str] = frozenset({"read", "glob", "grep", "webfetch", "diagnostics", "lint"})

VIRTUAL_ATTACHMENTS: frozenset[str] = frozenset({"diff", "git", "staged", "tree"})

MAX_CHECKPOINT_FILE_BYTES = 256_000

SAFE_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".schema", ".dist")
SECRET_EXACT_NAMES = frozenset(
    {
        ".env",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "credentials.json",
        "service-account.json",
        ".npmrc",
        ".pypirc",
        ".netrc",
    }
)
SECRET_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"})

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
        "-----BEGIN PRIVATE KEY-----[REDACTED]-----END PRIVATE KEY-----",
    ),
    (
        re.compile(r"\bsk-(?:or-v1-|ant-api\d+-|ant-|proj-|live_|test_|live-|test-)?[A-Za-z0-9_\-]{16,}\b"),
        "sk-[REDACTED]",
    ),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "gh_[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "github_pat_[REDACTED]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{16,}\b"), "xox-[REDACTED]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), "hf_[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{32,}\b"), "AIza[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{20,}", re.IGNORECASE), r"\1[REDACTED]"),
    (
        re.compile(
            r"(\b(?:[A-Z0-9_]*_)?(?:API_KEY|SECRET_KEY|ACCESS_TOKEN|AUTH_TOKEN|PRIVATE_KEY|CLIENT_SECRET|PASSWORD)\s*=\s*['\"]?)"
            r"[A-Za-z0-9_\-./+=]{12,}"
            r"(['\"]?)"
        ),
        r"\1[REDACTED]\2",
    ),
)


def is_secret_file(path: Path) -> bool:
    """Returns True if `path` looks like a secret/credential file that should not be read by default."""
    name = path.name.lower()
    if name.endswith(SAFE_TEMPLATE_SUFFIXES):
        return False
    if name in SECRET_EXACT_NAMES or name.startswith(".env."):
        return True
    if path.suffix.lower() in SECRET_EXTENSIONS:
        return True
    if name == "config.json" and path.parent.name.lower() == ".rbit":
        return True
    return False


def redact_secrets(text: str) -> str:
    """Redacts API keys, OAuth tokens, PEM private keys, and env secrets from text."""
    if not text:
        return text
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


@dataclass
class ToolResult:
    ok: bool
    summary: str
    output: str
    diff: str = ""
    duration_ms: int = 0


PermissionCallback = Callable[[str, str], Awaitable[bool]]


class ToolExecutor:
    def __init__(
        self,
        workspace: Path,
        session: Session,
        storage: StorageManager | None = None,
        permission_cb: PermissionCallback | None = None,
        on_todos_updated: Callable[[list[TodoItem]], None] | None = None,
        on_files_updated: Callable[[], None] | None = None,
        get_config: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.session = session
        self.storage = storage or StorageManager(self.workspace)
        self.permission_cb = permission_cb
        self.on_todos_updated = on_todos_updated
        self.on_files_updated = on_files_updated
        self.get_config = get_config
        self.undo_stack: list[FileSnapshot] = []
        self.redo_stack: list[FileSnapshot] = []
        self.always_allow_bash: bool = False
        self.allow_secret_reads: bool = False
        self._auto_checkpoint_done: bool = False

    def begin_turn(self) -> None:
        """Resets per-turn state so the next mutation gets a fresh auto-checkpoint."""
        self._auto_checkpoint_done = False

    def _maybe_auto_checkpoint(self) -> None:
        """Snapshots the workspace once per turn, right before the first file mutation."""
        if self._auto_checkpoint_done:
            return
        self._auto_checkpoint_done = True
        try:
            if self.list_workspace_files(limit=1):
                self.create_checkpoint("auto: before first edit of turn")
        except Exception:
            pass

    def _secret_reads_allowed(self) -> bool:
        if self.allow_secret_reads:
            return True
        if self.get_config is not None:
            return bool(self.get_config().get("allow_secret_reads", False))
        return False

    def resolve_path(self, raw_path: str) -> Path:
        p = Path(raw_path)
        if not p.is_absolute():
            p = (self.workspace / p).resolve()
        return p

    def rel_display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    def list_workspace_files(self, limit: int = 250) -> list[str]:
        results: list[str] = []
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for fname in sorted(files):
                full = Path(root) / fname
                results.append(self.rel_display(full))
                if len(results) >= limit:
                    return results
        return results

    def _compute_diff(self, old: str, new: str, filename: str) -> tuple[str, int, int]:
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
                lineterm="",
            )
        )
        added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
        return "\n".join(ln.rstrip("\r\n") for ln in diff_lines), added, removed

    def _record_file_change(self, rel_path: str, added: int, removed: int) -> None:
        stat = self.session.modified_files.get(rel_path)
        if stat is None:
            stat = FileChangeStat(path=rel_path, added=added, removed=removed)
            self.session.modified_files[rel_path] = stat
        else:
            stat.added = int(stat.added) + added
            stat.removed = int(stat.removed) + removed
        if self.on_files_updated:
            self.on_files_updated()

    @staticmethod
    def diagnose_content(rel_path: str, content: str) -> list[str]:
        """Fast in-process syntax check for Python, JSON, TOML, and XML/SVG files."""
        ext = Path(rel_path).suffix.lower()
        issues: list[str] = []

        if ext in (".py", ".pyi", ".pyw"):
            try:
                ast.parse(content, filename=rel_path)
            except SyntaxError as err:
                line = err.lineno or 1
                col = err.offset or 1
                snippet = f" -> `{err.text.strip()}`" if err.text and err.text.strip() else ""
                issues.append(f"{rel_path}:{line}:{col}: {type(err).__name__}: {err.msg}{snippet}")
        elif ext == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as err:
                issues.append(f"{rel_path}:{err.lineno}:{err.colno}: JSONDecodeError: {err.msg}")
        elif ext == ".toml" and tomllib is not None:
            try:
                tomllib.loads(content)
            except Exception as err:
                issues.append(f"{rel_path}: TOMLDecodeError: {err}")
        elif ext in (".xml", ".svg", ".xhtml"):
            try:
                ET.fromstring(content)
            except ET.ParseError as err:
                line, col = getattr(err, "position", (1, 1))
                issues.append(f"{rel_path}:{line}:{col}: XMLParseError: {err}")

        return issues

    def _attach_diagnostics(
        self,
        summary: str,
        output: str,
        rel_path: str,
        content: str,
    ) -> tuple[str, str]:
        issues = self.diagnose_content(rel_path, content)
        if not issues:
            return summary, output
        noun = "syntax issue" if len(issues) == 1 else "syntax issues"
        diag_block = "\n".join(f"- {item}" for item in issues)
        return (
            f"{summary} · {len(issues)} {noun}",
            f"{output}\n\nDiagnostics (syntax errors detected — fix immediately):\n{diag_block}",
        )

    # --- Undo / Redo ---
    def undo_last(self) -> str:
        if not self.undo_stack:
            return "No file changes to undo."
        snap = self.undo_stack.pop()
        target = Path(snap.path)
        if snap.existed_before:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(snap.previous_content, encoding="utf-8")
        elif target.exists():
            target.unlink()
        self.redo_stack.append(snap)
        rel = self.rel_display(target)
        if self.on_files_updated:
            self.on_files_updated()
        return f"Reverted `{rel}` ({snap.step_label})"

    def redo_last(self) -> str:
        if not self.redo_stack:
            return "No undone changes to redo."
        snap = self.redo_stack.pop()
        target = Path(snap.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(snap.new_content, encoding="utf-8")
        self.undo_stack.append(snap)
        rel = self.rel_display(target)
        if self.on_files_updated:
            self.on_files_updated()
        return f"Restored `{rel}` ({snap.step_label})"

    # --- Named Workspace Checkpoints (No Git required) ---
    def create_checkpoint(self, label: str = "") -> CheckpointMeta:
        files_map: dict[str, str] = {}
        for rel in self.list_workspace_files(limit=400):
            full = self.workspace / rel
            if not self._secret_reads_allowed() and is_secret_file(full):
                continue
            try:
                if full.stat().st_size <= MAX_CHECKPOINT_FILE_BYTES:
                    files_map[rel] = full.read_text(encoding="utf-8")
            except Exception:
                continue
        return self.storage.save_checkpoint(label or "Manual checkpoint", files_map)

    def restore_checkpoint(self, cid: str) -> str:
        loaded = self.storage.load_checkpoint_files(cid)
        if not loaded:
            return f"Checkpoint `{cid}` not found."
        meta, saved_files = loaded
        current_files = set(self.list_workspace_files(limit=400))

        # Restore saved text files
        restored = 0
        for rel, content in saved_files.items():
            full = self.workspace / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
            restored += 1

        # Remove newly created text files that didn't exist at checkpoint time
        removed = 0
        for rel in current_files - set(saved_files.keys()):
            full = self.workspace / rel
            if not self._secret_reads_allowed() and is_secret_file(full):
                continue
            try:
                if full.exists() and full.stat().st_size <= MAX_CHECKPOINT_FILE_BYTES:
                    full.unlink()
                    removed += 1
            except Exception:
                continue

        return f"Rewound workspace to `{meta.label}` ({restored} restored, {removed} removed)."

    # --- Direct Bang Shell Execution (!command) ---
    async def execute_bang_shell(self, command: str) -> ToolResult:
        t0 = time.perf_counter()
        res = await self._run_subprocess(command)
        res.output = redact_secrets(res.output)
        res.duration_ms = int((time.perf_counter() - t0) * 1000)
        return res

    # --- Main Tool Dispatcher ---
    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        agent_mode: str = "build",
    ) -> ToolResult:
        t0 = time.perf_counter()
        if agent_mode == "plan" and name in ("write", "edit", "multi_edit"):
            return ToolResult(
                ok=False,
                summary=f"{name} blocked in Plan mode",
                output="Plan mode is read-only. Press Tab to switch to Build mode to modify files.",
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )

        try:
            if name in ("write", "edit", "multi_edit"):
                self._maybe_auto_checkpoint()

            if name == "read":
                res = await asyncio.to_thread(self._tool_read, args)
            elif name == "write":
                res = self._tool_write(args)
            elif name == "edit":
                res = self._tool_edit(args)
            elif name == "multi_edit":
                res = self._tool_multi_edit(args)
            elif name == "glob":
                res = await asyncio.to_thread(self._tool_glob, args)
            elif name == "grep":
                res = await asyncio.to_thread(self._tool_grep, args)
            elif name == "diagnostics":
                res = await asyncio.to_thread(self._tool_diagnostics, args)
            elif name == "lint":
                res = await self._tool_lint(args)
            elif name == "todowrite":
                res = self._tool_todowrite(args)
            elif name == "webfetch":
                res = await self._tool_webfetch(args)
            elif name == "bash":
                res = await self._tool_bash(args, agent_mode=agent_mode)
            else:
                res = ToolResult(False, f"Unknown tool: {name}", f"Unknown tool: {name}")
        except Exception as exc:
            res = ToolResult(False, f"Error in {name}", f"{type(exc).__name__}: {exc}")

        res.output = redact_secrets(res.output)
        if res.diff:
            res.diff = redact_secrets(res.diff)
        res.duration_ms = int((time.perf_counter() - t0) * 1000)
        return res

    def _tool_read(self, args: dict[str, Any]) -> ToolResult:
        raw = args.get("filePath") or args.get("path") or "."
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, min(500, int(args.get("limit", 200))))
        target = self.resolve_path(raw)
        rel = self.rel_display(target)

        if not target.exists():
            return ToolResult(False, f"{rel} (not found)", f"File not found: {target}")

        if target.is_file() and not self._secret_reads_allowed() and is_secret_file(target):
            return ToolResult(
                False,
                f"{rel} (blocked: secret file)",
                f"Access blocked by Secret Guard: refusing to read sensitive file `{rel}`. "
                "Set `allow_secret_reads: true` in `.rbit/config.json` to override.",
            )

        if target.is_dir():
            entries = []
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if child.name in IGNORED_DIRS:
                    continue
                suffix = "/" if child.is_dir() else ""
                entries.append(f"{child.name}{suffix}")
            out = "\n".join(entries[:limit]) or "(empty directory)"
            return ToolResult(True, f"{len(entries)} entries", out)

        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        sliced = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{idx}: {line}" for idx, line in enumerate(sliced, start=offset))
        return ToolResult(True, f"{len(sliced)}/{len(lines)} lines", numbered or "(empty file)")

    def _tool_write(self, args: dict[str, Any]) -> ToolResult:
        raw = args.get("filePath") or args.get("path")
        content = str(args.get("content", ""))
        if not raw:
            return ToolResult(False, "missing filePath", "filePath is required.")

        target = self.resolve_path(raw)
        rel = self.rel_display(target)
        existed = target.exists()
        old_content = target.read_text(encoding="utf-8", errors="replace") if existed else ""

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        self.undo_stack.append(
            FileSnapshot(
                step_label=f"write {rel}",
                path=str(target),
                existed_before=existed,
                previous_content=old_content,
                new_content=content,
            )
        )
        self.redo_stack.clear()

        diff, added, removed = self._compute_diff(old_content, content, rel)
        self._record_file_change(rel, added, removed)
        summary, output = self._attach_diagnostics(
            f"(+{added} -{removed})",
            f"Wrote {rel} (+{added} -{removed} lines)",
            rel,
            content,
        )
        return ToolResult(True, summary, output, diff=diff)

    def _tool_edit(self, args: dict[str, Any]) -> ToolResult:
        raw = args.get("filePath") or args.get("path")
        old_str = str(args.get("oldString", ""))
        new_str = str(args.get("newString", ""))
        replace_all = bool(args.get("replaceAll", False))

        if not raw:
            return ToolResult(False, "missing filePath", "filePath is required.")
        target = self.resolve_path(raw)
        rel = self.rel_display(target)
        if not target.exists():
            return ToolResult(False, f"{rel} not found", f"File does not exist: {rel}")

        old_content = target.read_text(encoding="utf-8", errors="replace")
        count = old_content.count(old_str)
        if count == 0:
            return ToolResult(False, "oldString not found", "oldString not found in file content.")
        if count > 1 and not replace_all:
            return ToolResult(
                False,
                f"{count} ambiguous matches",
                f"Found {count} matches for oldString; add context or set replaceAll=true.",
            )

        new_content = (
            old_content.replace(old_str, new_str)
            if replace_all
            else old_content.replace(old_str, new_str, 1)
        )
        target.write_text(new_content, encoding="utf-8")

        self.undo_stack.append(
            FileSnapshot(
                step_label=f"edit {rel}",
                path=str(target),
                existed_before=True,
                previous_content=old_content,
                new_content=new_content,
            )
        )
        self.redo_stack.clear()

        diff, added, removed = self._compute_diff(old_content, new_content, rel)
        self._record_file_change(rel, added, removed)
        summary, output = self._attach_diagnostics(
            f"(+{added} -{removed})",
            f"Edited {rel} (+{added} -{removed} lines)",
            rel,
            new_content,
        )
        return ToolResult(True, summary, output, diff=diff)

    def _tool_multi_edit(self, args: dict[str, Any]) -> ToolResult:
        raw = args.get("filePath") or args.get("path")
        edits = args.get("edits") or []
        if not raw or not isinstance(edits, list) or not edits:
            return ToolResult(False, "invalid multi_edit args", "filePath and non-empty edits list required.")
        target = self.resolve_path(raw)
        rel = self.rel_display(target)
        if not target.exists():
            return ToolResult(False, f"{rel} not found", f"File does not exist: {rel}")

        original = target.read_text(encoding="utf-8", errors="replace")
        updated = original
        for idx, ed in enumerate(edits, start=1):
            old_s = str(ed.get("oldString", ""))
            new_s = str(ed.get("newString", ""))
            rep_all = bool(ed.get("replaceAll", False))
            if old_s not in updated:
                return ToolResult(False, f"edit #{idx} failed", f"oldString in edit #{idx} not found.")
            updated = updated.replace(old_s, new_s) if rep_all else updated.replace(old_s, new_s, 1)

        target.write_text(updated, encoding="utf-8")
        self.undo_stack.append(
            FileSnapshot(
                step_label=f"multi_edit {rel}",
                path=str(target),
                existed_before=True,
                previous_content=original,
                new_content=updated,
            )
        )
        self.redo_stack.clear()

        diff, added, removed = self._compute_diff(original, updated, rel)
        self._record_file_change(rel, added, removed)
        summary, output = self._attach_diagnostics(
            f"{len(edits)} edits (+{added} -{removed})",
            f"Applied {len(edits)} edits to {rel} (+{added} -{removed})",
            rel,
            updated,
        )
        return ToolResult(True, summary, output, diff=diff)

    def _tool_glob(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args.get("pattern", "*"))
        matches: list[str] = []
        for p in self.workspace.rglob(pattern if "**" not in pattern else pattern.replace("**/", "")):
            if any(part in IGNORED_DIRS for part in p.parts):
                continue
            if p.is_file():
                matches.append(self.rel_display(p))
            if len(matches) >= 100:
                break
        matches.sort()
        out = "\n".join(matches) if matches else "No files matched."
        return ToolResult(True, f"{len(matches)} matches", out)

    def _tool_grep(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args.get("pattern", ""))
        include = str(args.get("include", "*"))
        if not pattern:
            return ToolResult(False, "empty pattern", "Regex pattern is required.")
        regex = re.compile(pattern, re.IGNORECASE)
        hits: list[str] = []
        for rel in self.list_workspace_files(limit=300):
            full = self.workspace / rel
            if not self._secret_reads_allowed() and is_secret_file(full):
                continue
            if include != "*" and not full.match(include):
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{idx}: {line.strip()[:140]}")
                    if len(hits) >= 80:
                        break
            if len(hits) >= 80:
                break
        out = "\n".join(hits) if hits else "No matches found."
        return ToolResult(True, f"{len(hits)} matches", out)

    async def _tool_lint(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("filePath") or args.get("path") or ".").strip()
        target = self.resolve_path(raw)
        rel = self.rel_display(target)
        if not target.exists():
            return ToolResult(False, f"{rel} not found", f"Path not found: {rel}")

        ruff = shutil.which("ruff")
        if ruff:
            try:
                proc = await asyncio.create_subprocess_exec(
                    ruff,
                    "check",
                    "--output-format",
                    "concise",
                    str(target),
                    cwd=str(self.workspace),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=60.0)
            except Exception as exc:
                return ToolResult(False, f"ruff failed: {rel}", f"ruff execution error: {exc}")
            text = ("\n".join(
                part for part in (
                    out_b.decode("utf-8", errors="replace").strip(),
                    err_b.decode("utf-8", errors="replace").strip(),
                ) if part
            )).strip()
            ok = proc.returncode == 0
            count = 0 if ok else len([ln for ln in text.splitlines() if ln.strip()])
            return ToolResult(
                ok,
                "0 issues" if ok else f"{count} issues",
                (text or "No lint issues.")[:6000],
            )

        diag = self._tool_diagnostics(args)
        note = "ruff/pyflakes not installed — ran built-in syntax diagnostics only (pip install ruff)."
        return ToolResult(diag.ok, f"fallback: {diag.summary}", f"{diag.output}\n\n{note}")

    def _tool_diagnostics(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("filePath") or args.get("path") or ".").strip()
        target = self.resolve_path(raw)
        if not target.exists():
            rel = self.rel_display(target)
            return ToolResult(False, f"{rel} not found", f"Path not found: {rel}")

        targets: list[Path]
        if target.is_file():
            targets = [target]
        else:
            targets = [self.workspace / rel for rel in self.list_workspace_files(limit=300)]

        all_issues: list[str] = []
        checked = 0
        for fpath in targets:
            rel = self.rel_display(fpath)
            if fpath.suffix.lower() not in (".py", ".pyi", ".pyw", ".json", ".toml", ".xml", ".svg", ".xhtml"):
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            checked += 1
            all_issues.extend(self.diagnose_content(rel, content))

        if not all_issues:
            return ToolResult(True, f"0 issues ({checked} checked)", f"No syntax issues in {checked} file(s).")
        return ToolResult(
            True,
            f"{len(all_issues)} issues ({checked} checked)",
            "\n".join(f"- {item}" for item in all_issues),
        )

    def _tool_todowrite(self, args: dict[str, Any]) -> ToolResult:
        raw_todos = args.get("todos", [])
        items: list[TodoItem] = []
        for raw in raw_todos:
            if isinstance(raw, dict):
                items.append(
                    TodoItem(
                        id=raw.get("id") or short_id(),
                        content=str(raw.get("content", "")),
                        status=str(raw.get("status", "pending")),
                        priority=str(raw.get("priority", "medium")),
                    )
                )
        self.session.todos = items
        if self.on_todos_updated:
            self.on_todos_updated(items)

        completed = sum(1 for t in items if t.status == "completed")
        lines = []
        for t in items:
            icon = {
                "completed": "[✓]",
                "in_progress": "[•]",
                "cancelled": "[x]",
            }.get(t.status, "[ ]")
            lines.append(f"{icon} {t.content} ({t.priority})")
        return ToolResult(
            True,
            f"{completed}/{len(items)} done",
            "\n".join(lines) if lines else "Todo list cleared.",
        )

    async def _tool_webfetch(self, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(False, "invalid url", "URL must start with http:// or https://")
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "rbit-cli/1.2"})
        raw = resp.text
        # Strip scripts/styles and HTML tags cleanly
        cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = html.unescape(re.sub(r"\s+", " ", cleaned)).strip()
        return ToolResult(
            resp.status_code < 400,
            f"HTTP {resp.status_code} ({len(cleaned)} chars)",
            cleaned[:6000] or "(empty response)",
        )

    async def _tool_bash(self, args: dict[str, Any], agent_mode: str = "build") -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(False, "empty command", "command is required.")

        is_readonly = any(command.lower().startswith(pref.lower()) for pref in READONLY_BASH_PREFIXES)
        if agent_mode == "plan" and not is_readonly:
            return ToolResult(
                False,
                f"blocked in Plan mode: {command[:36]}",
                "Only read-only commands are allowed in Plan mode.",
            )

        if not is_readonly and not self.always_allow_bash and self.permission_cb is not None:
            allowed = await self.permission_cb("bash", command)
            if not allowed:
                return ToolResult(
                    False,
                    f"rejected: {command[:36]}",
                    "Command execution rejected by user.",
                )

        return await self._run_subprocess(command)

    async def _run_subprocess(self, command: str) -> ToolResult:
        if os.name == "nt":
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            proc.kill()
            return ToolResult(False, f"timeout: {command[:36]}", "Command timed out after 30s.")

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        combined = "\n".join(part for part in (stdout, stderr) if part) or "(no output)"
        ok = proc.returncode == 0
        return ToolResult(
            ok,
            f"exit {proc.returncode}",
            combined[:4000],
        )


OPENAI_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file with line numbers or list directory contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Create or overwrite a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["filePath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace exact string in an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "oldString": {"type": "string"},
                    "newString": {"type": "string"},
                    "replaceAll": {"type": "boolean"},
                },
                "required": ["filePath", "oldString", "newString"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": "Apply multiple atomic string replacements to a single file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "oldString": {"type": "string"},
                                "newString": {"type": "string"},
                                "replaceAll": {"type": "boolean"},
                            },
                            "required": ["oldString", "newString"],
                        },
                    },
                },
                "required": ["filePath", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents using regex.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "include": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diagnostics",
            "description": "Run syntax diagnostics on a file or directory (.py, .json, .toml, .xml, .svg).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lint",
            "description": "Run a real linter (ruff when installed, else built-in syntax diagnostics) on a file or directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subagent",
            "description": "Delegate read-only codebase exploration to a fresh-context explore subagent and get back a compact summary with file:line references.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "What to investigate in the workspace."},
                    "agent": {"type": "string", "enum": ["explore"]},
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "webfetch",
            "description": "Fetch and extract readable text from an HTTP/HTTPS URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell/PowerShell command in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todowrite",
            "description": "Create or update the structured session todo list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "cancelled"],
                                },
                                "priority": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                },
                            },
                            "required": ["content", "status", "priority"],
                        },
                    }
                },
                "required": ["todos"],
            },
        },
    },
]
