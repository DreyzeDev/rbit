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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlparse

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

import httpx

from .formatters import format_file, formatter_enabled
from .guard import guarded as guard_untrusted, risk_level, scan as scan_untrusted
from .models import CheckpointMeta, FileChangeStat, FileSnapshot, Session, TodoItem, short_id
from .permissions import (
    EDIT_ACTIONS,
    EXTERNAL_ACTIONS,
    mode_decision,
    permission_mode_from_config,
)
from .skills import load_skills, skill_tool_schema
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
    "grep",
    "findstr",
    "head",
    "tail",
    "wc",
    "python --version",
    "node -v",
    "npm -v",
    "Get-ChildItem",
    "Get-Location",
    "Test-Path",
    "pytest",
)

READONLY_TOOLS: frozenset[str] = frozenset(
    {"read", "glob", "grep", "webfetch", "websearch", "diagnostics", "lint", "symbols", "git", "list_changes", "skill", "risk_review", "read_many_files", "lsp"}
)

MUTATING_TOOLS: frozenset[str] = frozenset({"write", "edit", "multi_edit", "apply_patch"})

_STATEMENT_SPLIT = re.compile(r"(?:;|\|\||&&|\||\r?\n)")


class _SearchResultsParser(HTMLParser):
    """Collects DuckDuckGo HTML result anchors and snippets."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = attributes.get("class", "").split()
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self._current = {"title": "", "url": attributes.get("href", ""), "snippet": ""}
            self.results.append(self._current)
            self._in_title = True
        elif tag == "a" and self._current is not None and not self._current.get("url"):
            self._current["url"] = attributes.get("href", "")
        elif "result__snippet" in classes or "result-snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_title = False
        if tag in ("td", "div", "span") and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self._current["title"] = (self._current["title"] + " " + text).strip()
        elif self._in_snippet:
            self._current["snippet"] = (self._current["snippet"] + " " + text).strip()


def _clean_search_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return url


def is_readonly_bash_command(command: str) -> bool:
    """A command is read-only only if EVERY statement starts with a whitelisted
    prefix and there are no output redirects. `echo x; rm y` and `ls > f` are
    mutations, even though the first token looks harmless."""
    if ">" in command:
        return False
    statements = [s.strip() for s in _STATEMENT_SPLIT.split(command) if s.strip()]
    if not statements:
        return False
    for statement in statements:
        lowered = statement.lower()
        if not any(lowered.startswith(pref.lower()) for pref in READONLY_BASH_PREFIXES):
            return False
    return True

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
        "prod.env",
        ".envrc",
        "secrets.yaml",
        "secrets.yml",
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

PERMISSION_PATH_TOOLS: dict[str, str] = {
    "read": "read",
    "write": "edit",
    "edit": "edit",
    "multi_edit": "edit",
    "apply_patch": "edit",
    "notebook": "edit",
    "symbols": "read",
    "diagnostics": "read",
    "lint": "read",
}


def _expand_home(value: str) -> str:
    if value == "~" or value.startswith("~/"):
        return str(Path.home()) + value[1:]
    if value == "$HOME" or value.startswith("$HOME/"):
        return str(Path.home()) + value[5:]
    return value


def permission_pattern_matches(pattern: str, value: str, *, action: str = "") -> bool:
    """Whole-value wildcard matching (`*`, `?`) with opencode's shell suffix rule."""
    pattern = _expand_home(str(pattern)).replace("\\", "/")
    value = str(value).replace("\\", "/")
    if action == "shell" and pattern.endswith(" *") and not value.rstrip().endswith(" "):
        bare = pattern[:-2]
        if value.strip() == bare:
            return True
    lower = action in ("read", "edit", "glob", "external_directory") or (action == "shell" and os.name == "nt")
    if lower:
        pattern = pattern.lower()
        value = value.lower()
    return fnmatch.fnmatchcase(value, pattern)


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
        mcp: Any = None,
        lsp: Any = None,
        plugins: Any = None,
        http_client: Any = None,
        question_cb: Callable[[str, list[str]], Awaitable[str | None]] | None = None,
        audit: Any = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.session = session
        self.storage = storage or StorageManager(self.workspace)
        self.permission_cb = permission_cb
        self.on_todos_updated = on_todos_updated
        self.on_files_updated = on_files_updated
        self.get_config = get_config
        self.mcp = mcp
        self.lsp = lsp
        self.plugins = plugins
        self.http_client = http_client
        self.question_cb = question_cb
        self.audit = audit
        self._terminal_shell: Any = None
        self._browser_session: Any = None
        self.undo_stack: list[FileSnapshot] = []
        self.redo_stack: list[FileSnapshot] = []
        self.always_allow_bash: bool = False
        self.allowed_bash_prefixes: list[str] = []
        self.saved_permissions: list[dict[str, str]] = []
        self.allow_secret_reads: bool = False
        self.allow_outside_paths: bool = False
        self._auto_checkpoint_done: bool = False
        self._gitignore: GitIgnoreMatcher | None = None
        self._background_procs: list[asyncio.subprocess.Process] = []
        self._webfetch_cache: dict[str, tuple[float, str]] = {}
        self._websearch_cache: dict[str, tuple[float, str]] = {}
        self._file_cache: tuple[float, list[str]] | None = None
        self.session_readonly: bool = False
        self.extra_roots: list[Path] = []

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
        # Monorepo / approved external-directory support.
        for root in self.extra_roots:
            try:
                p.relative_to(root)
                return p
            except ValueError:
                continue
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

    # --- Permissions (opencode-compatible ordered rules) ---
    def permission_rule_chain(self, agent_mode: str = "build") -> list[dict[str, str]]:
        """Global rules first, agent rules last (later matches win)."""
        cfg = self.get_config() if self.get_config is not None else {}
        rules: list[dict[str, str]] = []
        legacy = cfg.get("permission")
        if isinstance(legacy, dict):
            for action, mapping in legacy.items():
                if isinstance(mapping, dict):
                    for resource, effect in mapping.items():
                        if isinstance(effect, str):
                            rules.append({"action": str(action), "resource": str(resource), "effect": effect})
                elif isinstance(mapping, str):
                    rules.append({"action": str(action), "resource": "*", "effect": mapping})
        configured = cfg.get("permissions")
        if isinstance(configured, list):
            for entry in configured:
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action")
                resource = entry.get("resource")
                effect = entry.get("effect")
                if isinstance(action, str) and isinstance(resource, str) and effect in ("allow", "deny", "ask"):
                    rules.append({"action": action, "resource": resource, "effect": effect})
        agents = cfg.get("agents")
        if isinstance(agents, dict):
            agent_cfg = agents.get(agent_mode)
            if isinstance(agent_cfg, dict) and isinstance(agent_cfg.get("permissions"), list):
                for entry in agent_cfg["permissions"]:
                    if not isinstance(entry, dict):
                        continue
                    action = entry.get("action")
                    resource = entry.get("resource")
                    effect = entry.get("effect")
                    if isinstance(action, str) and isinstance(resource, str) and effect in ("allow", "deny", "ask"):
                        rules.append({"action": action, "resource": resource, "effect": effect})
        rules.extend(self.saved_permissions)
        return rules

    def remember_permission(self, action: str, resource: str, effect: str = "allow") -> None:
        """Stores an in-session approval (the TUI 'always allow' choice)."""
        entry = {"action": action, "resource": resource, "effect": effect}
        if entry not in self.saved_permissions:
            self.saved_permissions.append(entry)

    def resolve_permission_effect(self, action: str, resource: str, agent_mode: str = "build") -> str | None:
        """Returns the last matching effect, or None when no rule covers the request."""
        matched: str | None = None
        for rule in self.permission_rule_chain(agent_mode):
            if not permission_pattern_matches(rule["action"], action):
                continue
            if permission_pattern_matches(rule["resource"], resource, action=action):
                matched = rule["effect"]
        return matched

    def current_permission_mode(self) -> str:
        """Effective permission mode (env override, then config, then default)."""
        cfg = self.get_config() if self.get_config is not None else {}
        return permission_mode_from_config(cfg if isinstance(cfg, dict) else {})

    def mode_allows(self, action: str) -> bool:
        """True when the active mode silences prompts for this action class."""
        return mode_decision(self.current_permission_mode(), action) == "allow"

    def _audit_permission(self, action: str, resource: str, allowed: bool, reason: str) -> None:
        if self.audit is None:
            return
        self.audit.permission(
            action,
            resource,
            "allow" if allowed else "deny",
            reason,
        )

    async def _authorize(self, action: str, resource: str, agent_mode: str) -> tuple[bool, str]:
        allowed, reason = await self._authorize_inner(action, resource, agent_mode)
        # Only decisions the user is affected by: "no-rule" is the silent default.
        if reason != "no-rule":
            self._audit_permission(action, resource, allowed, reason)
        return allowed, reason

    async def _authorize_inner(
        self, action: str, resource: str, agent_mode: str
    ) -> tuple[bool, str]:
        effect = self.resolve_permission_effect(action, resource, agent_mode)
        if effect == "deny":
            # An explicit `deny` is the user's veto: no mode may override it.
            return False, "deny"
        if effect == "allow":
            return True, "allow"

        mode = self.current_permission_mode()

        # `auto` means literally "never prompt", so it also silences rules that
        # asked to prompt for a specific resource. `deny` already returned above,
        # which is why the flag is safe to honor here.
        if mode == "auto":
            return True, "mode"

        # Otherwise a mode is only a default: an explicit `ask` rule stands
        # (the user singled this resource out), and modes fill the "no rule" gap.
        # Shell is excluded because `_tool_bash` owns that gate — it knows
        # whether a command is read-only.
        if effect is None:
            if action in EDIT_ACTIONS or action in EXTERNAL_ACTIONS:
                decision = mode_decision(mode, action)
                if decision == "allow":
                    return True, "mode"
                if decision == "ask":
                    if self.permission_cb is None:
                        return False, "approval-required"
                    approved = await self.permission_cb(
                        "bash" if action == "shell" else action, resource
                    )
                    return bool(approved), "mode-ask"
            return True, "no-rule"

        # effect == "ask": the user asked to be prompted for this one.
        if self.permission_cb is None:
            return False, "approval-required"
        approved = await self.permission_cb("bash" if action == "shell" else action, resource)
        return bool(approved), "ask"

    def _permission_requests(self, name: str, args: dict[str, Any]) -> list[tuple[str, str]]:
        """Maps a tool call to opencode-style (action, resource) pairs."""
        requests: list[tuple[str, str]] = []
        path_action = PERMISSION_PATH_TOOLS.get(name)
        if path_action:
            raw = args.get("filePath") or args.get("path")
            if isinstance(raw, str) and raw.strip():
                requests.append((path_action, raw.strip()))
            if name == "apply_patch":
                for line in str(args.get("patch") or "").splitlines():
                    if line.startswith("+++ "):
                        candidate = line[4:].strip().split("\t", 1)[0]
                        candidate = candidate.removeprefix("b/")
                        if candidate and candidate != "/dev/null":
                            requests.append((path_action, candidate))
        elif name in ("glob", "grep"):
            pattern = str(args.get("pattern") or "").strip()
            if pattern:
                requests.append((name, pattern))
        elif name == "webfetch":
            url = str(args.get("url") or "").strip()
            if url:
                requests.append(("webfetch", url))
        elif name == "subagent":
            agent = str(args.get("agent") or "explore").strip() or "explore"
            requests.append(("subagent", agent))
            requests.append(("task", agent))
        elif name == "bash":
            command = str(args.get("command") or "").strip()
            if command:
                requests.append(("shell", command))
        elif name == "terminal":
            command = str(args.get("command") or "").strip()
            if command:
                requests.append(("shell", command))
        elif self.mcp is not None and name in getattr(self.mcp, "tool_names", set()):
            requests.append((name, "*"))
        return requests

    def _permission_resource_path(self, raw: str) -> tuple[str, Path | None]:
        """Returns the rule resource (relative inside, absolute outside) and the target path."""
        candidate = Path(_expand_home(raw))
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            target = candidate.resolve()
        except OSError:
            target = candidate
        try:
            relative = target.relative_to(self.workspace)
            return relative.as_posix(), target
        except ValueError:
            return target.as_posix(), target

    async def _preflight_permissions(
        self, name: str, args: dict[str, Any], agent_mode: str
    ) -> ToolResult | None:
        requests = self._permission_requests(name, args)
        if not requests:
            return None
        for action, raw_resource in requests:
            external = False
            resource = raw_resource
            target: Path | None = None
            if action in ("read", "edit"):
                resource, target = self._permission_resource_path(raw_resource)
                if target is not None:
                    try:
                        target.relative_to(self.workspace)
                    except ValueError:
                        in_extra = False
                        for root in self.extra_roots:
                            try:
                                target.relative_to(root)
                                in_extra = True
                                break
                            except ValueError:
                                continue
                        external = not in_extra
            if external and target is not None:
                boundary = f"{target.parent.as_posix()}/*"
                allowed, reason = await self._authorize("external_directory", boundary, agent_mode)
                if reason == "no-rule":
                    boundary = target.parent.as_posix()
                    allowed, reason = await self._authorize("external_directory", boundary, agent_mode)
                if reason == "no-rule":
                    allowed, reason = await self._authorize("external_directory", "*", agent_mode)
                if reason != "no-rule" and not allowed:
                    return ToolResult(
                        False,
                        f"blocked: external path ({target.name})",
                        f"`{target}` is outside the workspace and `external_directory` is set to "
                        f"`{reason}`. Adjust `permissions` or enable `allow_outside_paths`.",
                    )
                if reason != "no-rule" and allowed and target.parent not in self.extra_roots:
                    self.extra_roots.append(target.parent)
            allowed, reason = await self._authorize(action, resource, agent_mode)
            if not allowed:
                detail = (
                    "denied by a permission rule"
                    if reason == "deny"
                    else "requires approval, but no interactive client is attached"
                    if reason == "approval-required"
                    else "rejected by the user"
                )
                return ToolResult(
                    False,
                    f"blocked: {action} {resource[:40]}",
                    f"Permission denied: `{action}` on `{resource}` — {detail}. "
                    "Update the `permissions` array in .rbit/config.json to allow it.",
                )
        return None

    def rel_display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    def invalidate_file_cache(self) -> None:
        self._file_cache = None

    def mcp_tool_schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style schemas for every tool exposed by connected MCP servers."""
        if self.mcp is None:
            return []
        try:
            return list(self.mcp.schemas())
        except Exception:
            return []

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
        # Move detection: identical lines removed on one side and added on the
        # other are likely relocations, not separate edits.
        sm = difflib.SequenceMatcher(None, [ln.strip() for ln in old_lines], [ln.strip() for ln in new_lines])
        old_bag: dict[str, int] = {}
        for op in sm.get_opcodes():
            if op[0] == "delete":
                for ln in old_lines[op[1] : op[2]]:
                    old_bag[ln.strip()] = old_bag.get(ln.strip(), 0) + 1
        moved = 0
        for op in sm.get_opcodes():
            if op[0] == "insert":
                for ln in new_lines[op[3] : op[4]]:
                    key = ln.strip()
                    if key and old_bag.get(key):
                        old_bag[key] -= 1
                        moved += 1
        if moved:
            diff_lines.append(f"# ⇄ ~{moved} line(s) moved")
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

    def import_graph(self, limit: int = 200) -> dict[str, set[str]]:
        """Python-only import graph: local file → local modules it imports."""
        import ast as _ast

        graph: dict[str, set[str]] = {}
        py_files = [rel for rel in self.list_workspace_files(limit=limit) if rel.endswith(".py")]
        module_names = {rel[:-3].replace("/", ".").replace("\\", ".") for rel in py_files}
        for rel in py_files:
            full = self.workspace / rel
            try:
                tree = _ast.parse(full.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            imports: set[str] = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    for alias in node.names:
                        if alias.name in module_names:
                            imports.add(alias.name)
                elif isinstance(node, _ast.ImportFrom) and node.module and node.level == 0:
                    if node.module in module_names:
                        imports.add(node.module)
            if imports:
                graph[rel] = imports
        return graph

    def undo_to_count(self, mark: int) -> int:
        """Reverts file snapshots until the undo stack is back to `mark` length."""
        reverted = 0
        while len(self.undo_stack) > max(0, mark):
            self.undo_last()
            reverted += 1
        return reverted

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
        is_mcp = self.mcp is not None and name in getattr(self.mcp, "tool_names", set())
        if self.session_readonly and (
            is_mcp or name in ("write", "edit", "multi_edit", "apply_patch", "notebook", "terminal", "image_generate") or name == "bash"
        ):
            return ToolResult(
                ok=False,
                summary=f"{name} blocked: read-only session",
                output="This session was started with --readonly: no mutating tools are allowed.",
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
        if agent_mode == "plan" and name in ("write", "edit", "multi_edit", "apply_patch"):
            return ToolResult(
                ok=False,
                summary=f"{name} blocked in Plan mode",
                output="Plan mode is read-only. Press Tab to switch to Build mode to modify files.",
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )

        if self.plugins is not None:
            hook_ctx = await self.plugins.trigger(
                "tool.execute.before",
                {"tool": name, "args": args, "agent_mode": agent_mode},
            )
            if hook_ctx.get("deny"):
                reason = str(hook_ctx.get("reason") or "blocked by plugin")
                return ToolResult(
                    ok=False,
                    summary=f"{name} blocked by plugin",
                    output=reason,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            if isinstance(hook_ctx.get("args"), dict):
                args = hook_ctx["args"]

        permission_block = await self._preflight_permissions(name, args, agent_mode)
        if permission_block is not None:
            permission_block.duration_ms = int((time.perf_counter() - t0) * 1000)
            return permission_block

        edit_tools = ("write", "edit", "multi_edit", "apply_patch")
        if self.plugins is not None and name in edit_tools:
            ctx = await self.plugins.trigger("before_edit", {"tool": name, "args": args, "agent_mode": agent_mode})
            if ctx.get("deny"):
                return ToolResult(False, f"{name} blocked by hook", str(ctx.get("reason") or "before_edit hook denied"))
        if self.plugins is not None and name in ("bash", "terminal"):
            await self.plugins.trigger("before_command", {"tool": name, "args": args, "agent_mode": agent_mode})

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
                res = await self._tool_git(args, agent_mode=agent_mode)
            elif name == "diagnostics":
                res = await self._tool_diagnostics(args)
            elif name == "skill":
                res = await asyncio.to_thread(self._tool_skill, args)
            elif name == "artifact":
                res = await asyncio.to_thread(self._tool_artifact, args)
            elif name == "schedule":
                res = await asyncio.to_thread(self._tool_schedule, args)
            elif name == "question":
                res = await self._tool_question(args)
            elif name == "terminal":
                res = await self._tool_terminal(args)
            elif name == "image_generate":
                res = await self._tool_image_generate(args)
            elif name == "mcp_resource":
                res = await self._tool_mcp_resource(args)
            elif name == "risk_review":
                res = await asyncio.to_thread(self._tool_risk_review, args)
            elif name == "read_many_files":
                res = await self._tool_read_many(args)
            elif name == "lsp":
                res = await self._tool_lsp(args, agent_mode=agent_mode)
            elif name == "browser":
                res = await self._tool_browser(args)
            elif name == "lint":
                res = await self._tool_lint(args)
            elif name == "notebook":
                res = self._tool_notebook(args)
            elif name == "list_changes":
                res = self._tool_list_changes(args)
            elif name == "todowrite":
                res = self._tool_todowrite(args)
            elif name == "webfetch":
                res = await self._tool_webfetch(args)
            elif name == "websearch":
                res = await self._tool_websearch(args)
            elif name == "bash":
                res = await self._tool_bash(args, agent_mode=agent_mode)
            elif is_mcp:
                mcp_result = await self.mcp.call(name, args)
                res = ToolResult(mcp_result.ok, mcp_result.summary, mcp_result.output)
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
            if self.plugins is not None:
                await self.plugins.trigger(
                    "on_error", {"tool": name, "args": args, "error": str(exc), "agent_mode": agent_mode}
                )

        res.output = redact_secrets(res.output)
        if res.diff:
            res.diff = redact_secrets(res.diff)
        if res.ok and name in ("write", "edit", "multi_edit", "apply_patch"):
            formatted = await asyncio.to_thread(self._format_mutated_files, name, args)
            if formatted:
                res.output = f"{res.output}\n\n{formatted}".strip()
        if self.plugins is not None and name in edit_tools:
            await self.plugins.trigger(
                "after_edit",
                {"tool": name, "args": args, "result": res, "agent_mode": agent_mode},
            )
        if self.plugins is not None and name in ("bash", "terminal"):
            await self.plugins.trigger(
                "after_command",
                {"tool": name, "args": args, "result": res, "agent_mode": agent_mode},
            )
        if self.plugins is not None:
            hook_ctx = await self.plugins.trigger(
                "tool.execute.after",
                {"tool": name, "args": args, "result": res, "agent_mode": agent_mode},
            )
            if isinstance(hook_ctx.get("result"), ToolResult):
                res = hook_ctx["result"]
            await self.plugins.trigger(
                "event",
                {"type": "tool.result", "tool": name, "ok": res.ok, "agent_mode": agent_mode},
            )
        res.duration_ms = int((time.perf_counter() - t0) * 1000)
        if self.audit is not None:
            self.audit.tool_call(
                name,
                args,
                ok=res.ok,
                summary=res.summary,
                duration_ms=res.duration_ms,
                agent_mode=agent_mode,
            )
        return res

    def _mutation_paths(self, name: str, args: dict[str, Any]) -> list[Path]:
        if name == "apply_patch":
            paths: list[Path] = []
            for line in str(args.get("patch") or "").splitlines():
                if not line.startswith("+++ "):
                    continue
                candidate = line[4:].strip().split("\t", 1)[0].removeprefix("b/")
                if candidate and candidate != "/dev/null":
                    try:
                        paths.append(self.resolve_path(candidate))
                    except PathOutsideWorkspace:
                        continue
            return paths
        raw = args.get("filePath") or args.get("path")
        if not raw:
            return []
        try:
            return [self.resolve_path(str(raw))]
        except PathOutsideWorkspace:
            return []

    def _format_mutated_files(self, name: str, args: dict[str, Any]) -> str:
        config = self.get_config() if self.get_config is not None else None
        if not formatter_enabled(config):
            return ""
        notes: list[str] = []
        for path in self._mutation_paths(name, args):
            if not path.is_file():
                continue
            result = format_file(path, config, self.workspace)
            if not result.ran:
                continue
            rel = self.rel_display(path)
            if result.ok:
                state = "formatted" if result.changed else "already formatted"
                notes.append(f"{result.name}: {rel} ({state})")
            else:
                notes.append(f"{result.name}: {rel} failed (tool output kept unchanged)")
        return "\n".join(notes)

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

        if target.suffix.lower() == ".pdf":
            return self._read_pdf(target, rel, offset, limit)

        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        sliced = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{idx}: {line}" for idx, line in enumerate(sliced, start=offset))
        return ToolResult(True, f"{len(sliced)}/{len(lines)} lines", numbered or "(empty file)")

    @staticmethod
    def _read_pdf(target: Path, rel: str, offset: int, limit: int) -> ToolResult:
        try:
            from pypdf import PdfReader
        except ImportError:
            return ToolResult(
                False,
                f"{rel} (PDF support not installed)",
                "Install the optional PDF dependency: `pip install rbit[pdf]`.",
            )
        try:
            reader = PdfReader(str(target))
            lines: list[str] = []
            for index, page in enumerate(reader.pages, start=1):
                lines.append(f"--- page {index} ---")
                text = page.extract_text() or ""
                lines.extend(text.splitlines())
        except Exception as exc:  # noqa: BLE001 - pypdf raises many parser errors
            return ToolResult(False, f"{rel} (PDF parse failed)", f"{type(exc).__name__}: {exc}")
        sliced = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{idx}: {line}" for idx, line in enumerate(sliced, start=offset))
        return ToolResult(True, f"{len(sliced)}/{len(lines)} PDF lines", numbered or "(empty PDF)")

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
            if not rep_all and updated.count(old_s) > 1:
                return ToolResult(
                    False,
                    f"edit #{idx} ambiguous",
                    f"Found {updated.count(old_s)} matches for edit #{idx} oldString; add context or set replaceAll=true.",
                )
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
                # LLM patches strip the space from empty context lines; treat a
                # blank line inside an unfinished hunk as an empty context line.
                old_so_far = sum(1 for ln in cur if ln[0] in (" ", "-"))
                if old_so_far < old_len:
                    cur.append(" ")
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
        # Keep parity with the fallback listing: search hidden dirs, but never
        # descend into VCS/tooling directories from IGNORED_DIRS.
        cmd.append("--hidden")
        for ignored in sorted(IGNORED_DIRS):
            cmd.extend(["--glob", f"!{ignored}/**"])
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

    async def _tool_git(self, args: dict[str, Any], agent_mode: str = "build") -> ToolResult:
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

        if action == "bisect":
            return await self._git_bisect(args, agent_mode=agent_mode)

        if action == "status":
            cmd = ["status", "--short"]
        elif action == "diff":
            cmd = ["diff"] + (["--staged"] if staged else []) + path_args
        elif action == "log":
            cmd = ["log", "--oneline", f"-{limit}"] + path_args
        elif action == "show":
            cmd = ["show", "--stat", "HEAD"] + path_args
        elif action == "branch":
            cmd = ["rev-parse", "--abbrev-ref", "HEAD"]
        elif action == "blame":
            if not raw_path:
                return ToolResult(False, "blame needs path", "`path` is required for git blame.")
            cmd = ["blame", raw_path]
        else:
            return ToolResult(
                False,
                f"unsupported git action: {action}",
                "Allowed read-only actions: status, diff, log, show, branch, blame. "
                "Bisect: action=`bisect` with `subaction` = status|start|good|bad|skip|reset|log.",
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

    #: Bisect subactions that move HEAD or refs, so they must clear permissions.
    _BISECT_MUTATING = frozenset({"start", "good", "bad", "skip", "reset", "run"})

    async def _git_bisect(self, args: dict[str, Any], agent_mode: str = "build") -> ToolResult:
        """Drives `git bisect` so the agent can find a regression commit.

        `git bisect run` is deliberately *not* accepted: it executes an arbitrary
        shell command repeatedly. Pass good/bad commits instead and let the agent
        verify each checkout with the normal tools, which keeps every command
        visible and permission-checked.
        """
        sub = str(args.get("subaction") or args.get("sub") or "status").strip().lower()
        rev = str(args.get("rev") or "").strip()

        allowed = {"status", "start", "good", "bad", "skip", "reset", "log", "run"}
        if sub not in allowed:
            return ToolResult(
                False,
                f"unsupported bisect subaction: {sub}",
                "Use status, start, good <rev>, bad <rev>, skip, reset or log. "
                "`run` is rejected on purpose because it executes arbitrary commands.",
            )
        if sub == "run":
            return ToolResult(
                False,
                "git bisect run is not allowed",
                "`git bisect run <cmd>` executes an arbitrary command for every step. "
                "Use `good`/`bad` and verify each checkout with the normal tools.",
            )

        if sub in self._BISECT_MUTATING:
            allowed_perm, reason = await self._authorize(
                "shell", f"git bisect {sub} {rev}".strip(), agent_mode
            )
            if not allowed_perm:
                return ToolResult(
                    False,
                    f"blocked: git bisect {sub} ({reason})",
                    f"Permission `{reason}` refused `git bisect {sub}`. Adjust `permissions` "
                    "or the permission mode to allow it.",
                )

        if sub == "status":
            cmd = ["bisect", "log"]
        elif sub == "log":
            cmd = ["bisect", "log"]
        elif sub == "reset":
            cmd = ["bisect", "reset"]
        elif sub == "skip":
            cmd = ["bisect", "skip"]
        elif sub == "start":
            cmd = ["bisect", "start"]
            if rev:
                cmd.append(rev)
        elif sub in ("good", "bad"):
            if not rev:
                return ToolResult(
                    False,
                    f"bisect {sub} needs a revision",
                    f"Pass `rev` (a commit, tag or `HEAD`) for `git bisect {sub}`.",
                )
            cmd = ["bisect", sub, rev]
        else:  # pragma: no cover - guarded above
            return ToolResult(False, "unsupported bisect subaction", sub)

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *cmd,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _err = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except Exception as exc:
            return ToolResult(False, f"git bisect {sub} failed", f"git execution error: {exc}")

        output = out_b.decode("utf-8", errors="replace").strip() or "(no output)"
        ok = proc.returncode == 0
        # Report the checked-out candidate so the agent knows what to test next.
        first_line = next((ln for ln in output.splitlines() if ln.strip()), "")
        summary = f"git bisect {sub}"
        if first_line:
            summary = f"{summary} · {first_line[:70]}"
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

        diag = await self._tool_diagnostics(args)
        note = "ruff/pyflakes not installed â€” ran built-in syntax diagnostics only (pip install ruff)."
        return ToolResult(diag.ok, f"fallback: {diag.summary}", f"{diag.output}\n\n{note}")

    async def _tool_diagnostics(self, args: dict[str, Any]) -> ToolResult:
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

        if self.lsp is not None and int(getattr(self.lsp, "configured_count", 0) or 0) > 0:
            supported = [path for path in targets if self.lsp.definition_for(path) is not None]
            if supported:
                results = await self.lsp.diagnostics_for_many(supported)
                issues = [
                    f"{self.rel_display(diag.path)}:{diag.line}:{diag.column} {diag.severity}: {diag.message}"
                    + (f" ({diag.source})" if diag.source else "")
                    for diags in results.values()
                    for diag in diags
                ]
                checked = len(supported)
                if not issues:
                    return ToolResult(
                        True,
                        f"0 issues ({checked} checked via LSP)",
                        f"No LSP diagnostics in {checked} file(s).",
                    )
                return ToolResult(
                    True,
                    f"{len(issues)} issues ({checked} checked via LSP)",
                    "\n".join(f"- {item}" for item in issues),
                )

        return self.builtin_diagnostics(args, targets)

    def builtin_diagnostics(self, args: dict[str, Any], targets: list[Path] | None = None) -> ToolResult:
        raw = str(args.get("filePath") or args.get("path") or ".").strip()
        target = self.resolve_path(raw)
        if not target.exists():
            rel = self.rel_display(target)
            return ToolResult(False, f"{rel} not found", f"Path not found: {rel}")
        if targets is None:
            targets = [target] if target.is_file() else [
                self.workspace / rel for rel in self.list_workspace_files(limit=300)
            ]
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

    def _tool_risk_review(self, args: dict[str, Any]) -> ToolResult:
        from .risk import analyze_risk, render_markdown

        report = analyze_risk(self.workspace)
        return ToolResult(
            report.level in ("low", "medium"),
            f"risk: {report.level} ({report.score}/100)",
            render_markdown(report),
        )

    async def _tool_read_many(self, args: dict[str, Any]) -> ToolResult:
        paths = args.get("paths") or args.get("files")
        if isinstance(paths, str):
            paths = [item.strip() for item in paths.split(",") if item.strip()]
        if not isinstance(paths, list) or not paths:
            return ToolResult(False, "missing paths", "`paths` must be a non-empty list.")
        limit = max(1, min(200, int(args.get("limit") or 160)))
        blocks: list[str] = []
        missing: list[str] = []
        for raw in paths[:50]:
            raw = str(raw).strip()
            if not raw:
                continue
            try:
                target = self.resolve_path(raw)
            except PathOutsideWorkspace:
                missing.append(raw)
                continue
            if not target.is_file():
                missing.append(raw)
                continue
            res = await asyncio.to_thread(self._tool_read, {"filePath": raw, "limit": limit})
            if res.ok:
                blocks.append(f'<file path="{raw}">\n{res.output}\n</file>')
            else:
                missing.append(raw)
        if not blocks:
            return ToolResult(False, "no files read", "Could not read: " + ", ".join(missing[:20]))
        note = f"\n\nMissing: {', '.join(missing[:20])}" if missing else ""
        return ToolResult(True, f"read {len(blocks)} file(s)", "\n\n".join(blocks) + note)

    async def _tool_lsp(self, args: dict[str, Any], agent_mode: str = "build") -> ToolResult:
        if self.lsp is None or int(getattr(self.lsp, "configured_count", 0) or 0) <= 0:
            return ToolResult(False, "LSP is not configured", "Enable `lsp` in `.rbit/config.json`.")
        action = str(args.get("action") or "definition").lower()
        raw = str(args.get("filePath") or args.get("path") or "")
        if not raw:
            return ToolResult(False, "missing filePath", "`filePath` is required.")
        path = self.resolve_path(raw)
        line = max(1, int(args.get("line") or 1))
        column = max(1, int(args.get("column") or 1))
        if action == "rename" and (self.session_readonly or agent_mode == "plan"):
            return ToolResult(False, "rename blocked", "LSP rename is disabled in read-only/Plan mode.")
        try:
            if action == "definition":
                locations = await self.lsp.definition(path, line, column)
                text = "\n".join(f"- {loc['path']}:{loc['line']}:{loc['column']}" for loc in locations)
                return ToolResult(True, f"{len(locations)} definition(s)", text or "No definition found.")
            if action == "references":
                locations = await self.lsp.references(path, line, column)
                text = "\n".join(f"- {loc['path']}:{loc['line']}:{loc['column']}" for loc in locations)
                return ToolResult(True, f"{len(locations)} reference(s)", text or "No references found.")
            if action == "hover":
                text = await self.lsp.hover(path, line, column)
                return ToolResult(True, "LSP hover", text or "No hover information.")
            if action == "symbols":
                symbols = await self.lsp.symbols(path)
                text = "\n".join(
                    f"- {item['name']} ({item['kind']}) at {path}:{item['line']}:{item['column']}"
                    for item in symbols
                )
                return ToolResult(True, f"{len(symbols)} symbol(s)", text or "No symbols found.")
            if action == "rename":
                new_name = str(args.get("newName") or "").strip()
                if not new_name:
                    return ToolResult(False, "missing newName", "`newName` is required for LSP rename.")
                applied, files = await self.lsp.rename(path, line, column, new_name)
                file_list = ", ".join(str(item) for item in files) or "none"
                return ToolResult(
                    applied > 0,
                    f"renamed in {applied} place(s)",
                    f"Applied {applied} edit(s) in: {file_list}",
                )
        except Exception as exc:  # noqa: BLE001 - language server errors are user-facing
            return ToolResult(False, "LSP request failed", str(exc))
        return ToolResult(
            False,
            f"unknown lsp action: {action}",
            "Use action=definition|references|hover|symbols|rename.",
        )

    async def _tool_browser(self, args: dict[str, Any]) -> ToolResult:
        from .browser import BrowserSession

        if not BrowserSession.available():
            return ToolResult(
                False,
                "browser support not installed",
                "Install the optional dependency: `pip install rbit[browser]`.",
            )
        action = str(args.get("action") or "open").strip().lower()
        if self._browser_session is None:
            self._browser_session = BrowserSession()
        session: BrowserSession = self._browser_session
        try:
            if action == "open":
                result = await asyncio.to_thread(session.open, str(args.get("url") or ""))
            elif action == "click":
                result = await asyncio.to_thread(session.click, str(args.get("selector") or ""))
            elif action == "fill":
                result = await asyncio.to_thread(
                    session.fill, str(args.get("selector") or ""), str(args.get("value") or "")
                )
            elif action in ("content", "snapshot"):
                result = await asyncio.to_thread(session.content)
            elif action == "screenshot":
                result = await asyncio.to_thread(session.screenshot, str(args.get("path") or "screenshot.png"))
            elif action == "close":
                result = await asyncio.to_thread(session.close)
                self._browser_session = None
            else:
                return ToolResult(
                    False,
                    f"unknown browser action: {action}",
                    "Use action=open|click|fill|content|screenshot|close.",
                )
        except Exception as exc:  # noqa: BLE001 - browser errors are user-facing
            return ToolResult(False, "browser failed", f"{type(exc).__name__}: {exc}")
        return ToolResult(result.ok, result.summary, result.output)

    def browser_tool_schema(self) -> dict[str, Any] | None:
        from .browser import BrowserSession

        if not BrowserSession.available():
            return None
        return {
            "type": "function",
            "function": {
                "name": "browser",
                "description": "Automate a headless Chromium browser (open, click, fill, read content, screenshot).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["open", "click", "fill", "content", "screenshot", "close"],
                        },
                        "url": {"type": "string"},
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
        }

    def _tool_skill(self, args: dict[str, Any]) -> ToolResult:
        skills = load_skills(self.workspace)
        if not skills:
            return ToolResult(
                False,
                "no skills installed",
                "No skills found. Add `.rbit/skills/<name>.md` or `~/.config/rbit/skills/<name>/SKILL.md`.",
            )
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult(
                False,
                "missing skill name",
                "Pass `name`. Available: " + ", ".join(sorted(skills)),
            )
        skill = skills.get(name)
        if skill is None:
            return ToolResult(
                False,
                f"unknown skill: {name}",
                "Available skills: " + ", ".join(sorted(skills)),
            )
        body = str(skill.get("body") or "")
        extra = str(args.get("args") or "")
        body = body.replace("{args}", extra).replace("$ARGUMENTS", extra)
        description = skill.get("description") or ""
        header = f"Skill: {name}" + (f" — {description}" if description else "")
        skill_path = Path(str(skill.get("path") or "")).parent
        return ToolResult(True, f"skill {name} loaded", f"{header}\nReference files: {skill_path}\n\n{body}")

    def skill_tool_schema(self) -> dict[str, Any] | None:
        return skill_tool_schema(load_skills(self.workspace))

    def _artifact_dir(self) -> Path:
        return self.storage.state_dir / "artifacts"

    def _artifact_path(self, name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.") or "artifact"
        if not Path(safe).suffix:
            safe += ".md"
        return self._artifact_dir() / safe

    def _tool_artifact(self, args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "create").strip().lower()
        directory = self._artifact_dir()
        if action == "list":
            if not directory.is_dir():
                return ToolResult(True, "0 artifacts", "No artifacts yet.")
            rows = []
            for path in sorted(directory.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
                if path.is_file():
                    rows.append(f"- {path.name} ({path.stat().st_size} bytes)")
            return ToolResult(True, f"{len(rows)} artifact(s)", "\n".join(rows) or "No artifacts yet.")
        name = str(args.get("name") or args.get("filePath") or "").strip()
        if not name:
            return ToolResult(False, "missing name", "`name` is required for artifact create/read/delete.")
        path = self._artifact_path(name)
        if action in ("read", "get"):
            if not path.exists():
                return ToolResult(False, f"artifact not found: {name}", f"No artifact at {path.name}.")
            text = path.read_text(encoding="utf-8", errors="replace")[:60_000]
            return ToolResult(True, f"artifact {path.name} ({len(text)} chars)", text)
        if action in ("delete", "remove"):
            if not path.exists():
                return ToolResult(False, f"artifact not found: {name}", f"No artifact at {path.name}.")
            path.unlink()
            return ToolResult(True, f"artifact deleted: {path.name}", f"Deleted {path.name}.")
        if action in ("create", "write"):
            content = str(args.get("content") or "")
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            rel = self.rel_display(path)
            return ToolResult(True, f"artifact {path.name} ({len(content)} chars)", f"Saved artifact: {rel}")
        return ToolResult(
            False,
            f"unknown artifact action: {action}",
            "Use action=create|read|list|delete.",
        )

    def _tool_schedule(self, args: dict[str, Any]) -> ToolResult:
        from .scheduler import Scheduler

        scheduler = Scheduler(self.workspace)
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            rows = scheduler.describe()
            return ToolResult(True, f"{len(rows)} schedule(s)", "\n".join(rows) or "No schedules.")
        if action in ("create", "add"):
            entry, error = scheduler.add(
                str(args.get("prompt") or ""),
                str(args.get("schedule") or ""),
                name=str(args.get("name") or ""),
                model=str(args.get("model") or ""),
                agent=str(args.get("agent") or "build"),
            )
            if entry is None:
                return ToolResult(False, "schedule not created", error)
            return ToolResult(
                True,
                f"schedule created: {entry['name']}",
                f"Created schedule `{entry['id']}` ({entry['schedule']}).",
            )
        if action in ("remove", "delete"):
            target = str(args.get("id") or args.get("name") or "").strip()
            return ToolResult(
                scheduler.remove(target),
                f"schedule removed: {target}" if target else "missing id/name",
                "Use action=list to see schedule IDs.",
            )
        return ToolResult(False, f"unknown schedule action: {action}", "Use action=create|list|remove.")

    async def _tool_question(self, args: dict[str, Any]) -> ToolResult:
        question = str(args.get("question") or "").strip()
        if not question:
            return ToolResult(False, "missing question", "`question` is required.")
        raw_options = args.get("options")
        if isinstance(raw_options, str):
            options = [part.strip() for part in raw_options.split("|") if part.strip()]
        elif isinstance(raw_options, list):
            options = [str(part).strip() for part in raw_options if str(part).strip()]
        else:
            options = []
        if self.question_cb is None:
            choices = " Options: " + ", ".join(options) if options else ""
            return ToolResult(
                False,
                "question unavailable in non-interactive mode",
                f"Ask the user directly in your reply.{choices}",
            )
        answer = await self.question_cb(question, options)
        if not answer:
            return ToolResult(False, "question cancelled", "The user dismissed the question.")
        return ToolResult(True, "answer received", answer)

    async def _tool_terminal(self, args: dict[str, Any]) -> ToolResult:
        from .terminal import PersistentShell, TerminalError

        action = str(args.get("action") or "run").strip().lower()
        if self._terminal_shell is None:
            self._terminal_shell = PersistentShell(self.workspace)
        shell: PersistentShell = self._terminal_shell
        if action == "close":
            await shell.close()
            return ToolResult(True, "terminal closed", "Persistent terminal closed.")
        if action in ("start", "open"):
            try:
                await shell.start()
            except (OSError, TerminalError) as exc:
                return ToolResult(False, "terminal start failed", str(exc))
            return ToolResult(True, "terminal started", "Persistent terminal started.")
        command = str(args.get("command") or "").strip()
        if not command:
            return ToolResult(False, "missing command", "`command` is required.")
        try:
            output = await shell.run(command)
        except (OSError, TerminalError) as exc:
            return ToolResult(False, "terminal failed", str(exc))
        return ToolResult(True, f"terminal: {len(output)} chars", output or "(no output)")

    async def _tool_image_generate(self, args: dict[str, Any]) -> ToolResult:
        import base64

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(False, "missing prompt", "`prompt` is required.")
        config = self.get_config() if self.get_config is not None else {}
        raw_image = config.get("image")
        image_cfg: dict[str, Any] = raw_image if isinstance(raw_image, dict) else {}
        api_base = str(image_cfg.get("api_base") or config.get("api_base") or "https://api.openai.com/v1").rstrip("/")
        api_base = api_base.removesuffix("/chat/completions")
        api_key = str(image_cfg.get("api_key") or "").strip() or (
            self.storage.resolve_api_key(config) if self.storage else ""
        )
        if not api_key:
            return ToolResult(False, "image_generate: missing API key", "Configure `image.api_key` or the provider key.")
        model = str(image_cfg.get("model") or args.get("model") or "gpt-image-1")
        size = str(args.get("size") or "1024x1024")
        body = {"model": model, "prompt": prompt, "size": size, "n": 1, "response_format": "b64_json"}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            if self.http_client is not None:
                response = await self.http_client.post(f"{api_base}/images/generations", headers=headers, json=body)
            else:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(f"{api_base}/images/generations", headers=headers, json=body)
        except httpx.HTTPError as exc:
            return ToolResult(False, "image_generate failed", str(exc))
        if response.status_code >= 400:
            return ToolResult(False, f"image_generate HTTP {response.status_code}", response.text[:500])
        try:
            data = response.json()["data"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            return ToolResult(False, "image_generate: malformed response", response.text[:300])
        raw: bytes
        if data.get("b64_json"):
            raw = base64.b64decode(data["b64_json"])
        elif data.get("url"):
            url = str(data["url"])
            if self.http_client is not None:
                fetched = await self.http_client.get(url)
            else:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    fetched = await client.get(url)
            if fetched.status_code >= 400:
                return ToolResult(False, "image download failed", f"HTTP {fetched.status_code}")
            raw = fetched.content
        else:
            return ToolResult(False, "image_generate: no image data", "Provider returned neither b64_json nor url.")
        name = str(args.get("name") or "generated").strip()
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "generated"
        if not Path(safe).suffix:
            safe += ".png"
        directory = self.storage.state_dir / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / safe
        path.write_bytes(raw)
        return ToolResult(True, f"image saved: {path.name} ({len(raw) // 1024} KiB)", f"Saved image artifact: {path}")

    async def _tool_mcp_resource(self, args: dict[str, Any]) -> ToolResult:
        if self.mcp is None or not getattr(self.mcp, "clients", {}):
            return ToolResult(False, "no MCP servers", "Configure an MCP server first (`rbit mcp add`).")
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            resources = await self.mcp.list_resources()
            rows = [
                f"{server}: {item.get('uri')} ({item.get('name') or 'resource'})"
                for server, items in resources.items()
                for item in items
            ]
            return ToolResult(True, f"{len(rows)} MCP resource(s)", "\n".join(rows) or "No resources.")
        if action == "read":
            server = str(args.get("server") or "").strip()
            uri = str(args.get("uri") or "").strip()
            if not server or not uri:
                return ToolResult(False, "missing server/uri", "Both `server` and `uri` are required.")
            try:
                result = await self.mcp.read_resource(server, uri)
            except Exception as exc:  # noqa: BLE001 - MCP errors are user-facing
                return ToolResult(False, "MCP resource read failed", str(exc))
            contents = result.get("contents") if isinstance(result, dict) else None
            if isinstance(contents, list):
                text = "\n\n".join(
                    str(item.get("text") or item.get("blob") or "") for item in contents if isinstance(item, dict)
                )
            else:
                text = str(result)
            return ToolResult(True, f"MCP resource: {uri}", text)
        return ToolResult(False, f"unknown mcp_resource action: {action}", "Use action=list|read.")

    def mcp_resource_schema(self) -> dict[str, Any] | None:
        if self.mcp is None or not getattr(self.mcp, "clients", {}):
            return None
        return {
            "type": "function",
            "function": {
                "name": "mcp_resource",
                "description": "List or read MCP resources from configured servers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "read"]},
                        "server": {"type": "string"},
                        "uri": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
        }

    def _tool_notebook(self, args: dict[str, Any]) -> ToolResult:
        """Cell-level access to .ipynb files: read / set_cell / insert_cell / delete_cell."""
        raw = args.get("filePath") or args.get("path")
        action = str(args.get("action", "read")).lower()
        if not raw:
            return ToolResult(False, "missing filePath", "filePath is required.")
        target = self.resolve_path(str(raw))
        rel = self.rel_display(target)
        if not target.exists():
            return ToolResult(False, f"{rel} not found", f"File does not exist: {rel}")
        try:
            nb = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return ToolResult(False, f"{rel} invalid", f"Cannot parse notebook: {exc}")
        cells = nb.get("cells")
        if not isinstance(cells, list):
            return ToolResult(False, f"{rel} invalid", "Notebook has no `cells` list.")

        def cell_source(cell: dict[str, Any]) -> str:
            src = cell.get("source") or []
            return src if isinstance(src, str) else "".join(src)

        if action == "read":
            lines: list[str] = []
            for i, cell in enumerate(cells):
                ctype = cell.get("cell_type", "?")
                source = cell_source(cell)
                outputs = cell.get("outputs") or []
                out_note = f" · {len(outputs)} output(s)" if outputs else ""
                lines.append(f"[{i}] {ctype}{out_note}")
                src_lines = source.splitlines()
                for src_line in src_lines[:40]:
                    lines.append(f"    {src_line}")
                if len(src_lines) > 40:
                    lines.append(f"    … ({len(src_lines) - 40} more lines)")
            return ToolResult(True, f"{len(cells)} cells", "\n".join(lines) or "(empty notebook)")

        try:
            index = int(args.get("index", -1))
        except (TypeError, ValueError):
            index = -1

        def save_nb(new_content: str, label: str) -> None:
            self.invalidate_file_cache()
            self.undo_stack.append(
                FileSnapshot(
                    step_label=label,
                    path=str(target),
                    existed_before=True,
                    previous_content=original,
                    new_content=new_content,
                )
            )
            self._record_file_change(rel, 0, 0)

        if action in ("set_cell", "insert_cell"):
            source = str(args.get("source", ""))
            if not source:
                return ToolResult(False, "missing source", "`source` is required for this action.")
            if action == "set_cell" and not (0 <= index < len(cells)):
                return ToolResult(False, "bad index", f"Cell index {index} out of range (0..{len(cells) - 1}).")
            original = target.read_text(encoding="utf-8", errors="replace")
            if action == "set_cell":
                cells[index]["source"] = source.splitlines(keepends=True)
                cells[index]["outputs"] = []
                cells[index]["execution_count"] = None
                updated = json.dumps(nb, ensure_ascii=False, indent=1)
                atomic_write_text(target, updated)
                save_nb(updated, f"notebook set_cell {rel}#{index}")
                return ToolResult(True, f"cell #{index} replaced", f"Cell #{index} of {rel} updated.")
            if index < 0 or index > len(cells):
                index = len(cells)
            new_cell: dict[str, Any] = {
                "cell_type": str(args.get("cellType") or "code"),
                "metadata": {},
                "source": source.splitlines(keepends=True),
                "outputs": [],
                "execution_count": None,
            }
            cells.insert(index, new_cell)
            updated = json.dumps(nb, ensure_ascii=False, indent=1)
            atomic_write_text(target, updated)
            save_nb(updated, f"notebook insert {rel}#{index}")
            return ToolResult(True, f"cell inserted at #{index}", f"Cell inserted into {rel} at #{index}.")
        if action == "delete_cell":
            if not (0 <= index < len(cells)):
                return ToolResult(False, "bad index", f"Cell index {index} out of range (0..{len(cells) - 1}).")
            original = target.read_text(encoding="utf-8", errors="replace")
            cells.pop(index)
            updated = json.dumps(nb, ensure_ascii=False, indent=1)
            atomic_write_text(target, updated)
            save_nb(updated, f"notebook delete {rel}#{index}")
            return ToolResult(True, f"cell #{index} deleted", f"Cell #{index} removed from {rel}.")
        return ToolResult(False, "unknown action", "action must be read | set_cell | insert_cell | delete_cell.")

    def _tool_list_changes(self, args: dict[str, Any]) -> ToolResult:
        """Summary of the files this session has modified (for the agent's own awareness)."""
        files = self.session.modified_files
        if not files:
            return ToolResult(True, "no changes", "No files modified in this session yet.")
        lines = ["Files modified in this session:"]
        for rel, st in list(files.items())[-30:]:
            lines.append(f"  {rel}  [+{st.added} -{st.removed}]")
        if self.undo_stack:
            lines.append(f"\nLast change: {self.undo_stack[-1].step_label}")
        return ToolResult(True, f"{len(files)} file(s) modified", "\n".join(lines))

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

    def _fence_untrusted(self, text: str, source: str) -> str:
        """Fences externally-sourced text and records a finding when suspicious.

        Web pages, search results and MCP payloads are the main vector for
        indirect prompt injection: the text is data the model must read, not
        instructions it should follow. The content itself is never rewritten —
        only fenced, so a report about what a page said stays accurate.
        """
        cfg = self.get_config() if self.get_config is not None else {}
        fenced, findings = guard_untrusted(text, source, cfg)
        if findings and self.audit is not None:
            self.audit.record(
                "injection",
                source=source,
                level=risk_level(findings),
                findings=[f.describe() for f in findings][:6],
            )
        return fenced

    async def _tool_webfetch(self, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(False, "invalid url", "URL must start with http:// or https://")
        cached = self._webfetch_cache.get(url)
        if cached and time.time() - cached[0] < 600:
            return ToolResult(
                True,
                f"cached · Markdown {len(cached[1])} chars",
                self._fence_untrusted(cached[1], f"webfetch:{url}"),
            )
        if self.http_client is not None:
            resp = await self.http_client.get(url, headers={"User-Agent": "rbit-cli/1.2"}, follow_redirects=True)
        else:
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
            self._fence_untrusted(cleaned, f"webfetch:{url}"),
        )

    async def _tool_websearch(self, args: dict[str, Any]) -> ToolResult:
        config = self.get_config() if self.get_config is not None else {}
        setting = config.get("websearch")
        if setting is False:
            return ToolResult(False, "websearch disabled", "Enable `websearch` in `.rbit/config.json`.")
        query = str(args.get("query") or args.get("q") or "").strip()
        if not query:
            return ToolResult(False, "empty query", "`query` is required.")
        try:
            limit = int(args.get("num_results") or 6)
        except (TypeError, ValueError):
            limit = 6
        limit = max(1, min(10, limit))
        provider = ""
        api_key = ""
        if isinstance(setting, dict):
            provider = str(setting.get("provider") or "").lower()
            api_key = str(setting.get("api_key") or "")
        provider = provider or "duckduckgo"
        cache_key = f"{provider}::{query.lower()}::{limit}"
        cached = self._websearch_cache.get(cache_key)
        if cached and time.time() - cached[0] < 300:
            return ToolResult(True, f"cached websearch: {limit} result(s)", cached[1])
        errors: list[str] = []
        items: list[dict[str, str]] = []
        if provider in ("duckduckgo", "ddg", "auto"):
            items, errors = await self._search_duckduckgo(query, limit)
        elif provider in ("tavily", "exa", "firecrawl"):
            key = api_key or os.environ.get(f"{provider.upper()}_API_KEY", "")
            if not key:
                errors.append(f"{provider}: api_key is missing")
            else:
                try:
                    items = await self._search_api(provider, query, limit, key)
                except httpx.HTTPError as exc:
                    errors.append(f"{provider}: {exc}")
        else:
            errors.append(f"unknown websearch provider: {provider}")
        if items:
            text = self._format_search_results(items, limit)
            self._websearch_cache[cache_key] = (time.time(), text)
            return ToolResult(
                True,
                f"{min(len(items), limit)} result(s) for: {query}",
                self._fence_untrusted(text, f"websearch:{query}"),
            )
        if provider not in ("duckduckgo", "ddg", "auto"):
            items, fallback_errors = await self._search_duckduckgo(query, limit)
            if items:
                text = self._format_search_results(items, limit)
                self._websearch_cache[cache_key] = (time.time(), text)
                return ToolResult(
                    True,
                    f"{min(len(items), limit)} result(s) for: {query} (DDG fallback)",
                    self._fence_untrusted(text, f"websearch:{query}"),
                )
            errors.extend(fallback_errors)
        return ToolResult(
            False,
            f"websearch failed for: {query}",
            "No results. " + ("; ".join(errors[-3:]) if errors else "Try a different query."),
        )

    @staticmethod
    def _format_search_results(items: list[dict[str, str]], limit: int) -> str:
        lines: list[str] = []
        seen: set[str] = set()
        for item in items:
            target = str(item.get("url") or "")
            if not target.startswith(("http://", "https://")) or target in seen:
                continue
            seen.add(target)
            title = item.get("title") or target
            snippet = str(item.get("snippet") or "").strip()
            lines.append(f"{len(lines) + 1}. {title}\n   {target}\n   {snippet}".rstrip())
            if len(lines) >= limit:
                break
        return "\n\n".join(lines)

    async def _search_api(self, provider: str, query: str, limit: int, key: str) -> list[dict[str, str]]:
        if provider == "tavily":
            url = "https://api.tavily.com/search"
            headers = {"Content-Type": "application/json"}
            body = {"api_key": key, "query": query, "max_results": limit, "search_depth": "basic"}
        elif provider == "exa":
            url = "https://api.exa.ai/search"
            headers = {"Content-Type": "application/json", "x-api-key": key}
            body = {"query": query, "numResults": limit, "contents": {"text": True}}
        else:
            url = "https://api.firecrawl.dev/v1/search"
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
            body = {"query": query, "limit": limit}
        if self.http_client is not None:
            response = await self.http_client.post(url, headers=headers, json=body)
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(url, headers=headers, json=body)
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{provider} HTTP {response.status_code}: {response.text[:160]}",
                request=response.request,
                response=response,
            )
        data = response.json()
        rows = data.get("results") if isinstance(data, dict) else None
        if rows is None and isinstance(data, dict):
            rows = data.get("data")
        results: list[dict[str, str]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            results.append(
                {
                    "title": str(row.get("title") or row.get("name") or ""),
                    "url": str(row.get("url") or row.get("link") or ""),
                    "snippet": str(row.get("content") or row.get("text") or row.get("description") or row.get("markdown") or ""),
                }
            )
        return results

    async def _search_duckduckgo(self, query: str, limit: int) -> tuple[list[dict[str, str]], list[str]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            )
        }
        attempts = [
            ("https://html.duckduckgo.com/html/", {"q": query}),
            ("https://lite.duckduckgo.com/lite/", {"q": query}),
        ]
        errors: list[str] = []
        for url, params in attempts:
            try:
                if self.http_client is not None:
                    resp = await self.http_client.get(url, params=params, headers=headers, follow_redirects=True)
                else:
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                        resp = await client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {exc}")
                continue
            if resp.status_code >= 400:
                errors.append(f"{url}: HTTP {resp.status_code}")
                continue
            parser = _SearchResultsParser()
            parser.feed(resp.text)
            items = [
                {
                    "title": item.get("title", ""),
                    "url": _clean_search_url(item.get("url", "")),
                    "snippet": item.get("snippet", ""),
                }
                for item in parser.results
            ]
            if any(str(item.get("url", "")).startswith(("http://", "https://")) for item in items):
                return items, errors
            errors.append(f"{url}: no results parsed")
        return [], errors

    def _sandbox_violation(self, command: str) -> str | None:
        """Best-effort path sandbox: block workspace escapes in bash commands.

        Best effort by design - it is not an OS-level sandbox; it stops the
        common absolute-path and ``..`` escapes."""
        if self.get_config is None or not self.get_config().get("bash_sandbox", False):
            return None
        for match in re.finditer(r"[A-Za-z]:[/\\][^\s\"'|;&]*", command):
            try:
                candidate = Path(match.group(0)).resolve()
                candidate.relative_to(self.workspace)
            except (ValueError, OSError):
                return match.group(0)
        if re.search(r"(?:^|[\s;|&=])\.\.[/\\]", command):
            return ".."
        return None

    def _prefix_allowed(self, command: str) -> bool:
        """True when the command starts with a user-approved prefix."""
        lowered = command.lower()
        return any(lowered.startswith(p.lower()) for p in self.allowed_bash_prefixes)

    def _bash_prefix(self, command: str) -> str:
        return " ".join(command.split()[:2])

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

        sandbox_violation = self._sandbox_violation(command)
        if sandbox_violation:
            return ToolResult(
                False,
                f"blocked by sandbox: {sandbox_violation[:40]}",
                "Sandbox mode (config `bash_sandbox`) restricts commands to the workspace: "
                "absolute paths outside it and `..` escapes are not allowed.",
            )

        is_readonly = is_readonly_bash_command(command)
        if agent_mode == "plan" and not is_readonly:
            return ToolResult(
                False,
                f"blocked in Plan mode: {command[:36]}",
                "Only read-only commands are allowed in Plan mode.",
            )

        if (
            self.resolve_permission_effect("shell", command, agent_mode) is None
            and not is_readonly
            and not self.always_allow_bash
            and not self._prefix_allowed(command)
            and not self.mode_allows("shell")
            and self.permission_cb is not None
        ):
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
            "description": (
                "Git helper: status, diff, log, show, blame (read-only), plus a "
                "guarded bisect (status/start/good/bad/skip/reset/log) to find the "
                "commit that introduced a regression. `bisect run` is refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "diff", "log", "show", "blame", "bisect"],
                    },
                    "subaction": {
                        "type": "string",
                        "enum": ["status", "start", "good", "bad", "skip", "reset", "log"],
                        "description": "Required when action is bisect.",
                    },
                    "rev": {
                        "type": "string",
                        "description": "Revision for bisect start/good/bad (commit, tag or HEAD).",
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
            "description": (
                "Delegate a goal to a fresh-context subagent and get back a compact summary. "
                "Built-in agents: explore (read-only). Custom agents come from `.rbit/agents/*.md`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "agent": {"type": "string", "description": "Subagent name (default: explore)."},
                    "background": {"type": "boolean", "description": "Run without blocking the current turn."}
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "websearch",
            "description": "Search the web (DuckDuckGo) and return titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "num_results": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_generate",
            "description": "Generate an image from a prompt and save it under .rbit/artifacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "size": {"type": "string"},
                    "name": {"type": "string"},
                    "model": {"type": "string"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "Run commands in a persistent shell session (start/run/close).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["run", "start", "close"]},
                    "command": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "question",
            "description": "Ask the user a question, optionally with multiple-choice options.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule",
            "description": "Create, list, or remove scheduled prompts (every 30m, daily 09:30, or ISO datetime).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "remove"]},
                    "prompt": {"type": "string"},
                    "schedule": {"type": "string"},
                    "name": {"type": "string"},
                    "id": {"type": "string"},
                    "model": {"type": "string"},
                    "agent": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_many_files",
            "description": "Read several files in one tool call (batch context loading).",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp",
            "description": "Ask LSP for definitions, references, hover info, document symbols, or rename a symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["definition", "references", "hover", "symbols", "rename"]},
                    "filePath": {"type": "string"},
                    "line": {"type": "integer"},
                    "column": {"type": "integer"},
                    "newName": {"type": "string"},
                },
                "required": ["action", "filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "risk_review",
            "description": "Score the current git diff for risky patterns (secrets, destructive changes, auth/CI, migrations).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "artifact",
            "description": "Create, read, list, or delete reusable artifacts stored in .rbit/artifacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "read", "list", "delete"]},
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["action"],
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

OPENAI_TOOLS_SCHEMA.append(
    {
        "type": "function",
        "function": {
            "name": "list_changes",
            "description": "Summary of the files modified in this session (paths and +/- line stats), for self-awareness.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
)
OPENAI_TOOLS_SCHEMA.append(
    {
        "type": "function",
        "function": {
            "name": "notebook",
            "description": "Read or edit a Jupyter notebook (.ipynb) cell-by-cell without corrupting JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "action": {"type": "string", "enum": ["read", "set_cell", "insert_cell", "delete_cell"]},
                    "index": {"type": "integer", "description": "Cell index (0-based)."},
                    "source": {"type": "string", "description": "New cell source (set_cell/insert_cell)."},
                    "cellType": {"type": "string", "enum": ["code", "markdown"]},
                },
                "required": ["filePath", "action"],
            },
        },
    }
)


def detect_test_command(workspace: Path) -> str | None:
    """Picks the project's test command: pytest / npm test / cargo test / make test."""
    ws = Path(workspace)
    if (ws / "pyproject.toml").exists() or (ws / "pytest.ini").exists() or any(ws.glob("test_*.py")):
        return "pytest -q"
    pkg = ws / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {})
            if "test" in scripts:
                return "npm test"
        except ValueError:
            pass
    if (ws / "Cargo.toml").exists():
        return "cargo test"
    if (ws / "Makefile").exists():
        return "make test"
    if any(ws.glob("test_*.py")):
        return "pytest -q"
    return None


def detect_language_servers() -> dict[str, bool]:
    """Detects language servers on PATH for the sidebar/doctor LSP status."""
    import shutil as _shutil

    return {
        "pylsp": _shutil.which("pylsp") is not None,
        "pyright": _shutil.which("pyright") is not None or _shutil.which("pyright-langserver") is not None,
        "typescript-language-server": _shutil.which("typescript-language-server") is not None,
        "clangd": _shutil.which("clangd") is not None,
    }
