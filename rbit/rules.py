"""Project instruction discovery: AGENTS.md / CLAUDE.md and configured `instructions`.

The system prompt receives a bounded, clearly-labelled bundle of project rules.
Local files are discovered from the workspace upwards; global rules fall back to
Claude Code's file when no rbit/opencode global file exists.
"""

from __future__ import annotations

import glob
import os
import time
from pathlib import Path
from typing import Any

import httpx

MAX_FILE_CHARS = 32_000
MAX_TOTAL_CHARS = 96_000
REMOTE_TTL_SECONDS = 60.0

_REMOTE_CACHE: dict[str, tuple[float, str]] = {}


def _read_text(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        if path.stat().st_size > 4 * MAX_FILE_CHARS:
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _truncate(text: str, label: str) -> str:
    if len(text) <= MAX_FILE_CHARS:
        return text
    return text[:MAX_FILE_CHARS] + f"\n\n[truncated: {label} is larger than {MAX_FILE_CHARS} chars]"


def _discover_project_file(workspace: Path) -> Path | None:
    """Nearest AGENTS.md wins over CLAUDE.md; parent directories are searched too."""
    try:
        current = workspace.resolve()
    except OSError:
        current = workspace
    directories = [current, *current.parents]
    for directory in directories:
        for name in ("AGENTS.md", "CLAUDE.md"):
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def _discover_global_file() -> Path | None:
    home = Path.home()
    for candidate in (
        home / ".config" / "rbit" / "AGENTS.md",
        home / ".rbit" / "AGENTS.md",
        home / ".config" / "opencode" / "AGENTS.md",
    ):
        if candidate.is_file():
            return candidate
    claude = home / ".claude" / "CLAUDE.md"
    return claude if claude.is_file() else None


def _expand_path(value: str, workspace: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate


def _expand_instruction_entries(entries: Any, workspace: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    if not isinstance(entries, list):
        return found
    for raw in entries:
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = raw.strip()
        if value.startswith(("http://", "https://")):
            continue
        candidate = _expand_path(value, workspace)
        has_glob = any(char in value for char in ("*", "?", "["))
        if has_glob:
            for match in sorted(glob.glob(str(candidate), recursive=True)):
                path = Path(match)
                if path.is_file():
                    found.append((value, path))
        elif candidate.is_file():
            found.append((value, candidate))
    return found


async def _fetch_remote(
    url: str, http_client: httpx.AsyncClient | None = None
) -> str:
    cached = _REMOTE_CACHE.get(url)
    now = time.monotonic()
    if cached is not None and now - cached[0] < REMOTE_TTL_SECONDS:
        return cached[1]
    client = http_client
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(5.0), follow_redirects=True)
    try:
        response = await client.get(url)
        response.raise_for_status()
        text = response.text
    except Exception:  # noqa: BLE001 - remote rules are optional; never break a turn
        return ""
    finally:
        if owned:
            await client.aclose()
    text = _truncate(text.strip(), url)
    _REMOTE_CACHE[url] = (now, text)
    return text


async def load_rules(
    workspace: Path,
    config: dict[str, Any] | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Returns labelled instruction blocks for the system prompt (may be empty)."""
    config = config or {}
    workspace = Path(workspace)
    blocks: list[str] = []
    seen: set[str] = set()

    project = _discover_project_file(workspace)
    if project is not None:
        text = _read_text(project)
        if text:
            try:
                label = project.relative_to(workspace).as_posix()
            except ValueError:
                label = str(project)
            blocks.append(f'<rules source="{label}">\n{_truncate(text, label)}\n</rules>')
            seen.add(str(project.resolve()))

    global_file = _discover_global_file()
    if global_file is not None and str(global_file.resolve()) not in seen:
        text = _read_text(global_file)
        if text:
            blocks.append(f'<rules source="{global_file}">\n{_truncate(text, str(global_file))}\n</rules>')

    for label, path in _expand_instruction_entries(config.get("instructions"), workspace):
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        text = _read_text(path)
        if not text:
            continue
        seen.add(resolved)
        try:
            display = path.relative_to(workspace).as_posix()
        except ValueError:
            display = label
        blocks.append(f'<rules source="{display}">\n{_truncate(text, display)}\n</rules>')

    remote_entries = config.get("instructions")
    if isinstance(remote_entries, list):
        for entry in remote_entries:
            if not isinstance(entry, str):
                continue
            url = entry.strip()
            if not url.startswith(("http://", "https://")):
                continue
            text = await _fetch_remote(url, http_client)
            if text:
                blocks.append(f'<rules source="{url}">\n{text}\n</rules>')

    if not blocks:
        return ""
    joined = "\n\n".join(blocks)
    if len(joined) > MAX_TOTAL_CHARS:
        joined = joined[:MAX_TOTAL_CHARS] + "\n\n[truncated: rules bundle exceeded the limit]"
    return joined
