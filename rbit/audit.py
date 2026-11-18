"""Append-only audit log of everything the agent does.

The blueprint requires an answer to "who/what/when/why/result" for every agent
action. This log is intentionally boring and appendable: one JSON object per
line in `.rbit/audit/YYYY-MM-DD.jsonl`, so it can be grepped, tailed and read by
other tools without parsing the session format.

Secrets are redacted before anything is written — an audit log that leaks an API
key is worse than no audit log.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Values that look like credentials, redacted before hitting the disk.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(sk|pk|rk|oc)[-_][A-Za-z0-9_-]{8,}"), r"\1-***REDACTED***"),
    (re.compile(r"\b(Bearer)\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE), r"\1 ***REDACTED***"),
    (re.compile(r"\b(gh[pousr])_[A-Za-z0-9]{16,}"), r"\1_***REDACTED***"),
    (re.compile(r"\b(xox[baprs])-[A-Za-z0-9-]{10,}"), r"\1-***REDACTED***"),
    (
        re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd)\b\s*[:=]\s*\S+"),
        r"\1=***REDACTED***",
    ),
)

MAX_FIELD_CHARS = 4000


def redact(text: str) -> str:
    """Removes credential-shaped substrings from free-form text."""
    if not text:
        return text
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _clip(value: Any, limit: int = MAX_FIELD_CHARS, *, redacting: bool = False) -> Any:
    """Truncates long fields, optionally redacting secrets at every depth.

    Redaction must recurse: tool arguments arrive as a dict, so redacting only
    top-level strings would let `{"command": "export API_KEY=..."}` through.
    """
    if isinstance(value, str):
        text = redact(value) if redacting else value
        if len(text) > limit:
            return text[:limit] + f"… [truncated {len(text) - limit} chars]"
        return text
    if isinstance(value, dict):
        return {str(k): _clip(v, limit, redacting=redacting) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit, redacting=redacting) for v in value]
    return value


class AuditLog:
    """Writes one JSON line per agent action; disabled when `enabled` is false."""

    def __init__(self, workspace: Path, enabled: bool = True) -> None:
        self.workspace = Path(workspace)
        self.enabled = enabled
        self.dir = self.workspace / ".rbit" / "audit"
        self._session_id: str = ""

    def bind_session(self, session_id: str) -> None:
        self._session_id = session_id

    def _path_for(self, now: datetime) -> Path:
        return self.dir / f"{now.strftime('%Y-%m-%d')}.jsonl"

    def record(self, event: str, **fields: Any) -> None:
        """Appends one entry. Never raises: auditing must not break a turn."""
        if not self.enabled:
            return
        now = datetime.now(timezone.utc)
        entry: dict[str, Any] = {
            "ts": now.isoformat(timespec="milliseconds"),
            "event": event,
        }
        if self._session_id:
            entry["session"] = self._session_id
        for key, value in fields.items():
            if value is None:
                continue
            entry[key] = _clip(value, redacting=True)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self._path_for(now).open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return

    def tool_call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        ok: bool | None = None,
        summary: str = "",
        reason: str = "",
        duration_ms: int = 0,
        agent_mode: str = "build",
    ) -> None:
        """Records one tool invocation with its outcome."""
        self.record(
            "tool",
            tool=tool,
            args=arguments or {},
            ok=ok,
            summary=summary,
            reason=reason,
            duration_ms=duration_ms,
            agent_mode=agent_mode,
        )

    def permission(
        self,
        action: str,
        resource: str,
        decision: str,
        reason: str = "",
        *,
        tool: str = "",
    ) -> None:
        self.record(
            "permission",
            action=action,
            resource=resource,
            decision=decision,
            reason=reason,
            tool=tool or None,
        )

    def read_entries(self, day: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Reads the most recent entries (newest last), optionally for one day."""
        if not self.dir.exists():
            return []
        if day:
            paths = [self.dir / f"{day}.jsonl"]
        else:
            paths = sorted(self.dir.glob("*.jsonl"))[-3:]
        entries: list[dict[str, Any]] = []
        for path in paths:
            if not path.is_file():
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except OSError:
                continue
        return entries[-limit:]

    def summary(self, day: str | None = None) -> dict[str, Any]:
        """Counts entries by event and by tool for a quick overview."""
        entries = self.read_entries(day, limit=100_000)
        by_event: dict[str, int] = {}
        by_tool: dict[str, int] = {}
        failures = 0
        for entry in entries:
            event = str(entry.get("event") or "?")
            by_event[event] = by_event.get(event, 0) + 1
            tool = entry.get("tool")
            if isinstance(tool, str):
                by_tool[tool] = by_tool.get(tool, 0) + 1
            if entry.get("ok") is False or entry.get("decision") == "deny":
                failures += 1
        return {
            "entries": len(entries),
            "by_event": by_event,
            "by_tool": by_tool,
            "failures": failures,
            "dir": str(self.dir),
        }


def default_audit_log(workspace: Path, config: dict[str, Any] | None = None) -> AuditLog:
    """Builds a log honouring `audit_log` (default on) from the config."""
    config = config or {}
    enabled = config.get("audit_log")
    if enabled is None:
        enabled = True
    return AuditLog(workspace, enabled=bool(enabled) and os.environ.get("RBIT_AUDIT_LOG", "1") != "0")
