"""Permission modes: quick presets over the ordered allow/ask/deny rule engine.

The rule engine (`ToolExecutor.resolve_permission_effect`) decides *what* the
policy is for an (action, resource) pair. A mode only decides what happens when
the answer would otherwise be a prompt — it never overrides an explicit `deny`
written by the user, and it is the only thing that can silence a prompt the
engine asked for.
"""

from __future__ import annotations

import os
from typing import Any

#: (id, label, description) — shown by `/permissions` and `rbit permissions mode`.
PERMISSION_MODES: tuple[tuple[str, str, str], ...] = (
    ("ask", "Ask everything", "Prompt before every file edit and shell command"),
    ("commands", "Commands only", "Shell commands auto-approved, file edits still prompt"),
    ("edits", "Edits only (default)", "File edits auto-approved, shell commands still prompt"),
    ("auto", "Auto - allow everything", "No prompts: edits, commands and paths outside the workspace"),
)

MODE_IDS: tuple[str, ...] = tuple(mode[0] for mode in PERMISSION_MODES)

DEFAULT_MODE = "edits"

#: Accepted aliases so `--permissions yolo`, `/permissions все` style input works.
_MODE_ALIASES: dict[str, str] = {
    "default": "edits",
    "normal": "edits",
    "edits-only": "edits",
    "edit": "edits",
    "write": "edits",
    "strict": "ask",
    "paranoid": "ask",
    "always-ask": "ask",
    "only-commands": "commands",
    "commands-only": "commands",
    "cmd": "commands",
    "shell": "commands",
    "yolo": "auto",
    "all": "auto",
    "everything": "auto",
    "allow-all": "auto",
    "allowall": "auto",
    "danger": "auto",
    "dangerously-skip-permissions": "auto",
    "авто": "auto",
    "всё": "auto",
    "все": "auto",
    "команды": "commands",
    "спрашивать": "ask",
}

#: Actions grouped by what they mutate. Used to map a mode onto an action.
#: `external_directory` is deliberately separate: writing outside the workspace
#: stays a prompt even in the default mode, and only `auto` silences it.
EDIT_ACTIONS: frozenset[str] = frozenset({"edit"})
EXTERNAL_ACTIONS: frozenset[str] = frozenset({"external_directory"})
COMMAND_ACTIONS: frozenset[str] = frozenset({"shell"})


def normalize_permission_mode(raw: Any, default: str = DEFAULT_MODE) -> str:
    """Coerces any user/config input into a valid mode id."""
    if isinstance(raw, str):
        # Accept the CLI spelling too: `--yolo`, `--dangerously-skip-permissions`.
        key = raw.strip().lower().lstrip("-").replace("_", "-")
        key = _MODE_ALIASES.get(key, key)
        if key in MODE_IDS:
            return key
    return default


def mode_decision(mode: str, action: str) -> str | None:
    """Returns ``"allow"``/``"ask"`` for `action` under `mode`, or None if N/A.

    None means "this mode has no opinion", i.e. the engine's answer stands.
    """
    mode = normalize_permission_mode(mode)
    if action in COMMAND_ACTIONS:
        return "allow" if mode in ("commands", "auto") else "ask"
    if action in EDIT_ACTIONS:
        return "allow" if mode in ("edits", "auto") else "ask"
    if action in EXTERNAL_ACTIONS:
        # Only `auto` has an opinion: it opens paths outside the workspace.
        # Every other mode stays silent so the engine's own default applies
        # (no rule -> the path resolver reports "outside workspace"), which
        # keeps modes from inventing a block that was not there before.
        return "allow" if mode == "auto" else None
    return None


def permission_mode_from_config(
    cfg: dict[str, Any] | None,
    env: dict[str, str] | None = None,
) -> str:
    """Resolves the effective mode: env override > legacy auto flags > config."""
    env = os.environ if env is None else env
    cfg = cfg or {}

    override = env.get("RBIT_PERMISSION_MODE")
    if isinstance(override, str) and override.strip():
        return normalize_permission_mode(override)

    # `RBIT_AUTO_APPROVE=1` (the --auto/--yolo flag) and `auto_approve` in the
    # config both predate modes and mean exactly "never prompt".
    if env.get("RBIT_AUTO_APPROVE") == "1" or config_flag(cfg.get("auto_approve")):
        return "auto"

    return normalize_permission_mode(cfg.get("permission_mode"), DEFAULT_MODE)


def config_flag(value: object) -> bool:
    """A boolean setting that may have been written by hand or by `config set` as a string.

    `"false"`, `"no"`, `"off"`, `"0"` and `""` are False; a plain truthiness test treated them
    as True, so `auto_approve: "no"` silently switched every confirmation off.
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none", "null")
    return bool(value)


def describe_permission_mode(mode: str) -> str:
    """One-line human summary of what a mode auto-approves."""
    mode = normalize_permission_mode(mode)
    for mode_id, label, description in PERMISSION_MODES:
        if mode_id == mode:
            return f"{label} - {description}"
    return mode


def mode_label(mode: str) -> str:
    mode = normalize_permission_mode(mode)
    for mode_id, label, _description in PERMISSION_MODES:
        if mode_id == mode:
            return label
    return mode


def mode_short_label(mode: str) -> str:
    """Compact sidebar form: `perms: auto`, `perms: ask`."""
    return f"perms: {normalize_permission_mode(mode)}"


def mode_table() -> list[tuple[str, str, str, bool, bool]]:
    """Matrix for the modal/CLI: (id, label, description, commands_auto, edits_auto)."""
    rows: list[tuple[str, str, str, bool, bool]] = []
    for mode_id, label, description in PERMISSION_MODES:
        rows.append(
            (
                mode_id,
                label,
                description,
                mode_decision(mode_id, "shell") == "allow",
                mode_decision(mode_id, "edit") == "allow",
            )
        )
    return rows
