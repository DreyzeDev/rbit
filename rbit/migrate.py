"""Config migration and diagnostic bundle collection.

Two operational needs from the blueprint:

* **Migrations with a backup** — rbit's config has grown over several versions
  (`budget_usd` -> `budget_mode` + `daily_budget_usd`, `permission` -> a mode
  plus rules, new top-level blocks). Silently rewriting a user's config is not
  acceptable, so every migration writes a timestamped backup first and is a no-op
  when there is nothing to do.
* **A diagnostic bundle with secrets redacted** — when something breaks, the
  useful information is the environment, the effective config and the last
  errors. The dangerous part is the API key inside the config, so the bundle is
  built through the same redaction used by the audit log.
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit import redact

#: Config keys that must never appear in a bundle, even partially.
ALWAYS_HIDDEN = {"api_key", "token", "password", "secret", "auth", "key"}


@dataclass
class MigrationResult:
    """What a migration changed, and where the backup went."""

    changes: list[str] = field(default_factory=list)
    backup: Path | None = None
    dry_run: bool = False

    def summary(self) -> str:
        if not self.changes:
            return "config already up to date — nothing to migrate"
        verb = "would change" if self.dry_run else "migrated"
        return f"{verb} {len(self.changes)} setting(s)"


def _backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.stem}.backup-{stamp}{path.suffix}")


def _migrate_values(config: dict[str, Any]) -> list[str]:
    """Applies known upgrades in place; returns human-readable changes."""
    changes: list[str] = []

    # `auto_approve: true` predates permission modes.
    if config.get("auto_approve") and not config.get("permission_mode"):
        config["permission_mode"] = "auto"
        changes.append("auto_approve -> permission_mode: auto")

    # A bare `budget_usd` was per-session; map it to a daily cap + mode.
    legacy_budget = config.pop("budget_usd", None)
    if isinstance(legacy_budget, (int, float)) and legacy_budget > 0:
        config.setdefault("daily_budget_usd", float(legacy_budget))
        config.setdefault("budget_mode", "strict")
        changes.append(
            f"budget_usd: {legacy_budget} -> daily_budget_usd: {float(legacy_budget)} "
            "+ budget_mode: strict"
        )

    # The old `permission` mapping is still honoured, but a mode is clearer.
    legacy_perm = config.get("permission")
    if isinstance(legacy_perm, dict) and not config.get("permissions"):
        config["permissions"] = [
            {"action": str(action), "resource": "*", "effect": str(effect)}
            for action, effect in legacy_perm.items()
            if isinstance(effect, str)
        ]
        changes.append(f"permission: {{...}} -> permissions: [{len(config['permissions'])} rule(s)]")

    # New safety defaults: make them explicit so users can see and change them.
    defaults: list[tuple[str, Any]] = [
        ("audit_log", True),
        ("prompt_injection_guard", "warn"),
        ("budget_mode", "warn"),
    ]
    for key, value in defaults:
        if key not in config:
            config[key] = value
            changes.append(f"added {key}: {value!r}")

    router = config.get("model_router")
    if isinstance(router, dict) and "policy" not in router:
        router["policy"] = "balanced"
        changes.append("model_router.policy: balanced")

    hooks = config.get("hooks")
    if isinstance(hooks, dict) and "timeout_s" not in hooks:
        hooks["timeout_s"] = 8.0
        changes.append("hooks.timeout_s: 8.0")

    if "config_version" not in config:
        config["config_version"] = 2
        changes.append("config_version: 2")

    return changes


def migrate_config(workspace: Path, *, dry_run: bool = False) -> MigrationResult:
    """Upgrades the workspace config, backing it up first."""
    path = Path(workspace) / ".rbit" / "config.json"
    result = MigrationResult(dry_run=dry_run)
    if not path.is_file():
        return result
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return result
    if not isinstance(config, dict):
        return result

    result.changes = _migrate_values(config)
    if not result.changes or dry_run:
        return result

    try:
        backup = _backup_path(path)
        shutil.copy2(path, backup)
        result.backup = backup
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        result.changes = []
    return result


def _scrub(value: Any, key: str = "") -> Any:
    """Recursively redacts config values whose key looks sensitive."""
    lowered = key.lower()
    if any(token in lowered for token in ALWAYS_HIDDEN):
        if isinstance(value, str) and not value.strip():
            return ""
        return "***REDACTED***"
    if isinstance(value, dict):
        return {k: _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, key) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value


def diagnostic_bundle(workspace: Path) -> str:
    """Builds a redacted text bundle describing the environment."""
    from . import __version__

    ws = Path(workspace)
    lines: list[str] = [
        "# rbit diagnostic bundle",
        f"created: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## environment",
        f"rbit: {__version__}",
        f"python: {sys.version.split()[0]}",
        f"platform: {platform.system()} {platform.release()} ({platform.machine()})",
        f"workspace: {ws}",
        f"git: {shutil.which('git') or 'not found'}",
        f"ruff: {shutil.which('ruff') or 'not found'}",
        "",
    ]

    config_path = ws / ".rbit" / "config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            lines.append("## config (secrets redacted)")
            lines.append(json.dumps(_scrub(config), ensure_ascii=False, indent=2))
        except (json.JSONDecodeError, OSError) as exc:
            lines.append(f"## config\nunreadable: {exc}")
    else:
        lines.append("## config\n<not present>")
    lines.append("")

    try:
        from .rules import discover_instruction_sources

        lines.append("## instructions")
        sources = discover_instruction_sources(ws)
        lines.append(", ".join(sources) if sources else "<none>")
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail
        lines.append(f"## instructions\nunavailable: {exc}")
    lines.append("")

    try:
        from .selftest import self_test

        report = self_test(ws)
        lines.append("## self-test")
        lines.append(report.text())
    except Exception as exc:  # noqa: BLE001
        lines.append(f"## self-test\nunavailable: {exc}")
    lines.append("")

    audit_dir = ws / ".rbit" / "audit"
    if audit_dir.is_dir():
        lines.append("## recent actions")
        try:
            from .audit import AuditLog

            log = AuditLog(ws)
            for entry in log.read_entries(limit=15):
                event = entry.get("event")
                detail = entry.get("tool") or entry.get("source") or entry.get("action") or ""
                outcome = entry.get("ok")
                suffix = "" if outcome is None else (" ok" if outcome else " failed")
                lines.append(f"{entry.get('ts', '')} {event} {detail}{suffix}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"unavailable: {exc}")
    else:
        lines.append("## recent actions\n<no audit log>")

    return "\n".join(lines)
