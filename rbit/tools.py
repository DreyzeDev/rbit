"""Agent tools for rbit: read, write, edit, multi_edit, glob, grep, bash, todowrite, webfetch + Checkpoints."""

from __future__ import annotations

import ast
import asyncio
import difflib
import fnmatch
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
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

READONLY_TOOLS: frozenset[str] = frozenset(
    {"read", "glob", "grep", "webfetch", "diagnostics", "lint", "symbols", "git"}
)

MUTATING_TOOLS: frozenset[str] = frozenset({"write", "edit", "multi_edit", "apply_patch"})

VIRTUAL_ATTACHMENTS: frozenset[str] = frozenset({"diff", "git", "staged", "tree"})

MAX_CHECKPOINT_FILE_BYTES = 256_000
MAX_READ_BYTES = 512_000
MAX_CHECKPOINTS = 20

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Removes ANSI/VT100 escape sequences so shell output cannot break TUI rendering."""
    return _ANSI_RE.sub("", text)


def atomic_write_text(path: Path, content: str) -> None:
    """Writes text via a temp file + os.replace so a crash never truncates the target.

    Preserves the target's dominant line ending: without this, Windows text mode
    turns every `\\n` into `\\r\\n`, so editing an LF file rewrote every line
    (and vice versa), exploding git diffs and breaking follow-up edits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    try:
        original = path.read_bytes()
    except OSError:
        original = b""
    if b"\r\n" not in data and original.count(b"\r\n") * 2 > original.count(b"\n"):
        data = data.replace(b"\n", b"\r\n")
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    try:
        Path(tmp_name).write_bytes(data)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


DANGEROUS_BASH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(?:/|~|\$HOME)\s*(?:$|[;&|])", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-z]*f[a-z]*r[a-z]*\s+(?:/|~|\$HOME)\s*(?:$|[;&|])", re.IGNORECASE),
    re.compile(r"\bmkfs(?:\.\w+)?\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=.*\bof=/dev/", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.IGNORECASE),
    re.compile(r"\bgit\s+push\b[^\n]*--force\b|\bgit\s+push\b[^\n]*\s-f(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\bdiskpart\b|\bshutdown\b", re.IGNORECASE),
    re.compile(r"\bdel\s+/[sqf]{1,3}\b", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b[^\n]*-Recurse[^\n]*(?:[A-Za-z]:\\|/)", re.IGNORECASE),
)


def is_dangerous_command(command: str) -> str:
    """Returns the matched deny-pattern when `command` is catastrophic, else an empty string."""
    for pattern in DANGEROUS_BASH_PATTERNS:
        match = pattern.search(command)
        if match:
            return match.group(0)
    return ""


class GitIgnoreMatcher:
    """Lightweight `.gitignore` reader covering common patterns (globs, negations, dir rules)."""

    def __init__(self, workspace: Path) -> None:
        self.rules: list[tuple[str, bool, bool]] = []
        path = workspace / ".gitignore"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:].strip()
            if not line:
                continue
            dir_only = line.endswith("/")
            line = line.strip("/")
            if line:
                self.rules.append((line, negate, dir_only))

    def is_ignored(self, rel: str, is_dir: bool = False) -> bool:
        rel = rel.replace("\\", "/").strip("/")
        if not rel or not self.rules:
            return False
        ignored = False
        for pattern, negate, dir_only in self.rules:
            if dir_only and not is_dir and not rel.startswith(pattern + "/"):
                continue
            if "/" in pattern:
                matched = fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel, pattern + "/*")
            else:
                matched = any(fnmatch.fnmatchcase(part, pattern) for part in rel.split("/"))
            if matched:
                ignored = not negate
        return ignored


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


def html_to_markdown(raw: str) -> str:
    """Converts HTML to a compact readable Markdown outline (headings, links, lists)."""
    text = re.sub(
        r"<(script|style|noscript|nav|header|footer|aside)[^>]*>.*?</\1>",
        " ",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )

    def _heading(match: re.Match[str]) -> str:
        level = int(match.group(1))
        body = re.sub(r"<[^>]+>", "", match.group(2))
        body = html.unescape(re.sub(r"\s+", " ", body)).strip()
        return f"\n\n{'#' * level} {body}\n\n" if body else "\n\n"

    text = re.sub(r"<h([1-3])[^>]*>(.*?)</h\1>", _heading, text, flags=re.DOTALL | re.IGNORECASE)

    def _link(match: re.Match[str]) -> str:
        href = match.group(1).strip()
        label = re.sub(r"<[^>]+>", "", match.group(2))
        label = html.unescape(re.sub(r"\s+", " ", label)).strip()
        return f"[{label}]({href})" if label else href

    text = re.sub(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        _link,
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|section|article)>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass
class ToolResult:
    ok: bool
    summary: str
    output: str
    diff: str = ""
    duration_ms: int = 0
    pid: int = 0


PermissionCallback = Callable[[str, str], Awaitable[bool]]


class PathOutsideWorkspace(ValueError):
    """Raised when a tool path resolves outside the workspace and is not allowed."""

    def __init__(self, raw_path: str) -> None:
        super().__init__(f"path outside workspace: {raw_path}")
        self.raw_path = raw_path


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
        self.allow_outside_paths: bool = False
        self._auto_checkpoint_done: bool = False
        self._gitignore: GitIgnoreMatcher | None = None
        self._background_procs: list[asyncio.subprocess.Process] = []
        self._webfetch_cache: dict[str, tuple[float, str]] = {}
        self._file_cache: tuple[float, list[str]] | None = None

    def _gitignore_matcher(self) -> GitIgnoreMatcher:
        if self._gitignore is None:
            self._gitignore = GitIgnoreMatcher(self.workspace)
        return self._gitignore

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
            p = self.workspace / p
        p = p.resolve()
        try:
            p.relative_to(self.workspace)
        except ValueError:
            if not self._outside_paths_allowed():
                raise PathOutsideWorkspace(raw_path) from None
        return p

    def _outside_paths_allowed(self) -> bool:
        if self.allow_outside_paths:
            return True
        if self.get_config is not None:
            return bool(self.get_config().get("allow_outside_paths", False))
        return False

    def rel_display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    def invalidate_file_cache(self) -> None:
        self._file_cache = None

    def list_workspace_files(self, limit: int = 250) -> list[str]:
        now = time.monotonic()
        if self._file_cache is not None and now - self._file_cache[0] < 2.0:
            return self._file_cache[1][:limit]
        results: list[str] = []
        ignore = self._gitignore_matcher()
        for root, dirs, files in os.walk(self.workspace):
            root_rel = self.rel_display(Path(root))
            dirs[:] = [
                d
                for d in dirs
                if d not in IGNORED_DIRS
                and not ignore.is_ignored(f"{root_rel}/{d}" if root_rel != "." else d, is_dir=True)
            ]
            for fname in sorted(files):
                full = Path(root) / fname
                rel = self.rel_display(full)
                if ignore.is_ignored(rel):
                    continue
                results.append(rel)
                if len(results) >= 2000:
                    break
            if len(results) >= 2000:
                break
        self._file_cache = (now, results)
        return results[:limit]

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
            f"{summary} Â· {len(issues)} {noun}",
            f"{output}\n\nDiagnostics (syntax errors detected â€” fix immediately):\n{diag_block}",
        )

    # --- Undo / Redo ---
    def undo_last(self) -> str:
        if not self.undo_stack:
            return "No file changes to undo."
        snap = self.undo_stack.pop()
        target = Path(snap.path)
        if snap.existed_before:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, snap.previous_content)
        elif target.exists():
            target.unlink()
        self.invalidate_file_cache()
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
        atomic_write_text(target, snap.new_content)
        self.invalidate_file_cache()
        self.undo_stack.append(snap)
        rel = self.rel_display(target)
        if self.on_files_updated:
            self.on_files_updated()
        return f"Restored `{rel}` ({snap.step_label})"

    # --- Named Workspace Checkpoints (No Git required) ---
    MANIFEST_KEY = "__manifest__"

    def create_checkpoint(self, label: str = "") -> CheckpointMeta:
        files_map: dict[str, str] = {}
        manifest: list[str] = []
        self.invalidate_file_cache()
        for rel in self.list_workspace_files(limit=400):
            full = self.workspace / rel
            if not self._secret_reads_allowed() and is_secret_file(full):
                continue
            manifest.append(rel)
            try:
                if full.stat().st_size <= MAX_CHECKPOINT_FILE_BYTES:
                    files_map[rel] = full.read_text(encoding="utf-8")
            except Exception:
                # Binary or undecodable file: no text content to snapshot, but the
                # manifest entry keeps restore_checkpoint from ever deleting it.
                continue
        files_map[self.MANIFEST_KEY] = json.dumps(manifest, ensure_ascii=False)
        return self.storage.save_checkpoint(label or "Manual checkpoint", files_map)

    def restore_checkpoint(self, cid: str) -> str:
        loaded = self.storage.load_checkpoint_files(cid)
        if not loaded:
            return f"Checkpoint `{cid}` not found."
        meta, saved_files = loaded
        self.invalidate_file_cache()
        current_files = set(self.list_workspace_files(limit=400))

        # Files that existed at checkpoint time (text or binary). Older checkpoints
        # have no manifest; then the saved text files are the best approximation.
        manifest = set(saved_files.keys())
        raw_manifest = saved_files.pop(self.MANIFEST_KEY, None)
        if raw_manifest:
            try:
                manifest = manifest | set(json.loads(raw_manifest))
            except (TypeError, ValueError):
                pass

        # Restore saved text files
        restored = 0
        for rel, content in saved_files.items():
            full = self.workspace / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(full, content)
            restored += 1
        # Remove newly created files that didn't exist at checkpoint time. Only
        # files a snapshot could hold (decodable text, within the size limit) are
        # deletable — restore could never recreate a binary file, so it must stay.
        removed = 0
        for rel in current_files - manifest:
            full = self.workspace / rel
            if not self._secret_reads_allowed() and is_secret_file(full):
                continue
            try:
                if not full.exists() or full.stat().st_size > MAX_CHECKPOINT_FILE_BYTES:
                    continue
                full.read_text(encoding="utf-8")
            except Exception:
                continue
            full.unlink()
            removed += 1

        self.invalidate_file_cache()
        return f"Rewound workspace to `{meta.label}` ({restored} restored, {removed} removed)."

    # --- Direct Bang Shell Execution (!command) ---
    async def execute_bang_shell(self, command: str) -> ToolResult:
        t0 = time.perf_counter()
        res = await self._run_subprocess(command)
        self.invalidate_file_cache()
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
        if agent_mode == "plan" and name in ("write", "edit", "multi_edit", "apply_patch"):
            return ToolResult(
                ok=False,
                summary=f"{name} blocked in Plan mode",
                output="Plan mode is read-only. Press Tab to switch to Build mode to modify files.",
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )

        try:
            if name in ("write", "edit", "multi_edit", "apply_patch"):
                self._maybe_auto_checkpoint()

            if name == "read":
                res = await asyncio.to_thread(self._tool_read, args)
            elif name == "write":
                res = self._tool_write(args)
            elif name == "edit":
                res = self._tool_edit(args)
            elif name == "multi_edit":
                res = self._tool_multi_edit(args)
            elif name == "apply_patch":
                res = self._tool_apply_patch(args)
            elif name == "glob":
                res = await asyncio.to_thread(self._tool_glob, args)
            elif name == "grep":
                res = await asyncio.to_thread(self._tool_grep, args)
            elif name == "symbols":
                res = await asyncio.to_thread(self._tool_symbols, args)
            elif name == "git":
                res = await self._tool_git(args)
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
        except PathOutsideWorkspace as exc:
            res = ToolResult(
                False,
                f"path outside workspace: {exc.raw_path}",
                f"`{exc.raw_path}` resolves outside the workspace. "
                "Pass a relative path inside the project, or enable `allow_outside_paths`.",
            )
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

        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        if size > MAX_READ_BYTES and "offset" not in args and "limit" not in args:
            size_kb = size / 1024
            return ToolResult(
                False,
                f"{rel} (too large: {size_kb:,.0f} KB)",
                f"File is {size_kb:,.0f} KB, above the {MAX_READ_BYTES // 1024} KB read budget. "
                "Use `grep`/`symbols` to locate what you need, or pass `offset` and `limit` to read a slice.",
            )

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
        atomic_write_text(target, content)
        self.invalidate_file_cache()

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

    @staticmethod
    def _fuzzy_spans(content: str, old: str) -> list[tuple[int, int]]:
        """Finds whitespace-normalized line-block matches of `old` inside `content`."""
        if not old.strip():
            return []
        c_lines = content.splitlines(keepends=True)
        o_lines = old.splitlines()
        if not o_lines or len(o_lines) > len(c_lines):
            return []
        offsets = [0]
        for line in c_lines:
            offsets.append(offsets[-1] + len(line))
        normalized = [" ".join(line.split()) for line in c_lines]
        target = [" ".join(line.split()) for line in o_lines]
        spans: list[tuple[int, int]] = []
        for start in range(len(c_lines) - len(o_lines) + 1):
            if normalized[start : start + len(o_lines)] == target:
                spans.append((offsets[start], offsets[start + len(o_lines)]))
        return spans

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
        span: tuple[int, int] | None = None
        notes: list[str] = []
        if count == 0:
            spans = self._fuzzy_spans(old_content, old_str)
            if not spans:
                return ToolResult(False, "oldString not found", "oldString not found in file content.")
            if len(spans) > 1 and not replace_all:
                return ToolResult(
                    False,
                    f"{len(spans)} ambiguous fuzzy matches",
                    f"Found {len(spans)} whitespace-normalized matches; add context or set replaceAll=true.",
                )
            span = spans[0]
            notes.append("fuzzy match (whitespace normalized) — verify the diff")
        elif count > 1 and not replace_all:
            return ToolResult(
                False,
                f"{count} ambiguous matches",
                f"Found {count} matches for oldString; add context or set replaceAll=true.",
            )

        if span is not None:
            replaced = old_content[span[0] : span[1]]
            addition = new_str
            # The span covers the matched lines including their trailing newline;
            # keep it, or the replacement glues the next line onto the last one.
            if replaced.endswith("\n") and not addition.endswith("\n"):
                addition += "\n"
            new_content = old_content[: span[0]] + addition + old_content[span[1] :]
        else:
            new_content = (
                old_content.replace(old_str, new_str)
                if replace_all
                else old_content.replace(old_str, new_str, 1)
            )
        atomic_write_text(target, new_content)
        self.invalidate_file_cache()

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
            f"Edited {rel} (+{added} -{removed} lines)"
            + (f"\n\nNote: {notes[0]}" if notes else ""),
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

        atomic_write_text(target, updated)
        self.invalidate_file_cache()
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

    @staticmethod
    def _parse_unified_diff(patch: str) -> tuple[str | None, list[tuple[int, int, list[str]]]]:
        """Parses a unified diff into (target_path, [(old_start, old_len, hunk_lines)])."""
        target: str | None = None
        hunks: list[tuple[int, int, list[str]]] = []
        cur: list[str] | None = None
        old_start = 0
        old_len = 0
        for raw in patch.replace("\r\n", "\n").split("\n"):
            if raw.startswith("--- "):
                cur = None
                continue
            if raw.startswith("+++ "):
                cur = None
                name = raw[4:].strip().split("\t")[0].strip()
                if name.startswith("b/"):
                    name = name[2:]
                if name and name != "/dev/null":
                    target = name
                continue
            if raw.startswith("@@"):
                cur = []
                header = raw.split("@@")[1].strip()
                old_part = header.split()[0] if header else "-0,0"
                old_start = abs(int(old_part[1:].split(",")[0] or 0))
                old_len = int(old_part.split(",")[1]) if "," in old_part else 1
                hunks.append((old_start, old_len, cur))
                continue
            if cur is None:
                continue
            if raw.startswith("\\"):
                continue
            if raw == "":
                continue
            if raw[0] not in (" ", "+", "-"):
                continue
            cur.append(raw)
        return target, hunks

    def _tool_apply_patch(self, args: dict[str, Any]) -> ToolResult:
        patch = str(args.get("patch", "")).strip("\n")
        if not patch.strip():
            return ToolResult(False, "empty patch", "`patch` must contain a unified diff.")

        target_hint, hunks = self._parse_unified_diff(patch)
        if not hunks:
            return ToolResult(False, "no hunks", "No `@@` hunks found in the patch.")

        raw = str(args.get("filePath") or args.get("path") or target_hint or "").strip()
        if not raw:
            return ToolResult(False, "missing filePath", "Provide `filePath` or a `+++ b/<path>` header.")

        target = self.resolve_path(raw)
        rel = self.rel_display(target)
        existed = target.exists()
        old_content = target.read_text(encoding="utf-8", errors="replace") if existed else ""
        lines = old_content.split("\n") if old_content else []
        if lines and lines[-1] == "":
            lines.pop()

        output = list(lines)
        cursor = 0
        for old_start, old_len, hunk_lines in hunks:
            start = max(0, old_start - 1) if old_start > 0 else 0
            if start < cursor:
                return ToolResult(
                    False,
                    f"overlapping hunk at line {old_start}",
                    f"Hunks are out of order or overlapping near line {old_start}.",
                )
            if start > len(output):
                return ToolResult(
                    False,
                    f"hunk out of range at line {old_start}",
                    f"Hunk targets line {old_start} but the file has {len(output)} lines.",
                )
            cursor = start
            for hline in hunk_lines:
                op, text = hline[0], hline[1:]
                if op == "+":
                    output.insert(cursor, text)
                    cursor += 1
                    continue
                if cursor >= len(output):
                    return ToolResult(
                        False,
                        f"patch context beyond EOF at line {cursor + 1}",
                        f"Cannot match `{text}` — file ended at line {cursor}.",
                    )
                if output[cursor] != text and output[cursor].rstrip() != text.rstrip():
                    return ToolResult(
                        False,
                        f"context mismatch at line {cursor + 1}",
                        f"Expected `{text}` but found `{output[cursor]}` at line {cursor + 1}. "
                        "Re-read the file and regenerate the patch.",
                    )
                if op == " ":
                    cursor += 1
                else:
                    del output[cursor]

        new_content = "\n".join(output)
        if old_content.endswith("\n") or not existed:
            new_content += "\n"

        atomic_write_text(target, new_content)
        self.invalidate_file_cache()

        self.undo_stack.append(
            FileSnapshot(
                step_label=f"apply_patch {rel}",
                path=str(target),
                existed_before=existed,
                previous_content=old_content,
                new_content=new_content,
            )
        )
        self.redo_stack.clear()

        diff, added, removed = self._compute_diff(old_content, new_content, rel)
        self._record_file_change(rel, added, removed)
        summary, out = self._attach_diagnostics(
            f"{len(hunks)} hunks (+{added} -{removed})",
            f"Applied patch to {rel} (+{added} -{removed} lines)",
            rel,
            new_content,
        )
        return ToolResult(True, summary, out, diff=diff)

    def _tool_glob(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args.get("pattern", "*"))
        matches: list[str] = []
        ignore = self._gitignore_matcher()
        for p in self.workspace.rglob(pattern if "**" not in pattern else pattern.replace("**/", "")):
            if any(part in IGNORED_DIRS for part in p.parts):
                continue
            if ignore.is_ignored(self.rel_display(p), is_dir=p.is_dir()):
                continue
            if p.is_file():
                matches.append(self.rel_display(p))
            if len(matches) >= 100:
                break
        matches.sort()
        out = "\n".join(matches) if matches else "No files matched."
        return ToolResult(True, f"{len(matches)} matches", out)

    def _grep_with_ripgrep(
        self,
        pattern: str,
        include: str,
        files_only: bool,
        context: int,
        max_per_file: int,
    ) -> ToolResult | None:
        rg = shutil.which("rg")
        if not rg:
            return None
        cmd = [rg, "--line-number", "--no-heading", "--color", "never", "-i"]
        # Respect .gitignore even outside a git repo, like the fallback path does.
        cmd.append("--no-require-git")
        if files_only:
            cmd.append("-l")
        if context:
            cmd.extend(["-C", str(context)])
        if max_per_file:
            cmd.extend(["--max-count", str(max_per_file)])
        if include != "*":
            cmd.extend(["--glob", include])
        cmd.extend(["--", pattern, "."])
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.workspace),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        lines: list[str] = []
        matched: list[str] = []
        for raw_line in proc.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # Normalize the path prefix to match the fallback output ("a.py:1:...",
            # never ".\a.py" or "./a.py"); leave the matched text untouched.
            path_part, sep, rest = line.partition(":")
            if path_part[:2] in ("./", ".\\"):
                path_part = path_part[2:]
            rel = path_part.replace("\\", "/") + sep + rest
            if files_only:
                if is_secret_file(self.workspace / rel) and not self._secret_reads_allowed():
                    continue
                matched.append(rel)
                continue
            path_part = rel.split(":", 1)[0]
            if is_secret_file(self.workspace / path_part) and not self._secret_reads_allowed():
                continue
            lines.append(rel[:200])
        if proc.returncode not in (0, 1):
            # 2+ means rg itself failed (bad regex, IO error); fall back so the
            # caller surfaces a real error instead of a bogus "no matches".
            return None
        if files_only:
            out = "\n".join(matched) if matched else "No matches found."
            return ToolResult(True, f"{len(matched)} files (rg)", out)
        out = "\n".join(lines) if lines else "No matches found."
        return ToolResult(True, f"{len(lines)} lines (rg)", out)

    def _tool_grep(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args.get("pattern", ""))
        include = str(args.get("include", "*"))
        files_only = bool(args.get("filesOnly") or args.get("files_only") or args.get("-l"))
        try:
            context = max(0, min(8, int(args.get("context", 0) or 0)))
        except (TypeError, ValueError):
            context = 0
        try:
            raw_per_file = int(args.get("maxPerFile", 0) or 0)
        except (TypeError, ValueError):
            raw_per_file = 0
        max_per_file = max(1, raw_per_file) if raw_per_file else 0
        if not pattern:
            return ToolResult(False, "empty pattern", "Regex pattern is required.")
        rg_result = self._grep_with_ripgrep(pattern, include, files_only, context, max_per_file)
        if rg_result is not None:
            return rg_result
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return ToolResult(False, "invalid regex", f"Invalid regex pattern: {exc}")
        hits: list[str] = []
        matched_files: list[str] = []
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
            lines = text.splitlines()
            file_hits = 0
            for idx, line in enumerate(lines, start=1):
                if not regex.search(line):
                    continue
                file_hits += 1
                if files_only:
                    break
                if context:
                    start = max(1, idx - context)
                    end = min(len(lines), idx + context)
                    for n in range(start, end + 1):
                        marker = ":" if n == idx else "-"
                        hits.append(f"{rel}{marker}{n}{marker} {lines[n - 1].strip()[:140]}")
                    hits.append("--")
                else:
                    hits.append(f"{rel}:{idx}: {line.strip()[:140]}")
                if max_per_file and file_hits >= max_per_file:
                    break
                if len(hits) >= 80:
                    break
            if file_hits:
                matched_files.append(rel)
            if len(hits) >= 80:
                break
        if files_only:
            out = "\n".join(matched_files) if matched_files else "No matches found."
            return ToolResult(True, f"{len(matched_files)} files", out)
        if context and hits and hits[-1] == "--":
            hits.pop()
        out = "\n".join(hits) if hits else "No matches found."
        return ToolResult(True, f"{len(hits)} lines", out)

    @staticmethod
    def _symbols_for_content(rel: str, content: str) -> list[str]:
        ext = Path(rel).suffix.lower()
        found: list[str] = []
        if ext in (".py", ".pyi", ".pyw"):
            try:
                tree = ast.parse(content)
            except SyntaxError as err:
                return [f"{rel}: syntax error at line {err.lineno or 1}: {err.msg}"]
            seen: set[int] = set()

            def visit(node: ast.AST, prefix: str = "") -> None:
                for child in ast.iter_child_nodes(node):
                    if id(child) in seen:
                        continue
                    seen.add(id(child))
                    if isinstance(child, ast.ClassDef):
                        bases = ", ".join(ast.unparse(b) for b in child.bases)
                        label = f"class {child.name}" + (f"({bases})" if bases else "")
                        found.append(f"{rel}:{child.lineno}: {prefix}{label}")
                        visit(child, prefix + child.name + ".")
                    elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        kind = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                        try:
                            sig = ast.unparse(child.args)
                        except Exception:
                            sig = "..."
                        found.append(f"{rel}:{child.lineno}: {prefix}{kind} {child.name}({sig})")
                        visit(child, prefix)
                    else:
                        visit(child, prefix)

            visit(tree)
        elif ext in (".js", ".jsx", ".ts", ".tsx"):
            patterns = (
                (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)"), "function"),
                (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)(?:\s+extends\s+([\w.]+))?"), "class"),
                (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>"), "const"),
            )
            for lineno, line in enumerate(content.splitlines(), start=1):
                for pattern, kind in patterns:
                    match = pattern.match(line)
                    if not match:
                        continue
                    if kind == "class":
                        label = f"class {match.group(1)}"
                        if match.group(2):
                            label += f" extends {match.group(2)}"
                    else:
                        args = (match.group(2) or "").strip()
                        label = f"{kind} {match.group(1)}({args})"
                    found.append(f"{rel}:{lineno}: {label}")
                    break
        elif ext in (".md", ".markdown"):
            for lineno, line in enumerate(content.splitlines(), start=1):
                match = re.match(r"^(#{1,6})\s+(.*)$", line)
                if match:
                    found.append(f"{rel}:{lineno}: {match.group(1)} {match.group(2).strip()[:90]}")
        return found

    def _tool_symbols(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("filePath") or args.get("path") or ".").strip()
        target = self.resolve_path(raw)
        rel = self.rel_display(target)
        if not target.exists():
            return ToolResult(False, f"{rel} not found", f"Path not found: {rel}")

        if target.is_file():
            targets = [target]
        else:
            allowed = {".py", ".pyi", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".md", ".markdown"}
            targets = [
                self.workspace / item
                for item in self.list_workspace_files(limit=300)
                if Path(item).suffix.lower() in allowed
            ]

        found: list[str] = []
        for fpath in targets:
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            found.extend(self._symbols_for_content(self.rel_display(fpath), content))
            if len(found) >= 300:
                found = found[:300]
                break

        out = "\n".join(found) if found else "No symbols found."
        return ToolResult(True, f"{len(found)} symbols", out)

    async def _tool_git(self, args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "status").strip().lower()
        raw_path = str(args.get("path") or "").strip()
        limit = max(1, min(int(args.get("limit", 20) or 20), 100))
        staged = bool(args.get("staged", False))

        path_args: list[str] = []
        if raw_path:
            target = self.resolve_path(raw_path)
            try:
                target.relative_to(self.workspace)
            except ValueError:
                return ToolResult(False, f"path outside workspace: {raw_path}", "`path` must stay inside the workspace.")
            path_args = ["--", self.rel_display(target)]

        if action == "status":
            cmd = ["status", "--short"]
        elif action == "diff":
            cmd = ["diff"] + (["--staged"] if staged else []) + path_args
        elif action == "log":
            cmd = ["log", "--oneline", f"-{limit}"] + path_args
        elif action == "show":
            cmd = ["show", "--stat", "HEAD"] + path_args
        elif action == "blame":
            if not raw_path:
                return ToolResult(False, "blame needs path", "`path` is required for git blame.")
            cmd = ["blame", raw_path]
        else:
            return ToolResult(
                False,
                f"unsupported git action: {action}",
                "Allowed read-only actions: status, diff, log, show, blame.",
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *cmd,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _err = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except Exception as exc:
            return ToolResult(False, f"git {action} failed", f"git execution error: {exc}")

        output = out_b.decode("utf-8", errors="replace").strip() or "(no output)"
        ok = proc.returncode == 0
        summary = f"git {action} Â· {len(output.splitlines())} line(s)"
        return ToolResult(ok, summary, output[:6000])

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
        note = "ruff/pyflakes not installed â€” ran built-in syntax diagnostics only (pip install ruff)."
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
                "completed": "[âœ“]",
                "in_progress": "[â€¢]",
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
        cached = self._webfetch_cache.get(url)
        if cached and time.time() - cached[0] < 600:
            return ToolResult(True, f"cached · Markdown {len(cached[1])} chars", cached[1])
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "rbit-cli/1.2"})
        raw = resp.text
        cleaned = html_to_markdown(raw) if "<" in raw else raw.strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned[:6000] or "(empty response)"
        if resp.status_code < 400:
            self._webfetch_cache[url] = (time.time(), cleaned)
        return ToolResult(
            resp.status_code < 400,
            f"HTTP {resp.status_code} · Markdown {len(cleaned)} chars",
            cleaned,
        )

    async def _tool_bash(self, args: dict[str, Any], agent_mode: str = "build") -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(False, "empty command", "command is required.")

        danger = is_dangerous_command(command)
        if danger:
            return ToolResult(
                False,
                f"blocked: dangerous command ({danger[:32]})",
                "This command matches a destructive deny-pattern (rm -rf /, mkfs, disk formatting, "
                "force-push, fork bomb, ...). Refusing to execute it even in build mode.",
            )

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

        try:
            timeout_s = float(args.get("timeout", 30) or 30)
        except (TypeError, ValueError):
            timeout_s = 30.0
        timeout_s = max(1.0, min(600.0, timeout_s))

        if args.get("background") or args.get("runInBackground"):
            result = await self._run_background(command)
        else:
            result = await self._run_subprocess(command, timeout_s=timeout_s)
        self.invalidate_file_cache()
        return result

    async def _run_background(self, command: str) -> ToolResult:
        if os.name == "nt":
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        self._background_procs.append(proc)
        return ToolResult(
            True,
            f"background pid {proc.pid}",
            f"Started in background (pid {proc.pid}): {command}\n"
            "Output is discarded; poll the process or write results to a file. "
            "Click the card (or press Ctrl+X) to stop it.",
            pid=proc.pid,
        )

    async def terminate_background(self) -> int:
        count = 0
        for proc in self._background_procs:
            if proc.returncode is None:
                try:
                    proc.terminate()
                    count += 1
                except ProcessLookupError:
                    pass
            try:
                await proc.wait()
            except Exception:
                pass
        self._background_procs.clear()
        return count

    def background_pids(self) -> list[int]:
        """Returns PIDs of still-running background shells started this session."""
        return [p.pid for p in self._background_procs if p.returncode is None and p.pid]

    async def terminate_background_pid(self, pid: int) -> bool:
        """Terminates a single background process by pid; returns True when it was killed."""
        for proc in list(self._background_procs):
            if proc.pid != pid:
                continue
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            try:
                await proc.wait()
            except Exception:
                pass
            self._background_procs.remove(proc)
            return True
        return False

    async def _run_subprocess(self, command: str, timeout_s: float = 30.0) -> ToolResult:
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
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return ToolResult(
                False,
                f"timeout: {command[:36]}",
                f"Command timed out after {timeout_s:g}s. Increase the `timeout` argument "
                "(max 600s) or use `background=true` for long-running processes.",
            )

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        combined = "\n".join(part for part in (stdout, stderr) if part) or "(no output)"
        combined = strip_ansi(combined)
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
            "description": "Replace an exact string in an existing file (falls back to whitespace-normalized fuzzy matching when no exact match exists).",
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
            "name": "apply_patch",
            "description": "Apply a unified diff (one `@@` hunk or many) to a file. Precise alternative to `edit` for larger multi-line changes; supports file creation when the file does not exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Target file path (optional when the patch has a `+++ b/<path>` header).",
                    },
                    "patch": {
                        "type": "string",
                        "description": "Unified diff text with `@@ -old,count +new,count @@` hunk headers.",
                    },
                },
                "required": ["patch"],
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
            "description": "Search file contents using regex, with optional context lines, per-file caps, and file-only output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "include": {"type": "string"},
                    "context": {"type": "integer", "description": "Lines of context around each match (0-8)."},
                    "maxPerFile": {"type": "integer", "description": "Maximum matched lines per file."},
                    "filesOnly": {"type": "boolean", "description": "Return only file names with matches (-l)."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "symbols",
            "description": "AST/regex symbol index: Python defs/classes, JS/TS functions, Markdown headings with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "File or directory (defaults to workspace root)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git",
            "description": "Read-only Git helper: status, diff, log, show, blame.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "diff", "log", "show", "blame"],
                    },
                    "path": {"type": "string", "description": "Optional path filter (required for blame)."},
                    "limit": {"type": "integer", "description": "Log entries limit (default 20)."},
                    "staged": {"type": "boolean", "description": "Use --staged for diff."},
                },
                "required": ["action"],
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
            "description": "Execute a shell/PowerShell command in the workspace (supports timeout and background execution).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number", "description": "Seconds before aborting (1-600, default 30)."},
                    "background": {"type": "boolean", "description": "Start detached and return immediately with a pid."},
                },
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
