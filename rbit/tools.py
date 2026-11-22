"""Agent tools for rbit: read, write, edit, multi_edit, glob, grep, bash, todowrite, webfetch + Checkpoints."""

from __future__ import annotations

import ast
import asyncio
import codecs
import difflib
import fnmatch
import html
import ipaddress
import json
import locale
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
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

from .formatters import build_formatters, format_file, formatter_enabled, formatter_is_custom
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

logger = logging.getLogger(__name__)

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
)

READONLY_TOOLS: frozenset[str] = frozenset(
    {"read", "glob", "grep", "webfetch", "websearch", "diagnostics", "lint", "symbols", "git", "list_changes", "skill", "risk_review", "read_many_files", "lsp"}
)

MUTATING_TOOLS: frozenset[str] = frozenset({"write", "edit", "multi_edit", "apply_patch"})

_STATEMENT_SPLIT = re.compile(r"(?:;|\|\||&&|\||&|\r?\n)")

#: Shell constructs that can smuggle a second command or write a file.
_SHELL_UNSAFE_CHARS = re.compile(r"[;&|<>`\r\n]|\$\(|\$\{")

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_WRAPPER_COMMANDS = frozenset({"sudo", "doas", "env", "command", "exec", "nohup", "time", "nice", "builtin", "xargs"})


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


def split_shell_statements(command: str) -> list[str]:
    """Splits a command line into statements on `;`, `&&`, `||`, `|`, `&` and newlines."""
    return [part.strip() for part in _STATEMENT_SPLIT.split(command) if part.strip()]


def normalize_shell_statement(statement: str) -> str:
    """Collapses whitespace and strips `sudo`/`env`/`command`/`FOO=1` style wrappers.

    The result is what a deny rule or danger check should look at, so that
    `sudo  rm   -rf x` and `FOO=1 git  push` cannot dodge a `rm *`/`git push *` rule."""
    tokens = statement.replace("\t", " ").strip().lstrip("({").split()
    while tokens:
        head = tokens[0]
        if _ENV_ASSIGNMENT.match(head):
            tokens.pop(0)
            continue
        if head.lower() in _WRAPPER_COMMANDS:
            tokens.pop(0)
            while tokens and (tokens[0].startswith("-") or _ENV_ASSIGNMENT.match(tokens[0])):
                tokens.pop(0)
            continue
        break
    return " ".join(tokens)


def _readonly_statement_ok(tokens: list[str]) -> bool:
    """True when one statement (already split into tokens) is a whitelisted read-only command."""
    lowered = [t.lower() for t in tokens]
    matched = False
    for prefix in READONLY_BASH_PREFIXES:
        parts = prefix.lower().split()
        if lowered[: len(parts)] == parts:
            matched = True
            break
    if not matched:
        return False
    if lowered[0] == "git" and len(lowered) > 1:
        sub = lowered[1]
        rest = lowered[2:]
        if any(t.startswith("--output") for t in rest):
            return False
        if sub == "branch":
            list_flags = {"--list", "-l", "--contains", "--merged", "--no-merged"}
            plain_flags = {"-a", "-r", "-v", "-vv", "--all", "--remote", "--show-current", "--no-color", "--color"}
            listing = any(t in list_flags for t in rest)
            for token in rest:
                if token.startswith("-"):
                    if token not in list_flags and token not in plain_flags:
                        return False
                elif not listing:
                    return False
    return True


def is_readonly_bash_command(command: str) -> bool:
    """A command is read-only only if EVERY statement is a whitelisted command
    (matched on token boundaries) and the line has no redirects, command
    substitution, backticks or process substitution. `echo x; rm y`, `ls > f`,
    `ls & rm y` and `ls $(rm y)` are mutations even though the first word looks
    harmless."""
    if re.search(r"[<>`]|\$\(|\$\{", command):
        return False
    statements = split_shell_statements(command)
    if not statements:
        return False
    for statement in statements:
        tokens = statement.split()
        if not tokens or not _readonly_statement_ok(tokens):
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



class FileEncodingError(ValueError):
    """Text cannot be written back in the encoding the file already uses."""


def _utf8_validity(raw: bytes) -> tuple[int, int]:
    """(characters decoded from valid multi-byte UTF-8, invalid byte sequences)."""
    decoded = raw.decode("utf-8", errors="replace")
    bad = decoded.count("�")
    good = sum(1 for ch in decoded if ch > "\x7f" and ch != "�")
    return good, bad


def _legacy_encoding(raw: bytes) -> str:
    """Best guess for a single-byte code page: cp1251 for Cyrillic text, else the locale's."""
    letters = [ch for ch in raw.decode("cp1251", errors="replace") if ch.isalpha()]
    if letters and sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ") / len(letters) > 0.3:
        return "cp1251"
    preferred = (locale.getpreferredencoding(False) or "").lower()
    if preferred.startswith("cp125") and preferred != "cp1251":
        try:
            codecs.lookup(preferred)
            return preferred
        except LookupError:
            pass
    return "cp1252"


def detect_file_encoding(raw: bytes) -> str:
    """Name of the codec that reads `raw` as text and writes it back byte for byte.

    UTF-8 (also with a BOM, and with a few stray bytes) stays "utf-8"; UTF-16/32 with a
    BOM keep their byte order; anything else is a legacy single-byte code page.

    A wrong legacy guess cannot corrupt text the model does not touch: decoding and
    encoding with the same codec is the identity, and bytes the codec has no character
    for survive as escapes. Only characters the model types are encoded, and those
    raise FileEncodingError if the code page cannot represent them.
    """
    if raw.startswith(codecs.BOM_UTF32_LE):
        return "utf-32-le"
    if raw.startswith(codecs.BOM_UTF32_BE):
        return "utf-32-be"
    if raw.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le"
    if raw.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be"
    if not raw or raw.isascii():
        return "utf-8"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    if b"\x00" in raw:  # binary, or a wide encoding without a BOM: keep the byte-exact behaviour
        return "utf-8"
    good, bad = _utf8_validity(raw)
    if good * 3 >= bad:  # mostly UTF-8 with a few stray bytes
        return "utf-8"
    return _legacy_encoding(raw)


def decode_file_bytes(raw: bytes) -> tuple[str, str]:
    """Returns (text, encoding). Undecodable bytes become lone surrogates and round-trip."""
    encoding = detect_file_encoding(raw)
    return raw.decode(encoding, errors="surrogateescape"), encoding


def read_text_for_display(path: Path) -> str:
    """File text for showing to the model: right encoding, U+FFFD for undecodable bytes."""
    raw = path.read_bytes()
    return raw.decode(detect_file_encoding(raw), errors="replace")


def encode_file_text(text: str, encoding: str, label: str = "file") -> bytes:
    """Encodes `text` in `encoding`, or raises FileEncodingError naming the characters it lacks."""
    try:
        return text.encode(encoding, errors="surrogateescape")
    except UnicodeEncodeError:
        missing: list[str] = []
        for ch in dict.fromkeys(text):
            try:
                ch.encode(encoding, errors="surrogateescape")
            except UnicodeEncodeError:
                missing.append(ch)
        shown = ", ".join(repr(ch) for ch in missing[:6]) + (" ..." if len(missing) > 6 else "")
        raise FileEncodingError(
            f"`{label}` is encoded as {encoding}, which cannot represent {shown}. "
            "Use characters that exist in that encoding, or convert the file to UTF-8 first."
        ) from None


def atomic_write_text(path: Path, content: str) -> None:
    """Writes text via a temp file + os.replace so a crash never truncates the target.

    Keeps the target's existing encoding (cp1251, UTF-16 with BOM, ...) and preserves its
    dominant line ending: without this, Windows text mode turns every `\\n` into
    `\\r\\n`, so editing an LF file rewrote every line (and vice versa), exploding git
    diffs and breaking follow-up edits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        original = path.read_bytes()
    except OSError:
        original = b""
    encoding = detect_file_encoding(original) if original else "utf-8"
    if encoding.startswith(("utf-16", "utf-32")):
        # Not ASCII-compatible: adapt the line endings on the text, then encode.
        original_text = original.decode(encoding, errors="surrogateescape")
        if (
            "\r\n" in original_text
            and "\r\n" not in content
            and original_text.count("\n") == original_text.count("\r\n")
        ):
            content = content.replace("\n", "\r\n")
        atomic_write_bytes(path, encode_file_text(content, encoding, path.name), mode_from=path)
        return
    # surrogateescape round-trips the bytes of a file that was read the same way,
    # instead of baking U+FFFD into the user's file.
    data = encode_file_text(content, encoding, path.name)
    if b"\r\n" not in data and b"\r\n" in original:
        crlf = original.count(b"\r\n")
        bare_lf = original.count(b"\n") - crlf
        if bare_lf == 0:
            data = data.replace(b"\n", b"\r\n")
        else:
            data = _merge_mixed_line_endings(original, data)
    atomic_write_bytes(path, data, mode_from=path)


def _merge_mixed_line_endings(original: bytes, data: bytes) -> bytes:
    """Keeps each untouched line's own ending in a file that mixes CRLF and LF."""
    old_lines = original.splitlines(keepends=True)
    new_lines = data.splitlines(keepends=True)
    crlf = original.count(b"\r\n")
    dominant = b"\r\n" if crlf * 2 > original.count(b"\n") else b"\n"
    if len(old_lines) > 20000 or len(new_lines) > 20000:
        return data.replace(b"\n", dominant) if dominant == b"\r\n" else data
    old_plain = [ln.rstrip(b"\r\n") for ln in old_lines]
    new_plain = [ln.rstrip(b"\r\n") for ln in new_lines]
    out: list[bytes] = []
    matcher = difflib.SequenceMatcher(None, old_plain, new_plain, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(old_lines[i1:i2])
            continue
        for ln in new_lines[j1:j2]:
            body = ln.rstrip(b"\r\n")
            ending = ln[len(body):]
            out.append(body + (dominant if ending else b""))
    return b"".join(out)


def atomic_write_bytes(path: Path, data: bytes, mode_from: Path | None = None) -> None:
    """Writes raw bytes via a temp file + os.replace, keeping the target's permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    try:
        Path(tmp_name).write_bytes(data)
        source = mode_from if mode_from is not None else path
        if source.exists():
            try:
                shutil.copymode(source, tmp_name)
            except OSError:
                pass
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
    re.compile(r"\bdel\s+/[sqf]{1,3}\b", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b[^\n]*-Recurse[^\n]*(?:[A-Za-z]:\\|/)", re.IGNORECASE),
)

#: Commands that are only dangerous when they *are* the command of a statement
#: (so `grep shutdown log` and `npm run shutdown` stay allowed).
_POWER_COMMANDS = frozenset(
    {"shutdown", "shutdown.exe", "reboot", "halt", "poweroff", "diskpart", "diskpart.exe", "stop-computer", "restart-computer"}
)
_ROOT_TARGET = re.compile(
    r"^(?:/\*?|~/?\*?|\$home/?\*?|\$\{home\}/?\*?|\$env:(?:userprofile|homepath|systemdrive)\\?\*?|[a-z]:[\\/]*\*?)$",
    re.IGNORECASE,
)


def _clean_token(token: str) -> str:
    return token.strip("\"'")


def _is_root_target(token: str) -> bool:
    return bool(_ROOT_TARGET.match(_clean_token(token)))


def _statement_danger(statement: str) -> str:
    tokens = statement.split()
    if not tokens:
        return ""
    head = _clean_token(tokens[0]).lower()
    args = tokens[1:]
    lowered = [a.lower() for a in args]
    if head in _POWER_COMMANDS:
        return statement
    if head in ("rm", "rm.exe", "rmdir", "rd", "del", "erase", "remove-item", "ri"):
        flags = [a for a in lowered if a.startswith("-") or (a.startswith("/") and len(a) <= 3)]
        short = "".join(f.lstrip("-/") for f in flags if not f.startswith("--"))
        recursive = (
            "--recursive" in lowered
            or "r" in short
            or "s" in short and head in ("rmdir", "rd", "del", "erase")
            or any(f.startswith("-rec") for f in lowered)
        )
        if "--no-preserve-root" in lowered:
            return statement
        if recursive and any(_is_root_target(a) for a in args if not a.startswith("-")):
            return statement
        return ""
    if head == "git":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-c", "-C") else rest[1:]
        if rest and rest[0].lower() == "push":
            for arg in rest[1:]:
                low = arg.lower()
                if low.startswith("--force") or low == "--mirror":
                    return statement
                if re.fullmatch(r"-[a-z]+", low) and "f" in low:
                    return statement
                if arg.startswith("+") and len(arg) > 1:
                    return statement
        return ""
    if head == "dd":
        if any(a.lower().startswith("of=/dev/") for a in args):
            return statement
        return ""
    if head.startswith("mkfs"):
        return statement
    if head == "format" and args and re.match(r"^[a-z]:", _clean_token(args[0]), re.IGNORECASE):
        return statement
    return ""


def is_dangerous_command(command: str) -> str:
    """Returns the matched deny-pattern when `command` is catastrophic, else an empty string."""
    for pattern in DANGEROUS_BASH_PATTERNS:
        match = pattern.search(command)
        if match:
            return match.group(0)
    for statement in split_shell_statements(command):
        found = _statement_danger(normalize_shell_statement(statement))
        if found:
            return found
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
        ".git-credentials",
        ".pgpass",
        ".htpasswd",
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
    # `.env::$DATA` (NTFS alternate data stream), `.env.` and `.env ` all open the
    # real `.env` on Windows, so compare the canonical name.
    name = path.name.split(":", 1)[0].rstrip(" .").lower()
    if not name:
        return False
    if name.endswith(SAFE_TEMPLATE_SUFFIXES):
        return False
    if name in SECRET_EXACT_NAMES or name.startswith(".env."):
        return True
    if Path(name).suffix in SECRET_EXTENSIONS:
        return True
    if name.endswith((".tfstate", ".tfstate.backup")) or ".tfstate." in name:
        return True
    parent = path.parent.name.split(":", 1)[0].rstrip(" .").lower()
    if parent == ".aws" and name in ("credentials", "config"):
        return True
    if parent == ".docker" and name == "config.json":
        return True
    in_rbit = any(part.lower() == ".rbit" for part in path.parts[:-1])
    if in_rbit and ((name.startswith("config") and name.endswith(".json")) or ".backup-" in name or name.startswith("config.json")):
        return True
    return False


_SECRET_KEY_NAMES = r"(?:[A-Z0-9_]*_)?(?:API_KEY|SECRET_KEY|ACCESS_TOKEN|AUTH_TOKEN|PRIVATE_KEY|CLIENT_SECRET|PASSWORD)"

#: Source-code-safe variants of the generic `KEY=value` rule: only literal values
#: are redacted, so `PASSWORD = load_password()` stays intact.
_CODE_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(\b" + _SECRET_KEY_NAMES + r"\s*[:=]\s*)(['\"])[^'\"\s]{8,}\2"),
        r"\1\2[REDACTED]\2",
    ),
    (
        re.compile(
            r"(\b" + _SECRET_KEY_NAMES + r"=)(?=[A-Za-z0-9_\-./+=]*\d)[A-Za-z0-9_\-./+=]{12,}(?![\w(\[])"
        ),
        r"\1[REDACTED]",
    ),
)


def redact_secrets(text: str, *, code: bool = False) -> str:
    """Redacts API keys, OAuth tokens, PEM private keys, and env secrets from text.

    With `code=True` (file contents shown to the model) the unquoted `KEY = value`
    rule is limited to literals, so identifiers and calls in source are not mangled."""
    if not text:
        return text
    out = text
    patterns = _SECRET_PATTERNS[:-1] + _CODE_SECRET_PATTERNS if code else _SECRET_PATTERNS
    for pat, repl in patterns:
        out = pat.sub(repl, out)
    return out


def _strip_blocks(text: str) -> str:
    """Removes <script>/<style>/... blocks in linear time (a lazy `.*?` regex is quadratic on unclosed tags)."""
    lowered = text.lower()
    out: list[str] = []
    pos = 0
    opener = re.compile(r"<(script|style|noscript|nav|header|footer|aside)\b[^>]{0,2000}>", re.IGNORECASE)
    while True:
        match = opener.search(text, pos)
        if not match:
            out.append(text[pos:])
            break
        out.append(text[pos : match.start()])
        close = lowered.find(f"</{match.group(1).lower()}>", match.end())
        if close == -1:
            out.append(" ")
            pos = match.end()
            continue
        out.append(" ")
        pos = close + len(match.group(1)) + 3
    return "".join(out)


def html_to_markdown(raw: str) -> str:
    """Converts HTML to a compact readable Markdown outline (headings, links, lists)."""
    text = _strip_blocks(raw[:MAX_HTML_CHARS])

    def _heading(match: re.Match[str]) -> str:
        level = int(match.group(1))
        body = re.sub(r"<[^>]+>", "", match.group(2))
        body = html.unescape(re.sub(r"\s+", " ", body)).strip()
        return f"\n\n{'#' * level} {body}\n\n" if body else "\n\n"

    text = re.sub(r"<h([1-3])[^>]{0,500}>(.{0,2000}?)</h\1>", _heading, text, flags=re.DOTALL | re.IGNORECASE)

    def _link(match: re.Match[str]) -> str:
        href = match.group(1).strip()
        label = re.sub(r"<[^>]+>", "", match.group(2))
        label = html.unescape(re.sub(r"\s+", " ", label)).strip()
        return f"[{label}]({href})" if label else href

    text = re.sub(
        r"<a\b[^>]{0,500}href=[\"']([^\"']{1,2000})[\"'][^>]{0,500}>(.{0,2000}?)</a>",
        _link,
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<li[^>]{0,500}>(.{0,2000}?)</li>", r"\n- \1", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|section|article)>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def read_text_lossless(path: Path) -> tuple[str, bytes]:
    """Reads a file without ever replacing bytes: invalid UTF-8 survives as surrogates
    (re-encoded verbatim on write). Line endings are normalised to `\\n` for matching."""
    raw = path.read_bytes()
    text, _encoding = decode_file_bytes(raw)
    return text.replace("\r\n", "\n"), raw


def printable(text: str) -> str:
    """Makes text with escaped surrogates safe to print/serialise."""
    return text.encode("utf-8", errors="replace").decode("utf-8")


_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d+,\d*\})")


def _regex_is_risky(pattern: str) -> bool:
    """Heuristic ReDoS check: a quantified group that itself contains a quantifier, like `(a+)+`."""
    return bool(_NESTED_QUANTIFIER.search(pattern)) or len(pattern) > 500


MAX_FETCH_BYTES = 2_000_000
MAX_HTML_CHARS = 300_000
MAX_REDIRECTS = 5
MAX_OUTPUT_KEEP = 100_000

from . import sandbox  # noqa: E402
from .envscrub import _SENSITIVE_ENV_NAMES, sanitized_env  # noqa: E402,F401  (re-exported)


def truncate_middle(text: str, limit: int = 4000) -> str:
    """Keeps the head and the tail of long output: the end usually holds the error."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n…[truncated {omitted} chars]…\n{text[-tail:]}"


async def _read_capped(stream: Any, keep: int = MAX_OUTPUT_KEEP) -> bytes:
    """Drains a pipe completely (so the child never blocks) but stores only head + tail."""
    if stream is None:
        return b""
    head = bytearray()
    tail = bytearray()
    dropped = 0
    while True:
        data = await stream.read(65536)
        if not data:
            break
        if len(head) < keep:
            take = data[: keep - len(head)]
            head.extend(take)
            data = data[len(take):]
        if data:
            tail.extend(data)
            if len(tail) > keep:
                dropped += len(tail) - keep
                del tail[: len(tail) - keep]
    marker = f"\n…[output truncated, {dropped} bytes dropped]…\n".encode("utf-8") if dropped else b""
    return bytes(head) + marker + bytes(tail)


async def _kill_process_tree(proc: Any) -> None:
    """Kills the whole process tree of `proc` (a plain kill() leaves grandchildren running)."""
    if proc.returncode is None:
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/T",
                    "/F",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=10.0)
            except Exception:
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined]
            except (ProcessLookupError, PermissionError, OSError):
                pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for loopback, link-local, private (RFC1918, fc00::/7), unspecified and other non-public addresses."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not ip.is_global or ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_unspecified


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    host = host.strip("[]")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:  # decimal / hex / octal IPv4 spellings (2130706433, 0x7f.1) that resolvers accept
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
    except (OSError, ValueError):
        return None


async def check_url_safe(url: str, *, allow_private: bool = False) -> str | None:
    """SSRF guard: returns an error message when `url` must not be fetched, else None."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except ValueError:
        return "invalid URL"
    if parsed.scheme not in ("http", "https"):
        return "URL must start with http:// or https://"
    if not host:
        return "URL has no host"
    if allow_private:
        return None
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return f"blocked: {host} is a loopback host"
    literal = _parse_ip_literal(host)
    if literal is not None:
        if _is_blocked_ip(literal):
            return f"blocked: {host} is a private, loopback or link-local address"
        return None
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return None  # unresolvable: the request itself will fail
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            return f"blocked: {host} resolves to a private, loopback or link-local address ({ip})"
    return None


@dataclass
class FetchedPage:
    status_code: int
    content: bytes
    encoding: str = "utf-8"
    truncated: bool = False

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


class UnsafeURL(ValueError):
    """Raised when a URL (or one of its redirect hops) points at a non-public host."""


def _charset_from(content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        try:
            "".encode(match.group(1))
            return match.group(1)
        except LookupError:
            pass
    return "utf-8"


async def safe_http_get(
    client: Any,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    allow_private: bool = False,
    max_bytes: int = MAX_FETCH_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchedPage:
    """GET with a per-hop SSRF check, manual redirects and a streamed body size cap."""
    current = url
    for _hop in range(max_redirects + 1):
        problem = await check_url_safe(current, allow_private=allow_private)
        if problem:
            raise UnsafeURL(problem)
        if isinstance(client, httpx.AsyncClient):
            async with client.stream("GET", current, headers=headers, follow_redirects=False) as resp:
                status = resp.status_code
                location = resp.headers.get("location", "")
                encoding = _charset_from(resp.headers.get("content-type", ""))
                body = bytearray()
                truncated = False
                if not (300 <= status < 400 and location):
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            del body[max_bytes:]
                            truncated = True
                            break
        else:  # injected duck-typed client (tests): no streaming API
            resp = await client.get(current, headers=headers, follow_redirects=False)
            status = resp.status_code
            location = (resp.headers.get("location", "") if getattr(resp, "headers", None) else "") or ""
            encoding = _charset_from(resp.headers.get("content-type", "") if getattr(resp, "headers", None) else "")
            raw = resp.content if isinstance(getattr(resp, "content", None), (bytes, bytearray)) else str(resp.text).encode("utf-8")
            truncated = len(raw) > max_bytes
            body = bytearray(raw[:max_bytes])
        if 300 <= status < 400 and location:
            current = str(httpx.URL(current).join(location))
            continue
        return FetchedPage(status, bytes(body), encoding, truncated)
    raise UnsafeURL(f"too many redirects (more than {max_redirects})")


async def _resolve_pinned_ips(host: str) -> list[str]:
    """Resolves `host` ONCE and returns its addresses, refusing the host if any is non-public.

    Raises UnsafeURL for loopback/private/link-local targets and httpx.ConnectError when
    the name does not resolve (so callers see the same error as a failed connection).
    """
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise UnsafeURL(f"blocked: {host} is a loopback host")
    literal = _parse_ip_literal(host)
    if literal is not None:
        if _is_blocked_ip(literal):
            raise UnsafeURL(f"blocked: {host} is a private, loopback or link-local address")
        return [str(literal)]
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise httpx.ConnectError(f"cannot resolve {host}: {exc}") from exc
    addresses: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise UnsafeURL(f"blocked: {host} resolves to a private, loopback or link-local address ({ip})")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise httpx.ConnectError(f"cannot resolve {host}: no usable address")
    return addresses


class PinnedIPTransport(httpx.AsyncBaseTransport):
    """httpx transport that connects to the exact address it validated (anti DNS-rebinding).

    A separate "check the name, then let httpx connect" has a gap: the name is resolved a
    second time at connect time, and an attacker-controlled DNS server can answer a public
    address to the check and 127.0.0.1 (or a cloud metadata address) to the connection.
    Here the name is resolved once per request, every address must be public, and the
    request is sent to that IP. The `Host` header and the TLS server name (SNI and
    certificate verification) keep the original host name.

    Proxies: a client built with this transport ignores HTTP(S)_PROXY (httpx only reads the
    environment when no transport is given). That is deliberate: a proxy would resolve the
    name itself and the pinning would mean nothing. The cost is that a machine that can only
    reach the internet through a proxy cannot use `webfetch` unless `webfetch_allow_private`
    is set, which makes `new_fetch_client` use the plain, proxy-aware client.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None, resolver: Any = None) -> None:
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        self._resolve = resolver or _resolve_pinned_ips

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.raw_host.decode("ascii")
        addresses = await self._resolve(host)
        original_url = request.url
        original_extensions = dict(request.extensions)
        last_error: Exception | None = None
        for address in addresses:
            if address == host:  # the URL already names a validated IP: nothing to pin
                return await self._inner.handle_async_request(request)
            request.url = original_url.copy_with(host=address)
            request.extensions = {**original_extensions, "sni_hostname": host}
            try:
                return await self._inner.handle_async_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc  # try the next validated address
        raise last_error or httpx.ConnectError(f"cannot connect to {host}")

    async def aclose(self) -> None:
        await self._inner.aclose()


def new_fetch_client(timeout: float, *, allow_private: bool = False) -> httpx.AsyncClient:
    """Client for fetching URLs chosen by the model: pinned to validated public IPs."""
    if allow_private:
        return httpx.AsyncClient(timeout=timeout)
    return httpx.AsyncClient(timeout=timeout, transport=PinnedIPTransport())


def _is_reparse_point(path: str | Path) -> bool:
    """True for symlinks and Windows junctions/reparse points."""
    try:
        if os.path.islink(path):
            return True
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def is_protected_agent_path(rel_parts: tuple[str, ...]) -> bool:
    """Paths the agent must never rewrite silently: VCS internals, the audit log and the app config."""
    parts = tuple(part.lower() for part in rel_parts)
    if ".git" in parts:
        return True
    if parts and parts[0] == ".rbit" and len(parts) >= 2:
        if parts[1] == "audit" or parts[1].startswith("config") or ".backup-" in parts[-1]:
            return True
    return False


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translates a glob (`*`, `?`, `[..]`, `**`, `{a,b}`) into an anchored regex on `/` paths."""

    def convert(text: str) -> str:
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            if c == "*":
                if text[i : i + 2] == "**":
                    i += 2
                    if text[i : i + 1] == "/":
                        out.append("(?:.*/)?")
                        i += 1
                    else:
                        out.append(".*")
                    continue
                out.append("[^/]*")
            elif c == "?":
                out.append("[^/]")
            elif c == "[":
                j = text.find("]", i + 2)
                if j == -1:
                    out.append(re.escape(c))
                else:
                    body = text[i + 1 : j]
                    if body.startswith("!"):
                        body = "^" + body[1:]
                    out.append("[" + body.replace("\\", "\\\\") + "]")
                    i = j
            elif c == "{":
                j = text.find("}", i + 1)
                if j == -1:
                    out.append(re.escape(c))
                else:
                    options = text[i + 1 : j].split(",")
                    out.append("(?:" + "|".join(convert(opt) for opt in options) + ")")
                    i = j
            else:
                out.append(re.escape(c))
            i += 1
        return "".join(out)

    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile("^" + convert(pattern.replace("\\", "/")) + "$", flags)


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
        self._listing_truncated: bool = False
        self._formatter_audited: set[str] = set()

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
        """Returns the last matching effect, or None when no rule covers the request.

        Shell commands are judged statement by statement (split on `; && || | &`
        and newlines, wrappers like `sudo`/`env`/`FOO=1` stripped, whitespace
        collapsed): a `deny` on any statement denies the line, and an `allow`
        only holds when every statement is allowed."""
        rules = self.permission_rule_chain(agent_mode)

        def last_match(value: str) -> str | None:
            matched: str | None = None
            for rule in rules:
                if not permission_pattern_matches(rule["action"], action):
                    continue
                if permission_pattern_matches(rule["resource"], value, action=action):
                    matched = rule["effect"]
            return matched

        if action != "shell":
            return last_match(resource)

        collapsed = " ".join(resource.split())
        statements = [normalize_shell_statement(s) for s in split_shell_statements(resource)]
        statements = [s for s in statements if s]
        candidates = [resource, collapsed]
        whole = [last_match(c) for c in candidates]
        per_statement = [last_match(s) for s in statements]
        everything = whole + per_statement
        if "deny" in everything:
            return "deny"
        if len(statements) <= 1:
            # Single statement: the normalized form is as good as the literal one.
            effects = [e for e in whole + per_statement if e is not None]
            return effects[-1] if effects else None
        if "ask" in per_statement:
            return "ask"
        if per_statement and all(e == "allow" for e in per_statement):
            return "allow"
        if any(e is None for e in per_statement):
            return None
        return last_match(collapsed)

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

    async def _check_protected_edit(
        self, target: Path, resource: str, agent_mode: str
    ) -> ToolResult | None:
        """Writes to `.git/**`, `.rbit/config*` and `.rbit/audit/**` always need a human yes.

        Even `edits`/`auto` modes do not silence this: those paths can run code
        (git hooks, config) or erase the audit trail. A rule that names the
        protected path explicitly (allow or deny) is the user's own decision and
        is honoured; a blanket `edit *` allow is not."""
        try:
            rel_parts = target.relative_to(self.workspace).parts
        except ValueError:
            return None
        if not is_protected_agent_path(rel_parts):
            return None
        explicit: str | None = None
        for rule in self.permission_rule_chain(agent_mode):
            if not permission_pattern_matches(rule["action"], "edit"):
                continue
            mentions = ".git" in rule["resource"].lower() or ".rbit" in rule["resource"].lower()
            if mentions and permission_pattern_matches(rule["resource"], resource, action="edit"):
                explicit = rule["effect"]
        if explicit == "allow":
            return None
        reason = "denied by a permission rule" if explicit == "deny" else "protected path"
        approved = False
        if explicit != "deny" and self.permission_cb is not None:
            approved = bool(await self.permission_cb("edit", f"{resource} (protected path)"))
            reason = "rejected by the user"
        elif explicit != "deny":
            reason = "requires approval, but no interactive client is attached"
        if approved:
            return None
        self._audit_permission("edit", resource, False, "protected-path")
        return ToolResult(
            False,
            f"blocked: protected path {resource[:40]}",
            f"Permission denied: `{resource}` is protected (git internals, rbit config or audit log) - {reason}. "
            "Add an explicit `edit` rule naming this path in `permissions` to allow it.",
        )

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
            if action == "edit" and target is not None:
                protected_block = await self._check_protected_edit(target, resource, agent_mode)
                if protected_block is not None:
                    return protected_block
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

    MAX_LISTED_FILES = 5000

    def _points_outside(self, path: str | Path) -> bool:
        """True when `path` is a symlink/junction whose target leaves the workspace."""
        if not _is_reparse_point(path):
            return False
        try:
            real = Path(os.path.realpath(path))
        except OSError:
            return True
        for root in (self.workspace, *self.extra_roots):
            try:
                real.relative_to(root)
                return False
            except ValueError:
                continue
        return True

    def list_workspace_files(self, limit: int = 250) -> list[str]:
        now = time.monotonic()
        if self._file_cache is not None and now - self._file_cache[0] < 2.0:
            return self._file_cache[1][:limit]
        results: list[str] = []
        truncated = False
        ignore = self._gitignore_matcher()
        for root, dirs, files in os.walk(self.workspace):
            root_rel = self.rel_display(Path(root))
            dirs[:] = [
                d
                for d in dirs
                if d not in IGNORED_DIRS
                and not ignore.is_ignored(f"{root_rel}/{d}" if root_rel != "." else d, is_dir=True)
                and not self._points_outside(os.path.join(root, d))
            ]
            for fname in sorted(files):
                full = Path(root) / fname
                rel = self.rel_display(full)
                if ignore.is_ignored(rel):
                    continue
                if self._points_outside(full):
                    continue
                results.append(rel)
                if len(results) >= self.MAX_LISTED_FILES:
                    truncated = True
                    break
            if truncated:
                break
        self._listing_truncated = truncated
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
            f"{summary} · {len(issues)} {noun}",
            f"{output}\n\nDiagnostics (syntax errors detected — fix immediately):\n{diag_block}",
        )

    # --- Undo / Redo ---
    def _push_snapshot(
        self,
        label: str,
        target: Path,
        existed: bool,
        old_text: str,
        new_text: str,
        old_bytes: bytes | None = None,
    ) -> None:
        """Records an undo step; the original bytes are kept so non-UTF-8 files restore exactly."""
        snap = FileSnapshot(
            step_label=label,
            path=str(target),
            existed_before=existed,
            previous_content=old_text,
            new_content=new_text,
        )
        snap.previous_bytes = old_bytes  # type: ignore[attr-defined]
        self.undo_stack.append(snap)
        self.redo_stack.clear()

    def undo_last(self, force: bool = False) -> str:
        if not self.undo_stack:
            return "No file changes to undo."
        snap = self.undo_stack.pop()
        target = Path(snap.path)
        rel = self.rel_display(target)
        if not force:
            try:
                current = read_text_lossless(target)[0] if target.exists() else None
            except OSError:
                current = None
            if current is None or current != snap.new_content.replace("\r\n", "\n"):
                return (
                    f"Cannot undo `{rel}` ({snap.step_label}): the file changed after that edit, "
                    "so reverting would silently overwrite newer content. The file was left as it is."
                )
        if snap.existed_before:
            target.parent.mkdir(parents=True, exist_ok=True)
            previous_bytes = getattr(snap, "previous_bytes", None)
            if previous_bytes is not None:
                atomic_write_bytes(target, previous_bytes, mode_from=target)
            else:
                atomic_write_text(target, snap.previous_content)
        elif target.exists():
            target.unlink()
        self.invalidate_file_cache()
        self.redo_stack.append(snap)
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
    TRUNCATED_KEY = "__truncated__"

    def create_checkpoint(self, label: str = "") -> CheckpointMeta:
        files_map: dict[str, str] = {}
        manifest: list[str] = []
        self.invalidate_file_cache()
        listed = self.list_workspace_files(limit=self.MAX_LISTED_FILES)
        truncated = self._listing_truncated
        for rel in listed:
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
        if truncated:
            # The file listing hit its cap: the manifest is incomplete, so a restore
            # must never delete "extra" files based on it.
            files_map[self.TRUNCATED_KEY] = "1"
        return self.storage.save_checkpoint(label or "Manual checkpoint", files_map)

    def _safe_restore_path(self, rel: Any) -> Path | None:
        """Validates a checkpoint manifest key: no absolute paths, `..`, protected paths or escaping links."""
        if not isinstance(rel, str) or not rel or "\x00" in rel:
            return None
        candidate = Path(rel)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            return None
        if is_protected_agent_path(candidate.parts):
            return None
        full = self.workspace / candidate
        try:
            full.resolve().relative_to(self.workspace)
        except (ValueError, OSError):
            return None
        return full

    def restore_checkpoint(self, cid: str) -> str:
        loaded = self.storage.load_checkpoint_files(cid)
        if not loaded:
            return f"Checkpoint `{cid}` not found."
        meta, saved_files = loaded
        saved_files = dict(saved_files)
        self.invalidate_file_cache()

        # Safety net: restore is destructive, so snapshot the current state first.
        try:
            self.create_checkpoint(f"auto: before restore of {meta.label}"[:80])
        except Exception:
            logger.warning("could not create the pre-restore safety checkpoint", exc_info=True)
        self.invalidate_file_cache()
        current_files = set(self.list_workspace_files(limit=self.MAX_LISTED_FILES))
        current_truncated = self._listing_truncated

        # Files that existed at checkpoint time (text or binary). Older checkpoints
        # have no manifest; then the saved text files are the best approximation.
        manifest = set(saved_files.keys())
        raw_manifest = saved_files.pop(self.MANIFEST_KEY, None)
        saved_truncated = saved_files.pop(self.TRUNCATED_KEY, None) is not None
        manifest.discard(self.MANIFEST_KEY)
        manifest.discard(self.TRUNCATED_KEY)
        if raw_manifest:
            try:
                manifest = manifest | {str(item) for item in json.loads(raw_manifest) if isinstance(item, str)}
            except (TypeError, ValueError):
                pass

        # Restore saved text files
        restored = 0
        skipped = 0
        for rel, content in saved_files.items():
            full = self._safe_restore_path(rel)
            if full is None or not isinstance(content, str):
                skipped += 1
                continue
            full.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(full, content)
            restored += 1
        # Remove newly created files that didn't exist at checkpoint time. Only
        # files a snapshot could hold (decodable text, within the size limit) are
        # deletable - restore could never recreate a binary file, so it must stay.
        removed = 0
        keep_extra = saved_truncated or current_truncated
        for rel in sorted(current_files - manifest):
            if keep_extra:
                break
            full = self._safe_restore_path(rel)
            if full is None:
                continue
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

        # The old undo/redo history describes a workspace that no longer exists.
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.invalidate_file_cache()
        notes = ""
        if skipped:
            notes += f", {skipped} unsafe path(s) skipped"
        if keep_extra:
            notes += ", file listing was truncated so no files were removed"
        return f"Rewound workspace to `{meta.label}` ({restored} restored, {removed} removed{notes})."

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
        notebook_mutation = name == "notebook" and str(args.get("action", "read")).lower() != "read"
        if agent_mode == "plan" and (name in ("write", "edit", "multi_edit", "apply_patch") or notebook_mutation):
            return ToolResult(
                ok=False,
                summary=f"{name} blocked in Plan mode",
                output="Plan mode is read-only. Press Tab to switch to Build mode to modify files.",
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
        schedule_mutation = name == "schedule" and str(args.get("action") or "list").strip().lower() in (
            "create",
            "add",
            "remove",
            "delete",
        )
        if schedule_mutation and (agent_mode == "plan" or self.session_readonly):
            why = "read-only session" if self.session_readonly else "Plan mode"
            return ToolResult(
                ok=False,
                summary=f"schedule blocked: {why}",
                output=f"Creating or removing schedules is not allowed in {why}.",
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
            if name in ("write", "edit", "multi_edit", "apply_patch") or notebook_mutation:
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
                res = await self._tool_terminal(args, agent_mode=agent_mode)
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
                # MCP tools come from third-party servers, so their risk is
                # classified from annotations or the tool name and then gated by
                # the same permission engine as built-in tools.
                blocked = await self._authorize_mcp_tool(name, agent_mode)
                if blocked is not None:
                    res = blocked
                else:
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

        res.output = redact_secrets(res.output, code=name in ("read", "read_many_files", "symbols"))
        if res.diff:
            res.diff = redact_secrets(res.diff)
        if res.ok and name in ("write", "edit", "multi_edit", "apply_patch"):
            try:
                formatted = await self._format_mutated_files(name, args, agent_mode)
            except Exception as exc:  # noqa: BLE001 - a formatter must never fail the edit itself
                logger.warning("formatter step failed after %s: %s", name, exc)
                formatted = ""
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

    def mcp_tool_risk(self, name: str) -> str:
        """Risk class for an exposed MCP tool name (read/write/destructive)."""
        from .policy import classify_mcp_tool

        tool = None
        if self.mcp is not None:
            tool = getattr(self.mcp, "tools", {}).get(name)
        if tool is not None:
            return classify_mcp_tool(tool, name)
        return classify_mcp_tool(None, name)

    async def _authorize_mcp_tool(self, name: str, agent_mode: str) -> ToolResult | None:
        """Applies permissions to an MCP tool; returns a block result or None.

        MCP tools arrive from third-party servers, so their default is *safe*,
        unlike file reads: `read` passes, while `write` and `destructive` require
        an explicit allow rule or user approval. Relying on `_authorize` alone
        would not work here — it treats an unknown action as "no rule, allow",
        which is the opposite of what a foreign tool needs.
        """
        from .policy import mcp_action_for

        risk = self.mcp_tool_risk(name)
        action = mcp_action_for(risk)

        # An explicit rule always wins.
        explicit = self.resolve_permission_effect(action, name, agent_mode)
        if explicit == "allow":
            return None
        if explicit is None and risk == "read":
            return None  # reads are as harmless as reading a file

        allowed = False
        reason = "deny" if explicit == "deny" else "approval-required"
        if explicit != "deny":
            if self.mode_allows(action):
                allowed, reason = True, "mode"
            elif self.permission_cb is not None:
                approved = await self.permission_cb(action, name)
                allowed = bool(approved)
                reason = "ask" if allowed else "rejected"
            elif risk == "destructive":
                # A destructive call with nobody to ask is refused, not guessed.
                allowed, reason = False, "approval-required"
            else:
                # No rule, no confirmation channel: fall back to the engine's own
                # default (allow), matching how every other tool behaves. Silent
                # breakage of unannotated MCP servers would be worse than a prompt.
                allowed, reason = True, "no-rule"
        elif explicit == "deny":
            allowed, reason = False, "deny"

        if allowed:
            return None
        return ToolResult(
            False,
            f"blocked: MCP tool {name} ({risk}, {reason})",
            f"MCP tool `{name}` is classified **{risk}** and permission `{reason}` "
            f"refused it. Grant `{action}` for `{name}` in `permissions`, or use a "
            "less dangerous tool.",
        )

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

    async def _approved_formatters(self, paths: list[Path], config: dict[str, Any], agent_mode: str) -> set[str]:
        """Formatter names allowed to run on `paths`. Stock built-ins run as before;
        a formatter whose command comes from the project config is a shell command
        and goes through the same approval gate as `bash`."""
        approved: set[str] = set()
        for definition in build_formatters(config, self.workspace):
            if not any(path.suffix.lower() in definition.extensions for path in paths):
                continue
            if not formatter_is_custom(definition):
                approved.add(definition.name)
                continue
            command_line = " ".join(definition.command)
            if await self._confirm_shell(command_line, agent_mode) is None:
                approved.add(definition.name)
            else:
                logger.warning("formatter %s from config not approved; skipped", definition.name)
        return approved

    async def _format_mutated_files(self, name: str, args: dict[str, Any], agent_mode: str = "build") -> str:
        config = self.get_config() if self.get_config is not None else None
        if not formatter_enabled(config):
            return ""
        paths = [p for p in self._mutation_paths(name, args) if p.is_file()]
        if not paths:
            return ""
        allowed = await self._approved_formatters(paths, config or {}, agent_mode)
        notes: list[str] = []
        for path in paths:
            result = await asyncio.to_thread(format_file, path, config, self.workspace, allowed=allowed)
            if not result.ran:
                continue
            rel = self.rel_display(path)
            if result.ok:
                state = "formatted" if result.changed else "already formatted"
                notes.append(f"{result.name}: {rel} ({state})")
            else:
                notes.append(f"{result.name}: {rel} failed (tool output kept unchanged)")
        return "\n".join(notes)

    @staticmethod
    def _int_arg(value: Any, default: int) -> int:
        """Tolerant int parsing for model-supplied numbers (None, "abc", floats)."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _tool_read(self, args: dict[str, Any]) -> ToolResult:
        raw = args.get("filePath") or args.get("path") or "."
        offset = max(1, self._int_arg(args.get("offset"), 1))
        limit = max(1, min(500, self._int_arg(args.get("limit"), 200)))
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
        if size > MAX_READ_BYTES and args.get("offset") is None and args.get("limit") is None:
            size_kb = size / 1024
            return ToolResult(
                False,
                f"{rel} (too large: {size_kb:,.0f} KB)",
                f"File is {size_kb:,.0f} KB, above the {MAX_READ_BYTES // 1024} KB read budget. "
                "Use `grep`/`symbols` to locate what you need, or pass `offset` and `limit` to read a slice.",
            )

        if target.suffix.lower() == ".pdf":
            return self._read_pdf(target, rel, offset, limit)

        raw_bytes = target.read_bytes()
        encoding = detect_file_encoding(raw_bytes)
        text = raw_bytes.decode(encoding, errors="replace")
        lines = text.splitlines()
        sliced = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{idx}: {line}" for idx, line in enumerate(sliced, start=offset))
        output = numbered or "(empty file)"
        if encoding != "utf-8":
            # Tell the model: the text above is already decoded, and edits keep this encoding.
            output += f"\n[file encoding: {encoding}; edit and write keep it]"
        return ToolResult(True, f"{len(sliced)}/{len(lines)} lines", output)

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
        old_content, old_bytes = read_text_lossless(target) if existed else ("", None)

        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, content)
        self.invalidate_file_cache()

        self._push_snapshot(f"write {rel}", target, existed, old_content, content, old_bytes)

        diff, added, removed = self._compute_diff(old_content, content, rel)
        self._record_file_change(rel, added, removed)
        summary, output = self._attach_diagnostics(
            f"(+{added} -{removed})",
            f"Wrote {rel} (+{added} -{removed} lines)",
            rel,
            content,
        )
        return ToolResult(True, summary, output, diff=printable(diff))

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

    @staticmethod
    def _reindent(new_str: str, old_str: str, matched: str) -> str:
        """Maps the indentation the model used for `old_str` onto the block actually found in the file."""

        def leading(text: str) -> str:
            first = text.split("\n", 1)[0]
            return first[: len(first) - len(first.lstrip(" \t"))]

        file_indent = leading(matched)
        old_indent = leading(old_str)
        if file_indent == old_indent:
            return new_str
        shifted: list[str] = []
        for line in new_str.split("\n"):
            if not line.strip():
                shifted.append(line)
            elif line.startswith(old_indent):
                shifted.append(file_indent + line[len(old_indent):])
            else:
                shifted.append(line)
        return "\n".join(shifted)

    def _apply_fuzzy_spans(self, content: str, spans: list[tuple[int, int]], old_str: str, new_str: str) -> str:
        """Replaces every span, last first, so earlier offsets stay valid."""
        result = content
        for start, end in sorted(spans, reverse=True):
            matched = result[start:end]
            addition = self._reindent(new_str, old_str, matched)
            # The span covers the matched lines including their trailing newline;
            # keep it, or the replacement glues the next line onto the last one.
            if matched.endswith("\n") and not addition.endswith("\n"):
                addition += "\n"
            result = result[:start] + addition + result[end:]
        return result

    def _tool_edit(self, args: dict[str, Any]) -> ToolResult:
        raw = args.get("filePath") or args.get("path")
        old_str = str(args.get("oldString", "")).replace("\r\n", "\n")
        new_str = str(args.get("newString", "")).replace("\r\n", "\n")
        replace_all = bool(args.get("replaceAll", False))

        if not raw:
            return ToolResult(False, "missing filePath", "filePath is required.")
        if not old_str:
            # `"".replace("", x)` inserts x between every character and `count("")`
            # matches everywhere, so an empty oldString would corrupt the file.
            return ToolResult(
                False,
                "empty oldString",
                "oldString must not be empty; use `write` to create or overwrite a file.",
            )
        target = self.resolve_path(raw)
        rel = self.rel_display(target)
        if not target.exists():
            return ToolResult(False, f"{rel} not found", f"File does not exist: {rel}")

        old_content, old_bytes = read_text_lossless(target)
        count = old_content.count(old_str)
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
            if not replace_all:
                spans = spans[:1]
            else:
                # Overlapping line windows (a repeated block) cannot all be replaced.
                kept: list[tuple[int, int]] = []
                for span in spans:
                    if not kept or span[0] >= kept[-1][1]:
                        kept.append(span)
                spans = kept
            new_content = self._apply_fuzzy_spans(old_content, spans, old_str, new_str)
            notes.append(
                f"fuzzy match (whitespace normalized, {len(spans)} replaced) — verify the diff"
            )
        elif count > 1 and not replace_all:
            return ToolResult(
                False,
                f"{count} ambiguous matches",
                f"Found {count} matches for oldString; add context or set replaceAll=true.",
            )
        else:
            new_content = (
                old_content.replace(old_str, new_str)
                if replace_all
                else old_content.replace(old_str, new_str, 1)
            )
        atomic_write_text(target, new_content)
        self.invalidate_file_cache()

        self._push_snapshot(f"edit {rel}", target, True, old_content, new_content, old_bytes)

        diff, added, removed = self._compute_diff(old_content, new_content, rel)
        self._record_file_change(rel, added, removed)
        summary, output = self._attach_diagnostics(
            f"(+{added} -{removed})",
            f"Edited {rel} (+{added} -{removed} lines)"
            + (f"\n\nNote: {notes[0]}" if notes else ""),
            rel,
            new_content,
        )
        return ToolResult(True, summary, output, diff=printable(diff))

    def _tool_multi_edit(self, args: dict[str, Any]) -> ToolResult:
        raw = args.get("filePath") or args.get("path")
        edits = args.get("edits") or []
        if not raw or not isinstance(edits, list) or not edits:
            return ToolResult(False, "invalid multi_edit args", "filePath and non-empty edits list required.")
        target = self.resolve_path(raw)
        rel = self.rel_display(target)
        if not target.exists():
            return ToolResult(False, f"{rel} not found", f"File does not exist: {rel}")

        original, original_bytes = read_text_lossless(target)
        updated = original
        for idx, ed in enumerate(edits, start=1):
            if not isinstance(ed, dict):
                return ToolResult(False, f"edit #{idx} failed", f"edit #{idx} must be an object.")
            old_s = str(ed.get("oldString", "")).replace("\r\n", "\n")
            new_s = str(ed.get("newString", "")).replace("\r\n", "\n")
            rep_all = bool(ed.get("replaceAll", False))
            if not old_s:
                return ToolResult(False, f"edit #{idx} failed", f"oldString in edit #{idx} must not be empty.")
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
        self._push_snapshot(f"multi_edit {rel}", target, True, original, updated, original_bytes)

        diff, added, removed = self._compute_diff(original, updated, rel)
        self._record_file_change(rel, added, removed)
        summary, output = self._attach_diagnostics(
            f"{len(edits)} edits (+{added} -{removed})",
            f"Applied {len(edits)} edits to {rel} (+{added} -{removed})",
            rel,
            updated,
        )
        return ToolResult(True, summary, output, diff=printable(diff))

    _HUNK_HEADER = re.compile(r"^@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@")

    @staticmethod
    def _parse_unified_diff(patch: str) -> tuple[str | None, list[tuple[int, int, list[str]]]]:
        """Parses a unified diff into (target_path, [(old_start, old_len, hunk_lines)]).

        `old_start` is -1 for LLM-style headers without line numbers (`@@ def foo @@`):
        those hunks are located by their content. Inside a numbered hunk the line
        counters decide where the hunk ends, so a removed `-- comment` (diff line
        `--- comment`) or an added `++i` is never mistaken for a file header."""
        target: str | None = None
        hunks: list[tuple[int, int, list[str]]] = []
        cur: list[str] | None = None
        old_start = 0
        old_len = 0
        new_len = 0
        counted = False
        old_seen = 0
        new_seen = 0
        pending_blank = 0
        lines = patch.replace("\r\n", "\n").split("\n")

        def finish() -> None:
            nonlocal cur
            if cur is not None and not counted:
                # recompute the true old length for an unnumbered hunk
                hunks[-1] = (hunks[-1][0], sum(1 for ln in cur if ln[0] in (" ", "-")), cur)
            cur = None

        skip_next = False
        for idx, raw in enumerate(lines):
            if skip_next:
                skip_next = False
                continue
            if raw.startswith("@@"):
                # A body line starts with ' ', '+', '-' or '\\', so '@@' is always a header.
                finish()
                cur = []
                pending_blank = 0
                old_seen = new_seen = 0
                header = ToolExecutor._HUNK_HEADER.match(raw)
                if header:
                    counted = True
                    old_start = int(header.group(1))
                    old_len = int(header.group(2)) if header.group(2) is not None else 1
                    new_len = int(header.group(4)) if header.group(4) is not None else 1
                else:
                    counted = False
                    old_start, old_len, new_len = -1, 0, 0
                hunks.append((old_start, old_len, cur))
                continue
            incomplete = cur is not None and counted and (old_seen < old_len or new_seen < new_len)
            if not incomplete and raw.startswith("--- ") and idx + 1 < len(lines) and lines[idx + 1].startswith("+++ "):
                finish()
                name = lines[idx + 1][4:].strip().split("\t")[0].strip()
                if name.startswith("b/"):
                    name = name[2:]
                if name and name != "/dev/null":
                    target = name
                skip_next = True
                continue
            if cur is None:
                continue
            if raw.startswith("\\"):
                continue
            if raw == "":
                if counted:
                    # LLM patches strip the space from empty context lines.
                    if old_seen < old_len:
                        cur.append(" ")
                        old_seen += 1
                        new_seen += 1
                else:
                    pending_blank += 1
                continue
            if raw[0] not in (" ", "+", "-"):
                continue
            if pending_blank:
                cur.extend([" "] * pending_blank)
                pending_blank = 0
            cur.append(raw)
            if raw[0] in (" ", "-"):
                old_seen += 1
            if raw[0] in (" ", "+"):
                new_seen += 1
        finish()
        return target, hunks

    @staticmethod
    def _lines_match(output: list[str], block: list[str], pos: int) -> bool:
        if pos < 0 or pos + len(block) > len(output):
            return False
        return all(output[pos + i] == line or output[pos + i].rstrip() == line.rstrip() for i, line in enumerate(block))

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
        old_content, old_bytes = read_text_lossless(target) if existed else ("", None)
        lines = old_content.split("\n") if old_content else []
        if lines and lines[-1] == "":
            lines.pop()

        output = list(lines)
        cursor = 0
        delta = 0
        for old_start, old_len, hunk_lines in hunks:
            old_block = [ln[1:] for ln in hunk_lines if ln[0] in (" ", "-")]
            new_block = [ln[1:] for ln in hunk_lines if ln[0] in (" ", "+")]
            numbered = old_start >= 0
            if numbered:
                # `-N,0` means "insert after line N"; otherwise N is the first old line.
                hint = (old_start if not old_block and old_start > 0 else max(0, old_start - 1)) + delta
            else:
                hint = cursor
            if numbered and hint < cursor:
                return ToolResult(
                    False,
                    f"overlapping hunk at line {old_start}",
                    f"Hunks are out of order or overlapping near line {old_start}.",
                )
            if not old_block:
                pos = min(hint, len(output))
                if numbered and hint > len(output):
                    return ToolResult(
                        False,
                        f"hunk out of range at line {old_start}",
                        f"Hunk targets line {old_start} but the file has {len(output)} lines.",
                    )
            elif self._lines_match(output, old_block, hint):
                pos = hint
            else:
                candidates = [
                    p for p in range(cursor, len(output) - len(old_block) + 1) if self._lines_match(output, old_block, p)
                ]
                if not candidates:
                    at = min(hint, len(output))
                    mismatch = 0
                    while (
                        mismatch < len(old_block)
                        and at + mismatch < len(output)
                        and (
                            output[at + mismatch] == old_block[mismatch]
                            or output[at + mismatch].rstrip() == old_block[mismatch].rstrip()
                        )
                    ):
                        mismatch += 1
                    line_no = at + mismatch + 1
                    if at + mismatch >= len(output):
                        return ToolResult(
                            False,
                            f"patch context beyond EOF at line {line_no}",
                            f"Cannot match `{old_block[mismatch] if mismatch < len(old_block) else ''}` "
                            f"- file ended at line {len(output)}.",
                        )
                    return ToolResult(
                        False,
                        f"context mismatch at line {line_no}",
                        f"Expected `{old_block[mismatch]}` but found `{output[at + mismatch]}` at line {line_no}. "
                        "Re-read the file and regenerate the patch.",
                    )
                pos = min(candidates, key=lambda p: abs(p - hint))
            output[pos : pos + len(old_block)] = new_block
            delta += len(new_block) - len(old_block)
            cursor = pos + len(new_block)

        new_content = "\n".join(output)
        if old_content.endswith("\n") or not existed:
            new_content += "\n"

        atomic_write_text(target, new_content)
        self.invalidate_file_cache()

        self._push_snapshot(f"apply_patch {rel}", target, existed, old_content, new_content, old_bytes)

        diff, added, removed = self._compute_diff(old_content, new_content, rel)
        self._record_file_change(rel, added, removed)
        summary, out = self._attach_diagnostics(
            f"{len(hunks)} hunks (+{added} -{removed})",
            f"Applied patch to {rel} (+{added} -{removed} lines)",
            rel,
            new_content,
        )
        return ToolResult(True, summary, out, diff=printable(diff))

    MAX_GLOB_RESULTS = 100
    MAX_GLOB_SCAN = 50_000
    MAX_GREP_FILES = 5000
    MAX_GREP_LINE = 2000
    GREP_DEADLINE_S = 20.0

    def _tool_glob(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args.get("pattern", "*")).strip() or "*"
        normalized = pattern.replace("\\", "/")
        if (
            normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or Path(pattern).is_absolute()
            or ".." in normalized.split("/")
        ):
            return ToolResult(
                False,
                "invalid glob pattern",
                "Glob patterns must be relative to the workspace and must not contain `..` "
                "or absolute paths. Use e.g. `src/**/*.py`.",
            )
        regex = _glob_to_regex(normalized)
        # A bare pattern (`*.py`) matches the file name at any depth, like rglob.
        by_name = "/" not in normalized
        ignore = self._gitignore_matcher()
        matches: list[str] = []
        scanned = 0
        scan_limited = False
        for root, dirs, files in os.walk(self.workspace):
            root_rel = self.rel_display(Path(root)).replace("\\", "/")
            prefix = "" if root_rel == "." else root_rel + "/"
            # Judge ignored directories by their path relative to the workspace, so a
            # project that lives inside a directory named `build` is still searched.
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in IGNORED_DIRS
                and not ignore.is_ignored(f"{prefix}{d}", is_dir=True)
                and not self._points_outside(os.path.join(root, d))
            )
            for fname in files:
                scanned += 1
                if scanned > self.MAX_GLOB_SCAN:
                    scan_limited = True
                    break
                rel = f"{prefix}{fname}"
                if not (regex.match(fname) if by_name else regex.match(rel)):
                    continue
                if ignore.is_ignored(rel):
                    continue
                if self._points_outside(os.path.join(root, fname)):
                    continue
                matches.append(rel)
            if scan_limited:
                break
        matches.sort()
        total = len(matches)
        shown = matches[: self.MAX_GLOB_RESULTS]
        out = "\n".join(shown) if shown else "No files matched."
        if total > len(shown) or scan_limited:
            out += f"\n[truncated: showing {len(shown)} of {total}{'+' if scan_limited else ''} matches]"
        return ToolResult(True, f"{len(shown)} matches" + (" (truncated)" if total > len(shown) else ""), out)

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
                env=sanitized_env(),
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
            # Context lines look like `.env-12-text`, so the path cannot be cut at the
            # first colon: test every plausible split point.
            if not self._secret_reads_allowed() and self._rg_line_is_secret(rel):
                continue
            lines.append(rel[:200])
        if proc.returncode not in (0, 1):
            # 2+ means rg itself failed (bad regex, IO error); fall back so the
            # caller surfaces a real error instead of a bogus "no matches".
            return None
        cap = 500
        if files_only:
            out = "\n".join(matched[:cap]) if matched else "No matches found."
            if len(matched) > cap:
                out += f"\n[results truncated: showing {cap} of {len(matched)} files]"
            return ToolResult(True, f"{len(matched)} files (rg)", out)
        out = "\n".join(lines[:cap]) if lines else "No matches found."
        if len(lines) > cap:
            out += f"\n[results truncated: showing {cap} of {len(lines)} lines]"
        return ToolResult(True, f"{len(lines)} lines (rg)", out)

    _RG_SPLIT = re.compile(r"[:\-](\d+)[:\-]")

    def _rg_line_is_secret(self, line: str) -> bool:
        """True when any plausible path prefix of an rg output line is a secret file."""
        candidates = {line.split(":", 1)[0]}
        for match in self._RG_SPLIT.finditer(line):
            candidates.add(line[: match.start()])
        return any(c and is_secret_file(self.workspace / c) for c in candidates)

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
        # ripgrep reads UTF-8 (and BOM'd UTF-16) only: a legacy-encoded file (cp1251...) never
        # matches a non-ASCII pattern and silently drops out of the results. The built-in
        # search decodes every file with the same detection as `read`, so use it for those.
        if pattern.isascii():
            rg_result = self._grep_with_ripgrep(pattern, include, files_only, context, max_per_file)
            if rg_result is not None:
                return rg_result
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return ToolResult(False, "invalid regex", f"Invalid regex pattern: {exc}")
        if _regex_is_risky(pattern):
            return ToolResult(
                False,
                "regex too complex",
                "The pattern has nested quantifiers (e.g. `(a+)+`) that can hang the built-in matcher. "
                "Simplify it, or install ripgrep (`rg`) which is safe on such patterns.",
            )
        hits: list[str] = []
        matched_files: list[str] = []
        listed = self.list_workspace_files(limit=self.MAX_GREP_FILES)
        truncated = self._listing_truncated or len(listed) >= self.MAX_GREP_FILES
        deadline = time.monotonic() + self.GREP_DEADLINE_S
        timed_out = False
        hit_cap = 80
        for rel in listed:
            if time.monotonic() > deadline:
                timed_out = True
                break
            full = self.workspace / rel
            if not self._secret_reads_allowed() and is_secret_file(full):
                continue
            if include != "*" and not full.match(include):
                continue
            try:
                text = read_text_for_display(full)
            except Exception:
                continue
            lines = text.splitlines()
            file_hits = 0
            for idx, line in enumerate(lines, start=1):
                if len(line) > self.MAX_GREP_LINE:
                    line = line[: self.MAX_GREP_LINE]
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
                if len(hits) >= hit_cap:
                    break
            if file_hits:
                matched_files.append(rel)
            if len(hits) >= hit_cap:
                truncated = True
                break
        notes: list[str] = []
        if truncated:
            notes.append("results truncated (hit result or file limit)")
        if timed_out:
            notes.append("results truncated (search time budget exhausted)")
        suffix = ("\n[" + "; ".join(notes) + "]") if notes else ""
        if files_only:
            out = "\n".join(matched_files) if matched_files else "No matches found."
            return ToolResult(True, f"{len(matched_files)} files", out + suffix)
        if context and hits and hits[-1] == "--":
            hits.pop()
        out = "\n".join(hits) if hits else "No matches found."
        return ToolResult(True, f"{len(hits)} lines", out + suffix)

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
                for item in self.list_workspace_files(limit=self.MAX_GREP_FILES)
                if Path(item).suffix.lower() in allowed
            ]

        found: list[str] = []
        truncated = False
        for fpath in targets:
            try:
                if fpath.stat().st_size > MAX_READ_BYTES * 2:
                    continue
                content = read_text_for_display(fpath)
            except Exception:
                continue
            found.extend(self._symbols_for_content(self.rel_display(fpath), content))
            if len(found) >= 300:
                found = found[:300]
                truncated = True
                break

        out = "\n".join(found) if found else "No symbols found."
        if truncated:
            out += "\n[results truncated at 300 symbols; narrow `path`]"
        return ToolResult(True, f"{len(found)} symbols", out)

    async def _tool_git(self, args: dict[str, Any], agent_mode: str = "build") -> ToolResult:
        action = str(args.get("action") or "status").strip().lower()
        raw_path = str(args.get("path") or "").strip()
        limit = max(1, min(self._int_arg(args.get("limit"), 20) or 20, 100))
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
            # `--` keeps a path like `-L1,5` from being parsed as an option.
            cmd = ["blame", "--", *path_args[1:]]
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
                env=sanitized_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _err = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except Exception as exc:
            return ToolResult(False, f"git {action} failed", f"git execution error: {exc}")

        output = out_b.decode("utf-8", errors="replace").strip() or "(no output)"
        ok = proc.returncode == 0
        summary = f"git {action} · {len(output.splitlines())} line(s)"
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
            # Bisect moves HEAD and rewrites refs: never in Plan or read-only sessions.
            if agent_mode == "plan" or self.session_readonly:
                why = "read-only session" if self.session_readonly else "Plan mode"
                return ToolResult(
                    False,
                    f"git bisect {sub} blocked: {why}",
                    f"`git bisect {sub}` changes the checkout and is not allowed in {why}. "
                    "Use `status` or `log` to inspect a bisect in progress.",
                )
            if rev.startswith("-"):
                return ToolResult(False, "invalid revision", f"`rev` must not start with `-`: {rev}")
            command_line = f"git bisect {sub} {rev}".strip()
            blocked = await self._confirm_shell(command_line, agent_mode)
            if blocked is not None:
                return ToolResult(
                    False,
                    f"blocked: git bisect {sub}",
                    f"`git bisect {sub}` needs approval ({blocked.output}) Adjust `permissions` "
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
                env=sanitized_env(),
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
                    env=sanitized_env(),
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
        note = "ruff/pyflakes not installed — ran built-in syntax diagnostics only (pip install ruff)."
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
            targets = [self.workspace / rel for rel in self.list_workspace_files(limit=self.MAX_GREP_FILES)]

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
                self.workspace / rel for rel in self.list_workspace_files(limit=self.MAX_GREP_FILES)
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
                page_url = str(args.get("url") or "")
                problem = await check_url_safe(page_url, allow_private=self._allow_private_fetch())
                if problem:
                    return ToolResult(False, "blocked: unsafe URL", f"browser open refused `{page_url}`: {problem}")
                result = await asyncio.to_thread(session.open, page_url)
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

    async def _tool_terminal(self, args: dict[str, Any], agent_mode: str = "build") -> ToolResult:
        from .terminal import PersistentShell, TerminalError

        action = str(args.get("action") or "run").strip().lower()
        if action != "close" and sandbox.mode(self._config_or_empty()) in ("auto", "strict") and sandbox.backend():
            # A long-lived interactive shell cannot be wrapped command by command, and running it
            # outside the sandbox would be a silent way around it.
            return ToolResult(
                False,
                "terminal unavailable in the sandbox",
                "`shell_sandbox` is on, and the persistent `terminal` is not sandboxed. Use `bash`.",
            )
        if action not in ("close", "start", "open"):
            command_to_run = str(args.get("command") or "").strip()
            if command_to_run:
                # Same gate as `bash`: danger list, sandbox, Plan mode, approval.
                blocked = await self._gate_shell(command_to_run, agent_mode)
                if blocked is not None:
                    return blocked
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
            try:
                if self.http_client is not None:
                    fetched = await safe_http_get(
                        self.http_client, url, allow_private=self._allow_private_fetch(), max_bytes=20_000_000
                    )
                else:
                    async with new_fetch_client(60.0, allow_private=self._allow_private_fetch()) as client:
                        fetched = await safe_http_get(
                            client, url, allow_private=self._allow_private_fetch(), max_bytes=20_000_000
                        )
            except UnsafeURL as exc:
                return ToolResult(False, "image download blocked", f"Refused `{url}`: {exc}")
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
            original, original_bytes = read_text_lossless(target)
            nb = json.loads(original)
        except (OSError, ValueError) as exc:
            return ToolResult(False, f"{rel} invalid", f"Cannot parse notebook: {exc}")
        if not isinstance(nb, dict):
            return ToolResult(False, f"{rel} invalid", "Notebook root must be a JSON object.")
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

        def save_nb(label: str) -> str:
            updated = json.dumps(nb, ensure_ascii=False, indent=1) + "\n"
            atomic_write_text(target, updated)
            self.invalidate_file_cache()
            self._push_snapshot(label, target, True, original, updated, original_bytes)
            self._record_file_change(rel, 0, 0)
            return updated

        supports_ids = int(nb.get("nbformat", 4) or 4) == 4 and int(nb.get("nbformat_minor", 0) or 0) >= 5

        if action in ("set_cell", "insert_cell"):
            source = str(args.get("source", ""))
            if not source:
                return ToolResult(False, "missing source", "`source` is required for this action.")
            if action == "set_cell" and not (0 <= index < len(cells)):
                return ToolResult(False, "bad index", f"Cell index {index} out of range (0..{len(cells) - 1}).")
            if action == "set_cell":
                cell = cells[index]
                cell["source"] = source.splitlines(keepends=True)
                if cell.get("cell_type", "code") == "code":
                    cell["outputs"] = []
                    cell["execution_count"] = None
                else:
                    # markdown/raw cells must not carry code-only fields
                    cell.pop("outputs", None)
                    cell.pop("execution_count", None)
                save_nb(f"notebook set_cell {rel}#{index}")
                return ToolResult(True, f"cell #{index} replaced", f"Cell #{index} of {rel} updated.")
            cell_type = str(args.get("cellType") or "code").lower()
            if cell_type not in ("code", "markdown", "raw"):
                return ToolResult(False, "bad cellType", "cellType must be code, markdown or raw.")
            if index < 0 or index > len(cells):
                index = len(cells)
            new_cell: dict[str, Any] = {
                "cell_type": cell_type,
                "metadata": {},
                "source": source.splitlines(keepends=True),
            }
            if cell_type == "code":
                new_cell["outputs"] = []
                new_cell["execution_count"] = None
            if supports_ids:
                new_cell["id"] = uuid.uuid4().hex[:8]
            cells.insert(index, new_cell)
            save_nb(f"notebook insert {rel}#{index}")
            return ToolResult(True, f"cell inserted at #{index}", f"Cell inserted into {rel} at #{index}.")
        if action == "delete_cell":
            if not (0 <= index < len(cells)):
                return ToolResult(False, "bad index", f"Cell index {index} out of range (0..{len(cells) - 1}).")
            cells.pop(index)
            save_nb(f"notebook delete {rel}#{index}")
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
        if not isinstance(raw_todos, list):
            # A string/None from a confused model must not wipe the user's list.
            return ToolResult(
                False,
                "invalid todos",
                "`todos` must be a list of {content, status, priority} objects; the existing list was kept.",
            )
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

    @staticmethod
    def _clean_fetched(raw: str) -> str:
        cleaned = html_to_markdown(raw) if "<" in raw else raw.strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned[:6000] or "(empty response)"

    def _allow_private_fetch(self) -> bool:
        config = self.get_config() if self.get_config is not None else {}
        return bool(config.get("webfetch_allow_private", False)) if isinstance(config, dict) else False

    async def _tool_webfetch(self, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(False, "invalid url", "URL must start with http:// or https://")
        cached = self._webfetch_cache.get(url)
        if cached and time.time() - cached[0] < 600:
            return ToolResult(
                True,
                f"cached \u00b7 Markdown {len(cached[1])} chars",
                self._fence_untrusted(cached[1], f"webfetch:{url}"),
            )
        headers = {"User-Agent": "rbit-cli/1.2"}
        allow_private = self._allow_private_fetch()
        try:
            if self.http_client is not None:
                page = await safe_http_get(self.http_client, url, headers=headers, allow_private=allow_private)
            else:
                async with new_fetch_client(15.0, allow_private=allow_private) as client:
                    page = await safe_http_get(client, url, headers=headers, allow_private=allow_private)
        except UnsafeURL as exc:
            return ToolResult(False, "blocked: unsafe URL", f"webfetch refused `{url}`: {exc}")
        except httpx.HTTPError as exc:
            return ToolResult(False, "webfetch failed", f"{type(exc).__name__}: {exc}")
        # Parsing is CPU-bound regex work on untrusted input: keep it off the event loop.
        cleaned = await asyncio.to_thread(self._clean_fetched, page.text)
        if page.truncated:
            cleaned += f"\n[page truncated at {MAX_FETCH_BYTES // 1_000_000} MB]"
        if page.status_code < 400:
            self._webfetch_cache[url] = (time.time(), cleaned)
        return ToolResult(
            page.status_code < 400,
            f"HTTP {page.status_code} \u00b7 Markdown {len(cleaned)} chars",
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
        """True when the command starts with a user-approved prefix.

        Compared token by token (`npm test` allows `npm test --silent`, never
        `npm testing`) and only for a single plain statement: chaining,
        redirects, substitution or a newline would let an approved prefix carry
        an arbitrary second command."""
        if _SHELL_UNSAFE_CHARS.search(command):
            return False
        tokens = [t.lower() for t in command.split()]
        if not tokens:
            return False
        for prefix in self.allowed_bash_prefixes:
            parts = [t.lower() for t in str(prefix).split()]
            if parts and tokens[: len(parts)] == parts:
                return True
        return False

    def _bash_prefix(self, command: str) -> str:
        return " ".join(command.split()[:2])

    async def _confirm_shell(
        self,
        command: str,
        agent_mode: str,
        *,
        readonly: bool = False,
        preasked: bool = False,
        fail_closed: bool = True,
    ) -> ToolResult | None:
        """Approval step shared by `bash`, `terminal`, git bisect and project formatters.

        Returns None when the command may run. With nobody to ask (no
        `permission_cb`) and no standing approval (`always_allow_bash`, an
        allow rule, an approved prefix, `commands`/`auto` mode) the answer is
        a refusal: fail closed, exactly like `edit`."""
        if readonly:
            return None
        effect = self.resolve_permission_effect("shell", command, agent_mode)
        if effect == "deny":
            return ToolResult(False, f"blocked: {command[:36]}", "Permission denied: `shell` is denied by a permission rule.")
        if effect == "allow" or self.always_allow_bash or self._prefix_allowed(command):
            return None
        if effect == "ask" and preasked:
            return None  # the permission preflight already asked the user
        if effect is None and self.mode_allows("shell"):
            return None
        if self.permission_cb is None:
            if not fail_closed:
                return None  # legacy `bash` behaviour for headless callers without a prompt channel
            return ToolResult(
                False,
                f"blocked: {command[:36]}",
                "Command requires approval, but no interactive client is attached. "
                "Allow it with a `shell` permission rule, `always_allow_bash`, or the `commands`/`auto` permission mode.",
            )
        if not await self.permission_cb("bash", command):
            return ToolResult(False, f"rejected: {command[:36]}", "Command execution rejected by user.")
        return None

    async def _gate_shell(self, command: str, agent_mode: str, *, fail_closed: bool = True) -> ToolResult | None:
        """The one gate every shell-like tool goes through: danger list, sandbox,
        Plan mode, then approval. Returns a blocking result or None."""
        if sandbox.mode(self._config_or_empty()) == "strict" and sandbox.backend() is None:
            return ToolResult(False, f"blocked: {command[:36]}", sandbox.unavailable_message())
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
        return await self._confirm_shell(
            command, agent_mode, readonly=is_readonly, preasked=True, fail_closed=fail_closed
        )

    async def _tool_bash(self, args: dict[str, Any], agent_mode: str = "build") -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(False, "empty command", "command is required.")

        # Fails closed like `terminal`: without a prompt channel a command needs a standing
        # approval (`always_allow_bash`, which `rbit run`/ACP/`--auto` set, an allow rule, or
        # the `commands`/`auto` mode). Read-only commands never need one.
        blocked = await self._gate_shell(command, agent_mode)
        if blocked is not None:
            return blocked

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

    @staticmethod
    def _shell_argv(command: str) -> list[str] | None:
        """PowerShell invocation on Windows (UTF-8 output so Cyrillic is not mangled); None elsewhere."""
        if os.name != "nt":
            return None
        prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$OutputEncoding=[System.Text.Encoding]::UTF8;"
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + command]

    def _config_or_empty(self) -> dict[str, Any]:
        return self.get_config() if self.get_config is not None else {}

    async def _spawn_shell(self, command: str, *, capture: bool) -> asyncio.subprocess.Process:
        out = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
        err = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
        common: dict[str, Any] = {
            "cwd": str(self.workspace),
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": out,
            "stderr": err,
            "env": sanitized_env(),
        }
        cfg = self._config_or_empty()
        if sandbox.mode(cfg) in ("auto", "strict"):
            boxed = sandbox.wrap(command, self.workspace, network=sandbox.network_allowed(cfg))
            if boxed is not None:
                return await asyncio.create_subprocess_exec(*boxed, **common)
        argv = self._shell_argv(command)
        if argv is not None:
            return await asyncio.create_subprocess_exec(*argv, **common)
        return await asyncio.create_subprocess_shell(command, start_new_session=True, **common)

    async def _run_background(self, command: str) -> ToolResult:
        proc = await self._spawn_shell(command, capture=False)
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
        proc = await self._spawn_shell(command, capture=True)

        async def collect() -> tuple[bytes, bytes]:
            out, err, _code = await asyncio.gather(_read_capped(proc.stdout), _read_capped(proc.stderr), proc.wait())
            return out, err

        try:
            stdout_b, stderr_b = await asyncio.wait_for(collect(), timeout=timeout_s)
        except asyncio.TimeoutError:
            await _kill_process_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:
                pass
            return ToolResult(
                False,
                f"timeout: {command[:36]}",
                f"Command timed out after {timeout_s:g}s (the whole process tree was killed). "
                "Increase the `timeout` argument (max 600s) or use `background=true` for long-running processes.",
            )

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        combined = "\n".join(part for part in (stdout, stderr) if part) or "(no output)"
        combined = strip_ansi(combined)
        ok = proc.returncode == 0
        return ToolResult(
            ok,
            f"exit {proc.returncode}",
            truncate_middle(combined, 4000),
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
