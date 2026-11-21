"""Trust for a project's `.rbit/config.json`.

A cloned repository can ship its own `.rbit/config.json`. Most of it is harmless (theme, model,
budgets), but some settings run programs or widen what the agent may do: MCP servers and LSP
servers are started as subprocesses, plugins are imported Python code, hooks and formatters run
commands, `permission_mode: auto` switches the confirmations off, `api_base` decides where the
API key is sent, `instructions` can pull files such as `~/.ssh/id_rsa` into the prompt.

Those settings only take effect for a workspace once the user trusts them:

* the trust record lives in the USER's profile (`~/.rbit/trust.json`), never in the project;
* it is kept per workspace and per setting, as a fingerprint of the value, so a changed value
  is untrusted again and one trusted key does not trust another;
* values written by rbit itself on the user's behalf (`rbit mcp add`, `/connect`, `config set`,
  `plugin enable`) are trusted automatically, because the user just asked for them;
* `rbit trust` shows what is in the file and trusts it after confirmation.

Everything else in the file keeps working untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

#: Settings that are ignored until trusted (the whole key).
STRIP_KEYS: tuple[str, ...] = (
    "mcp",
    "mcp_servers",
    "lsp",
    "plugins",
    "hooks",
    "formatter",
    "permission_mode",
    "auto_approve",
    "allowed_bash_prefixes",
    "allow_outside_paths",
    "allow_secret_reads",
    "webfetch_allow_private",
    "acp_allow_all",
    "prompt_injection_guard",
    "instructions",
    "api_base",
    "extra_headers",
    "protocol",
    "server_token",
)

#: For these, a value that only narrows things is harmless, so it does not need trust.
SAFE_VALUES: dict[str, Any] = {
    "auto_approve": False,
    "allow_outside_paths": False,
    "allow_secret_reads": False,
    "webfetch_allow_private": False,
    "acp_allow_all": False,
    "prompt_injection_guard": True,
    "permission_mode": "ask",
}

#: Permission rule containers: only their `allow` rules need trust (deny/ask only narrow).
PERMISSION_KEYS: tuple[str, ...] = ("permissions", "permission", "agents")

PROFILES_KEY = "profiles"

TRUST_ALL_ENV = "RBIT_TRUST_ALL"
TRUST_STORE_ENV = "RBIT_TRUST_STORE"


def fingerprint(value: Any) -> str:
    """Stable fingerprint of a JSON value (key order does not matter)."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def trust_all_requested() -> bool:
    """`RBIT_TRUST_ALL=1` (set by the user, e.g. in CI) trusts every project config."""
    return (os.environ.get(TRUST_ALL_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ classification


def _is_risky_scalar(key: str, value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if key in SAFE_VALUES and value == SAFE_VALUES[key]:
        return False
    return True


def _allow_only(value: Any) -> Any:
    """`value` without its allow rules (what stays active while the key is untrusted)."""
    if isinstance(value, list):
        return [e for e in value if not (isinstance(e, dict) and e.get("effect") == "allow")]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, dict):
                kept = {r: e for r, e in v.items() if e != "allow"}
                if kept:
                    out[k] = kept
            elif v != "allow":
                out[k] = v
        return out
    return value


def _agents_without_allow(agents: Any) -> Any:
    if not isinstance(agents, dict):
        return agents
    out: dict[str, Any] = {}
    for name, cfg in agents.items():
        if isinstance(cfg, dict) and isinstance(cfg.get("permissions"), list):
            cfg = {**cfg, "permissions": _allow_only(cfg["permissions"])}
        out[name] = cfg
    return out


def _has_allow(key: str, value: Any) -> bool:
    if key == "agents":
        return _agents_without_allow(value) != value
    return _allow_only(value) != value


def _profile_dangers(profiles: Any) -> dict[str, dict[str, Any]]:
    """The risky settings inside profile overlays: {profile: {key: value}}."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(profiles, dict):
        return out
    for name, overlay in profiles.items():
        if not isinstance(overlay, dict):
            continue
        risky = {k: overlay[k] for k in STRIP_KEYS if k in overlay and _is_risky_scalar(k, overlay[k])}
        for k in PERMISSION_KEYS:
            if k in overlay and _has_allow(k, overlay[k]):
                risky[k] = overlay[k]
        if risky:
            out[str(name)] = risky
    return out


def risky_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Every setting in `raw` that needs trust, as {key: value-as-written}."""
    found: dict[str, Any] = {}
    for key in STRIP_KEYS:
        if key in raw and _is_risky_scalar(key, raw[key]):
            found[key] = raw[key]
    for key in PERMISSION_KEYS:
        if key in raw and _has_allow(key, raw[key]):
            found[key] = raw[key]
    profiles = _profile_dangers(raw.get(PROFILES_KEY))
    if profiles:
        found[PROFILES_KEY] = profiles
    return found


def split_config(raw: dict[str, Any], trusted: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Returns (config with every untrusted risky setting removed, names of the removed settings)."""
    clean = dict(raw)
    untrusted: list[str] = []
    for key, value in risky_settings(raw).items():
        if trusted.get(key) == fingerprint(value):
            continue
        untrusted.append(key)
        if key == PROFILES_KEY:
            profiles = {}
            for name, overlay in (raw.get(PROFILES_KEY) or {}).items():
                if isinstance(overlay, dict):
                    overlay = {k: v for k, v in overlay.items() if k not in STRIP_KEYS}
                    for pk in PERMISSION_KEYS:
                        if pk in overlay:
                            overlay[pk] = _agents_without_allow(overlay[pk]) if pk == "agents" else _allow_only(overlay[pk])
                profiles[name] = overlay
            clean[PROFILES_KEY] = profiles
        elif key in PERMISSION_KEYS:
            clean[key] = _agents_without_allow(value) if key == "agents" else _allow_only(value)
        else:
            clean.pop(key, None)
    return clean, sorted(untrusted)


def describe(key: str, value: Any) -> str:
    """One readable line for `rbit trust`: what the setting would do."""
    if key in ("mcp", "mcp_servers") and isinstance(value, dict):
        parts = []
        for name, entry in value.items():
            if isinstance(entry, dict):
                target = entry.get("url") or " ".join(map(str, entry.get("command") or [])) or "?"
                parts.append(f"{name} -> {target}")
            else:
                parts.append(f"{name} -> {entry}")
        return "starts MCP servers: " + "; ".join(parts)
    if key == "plugins":
        return "imports and runs plugin code: " + ", ".join(map(str, value if isinstance(value, list) else [value]))
    if key == "lsp":
        return "starts language servers: " + json.dumps(value, ensure_ascii=False)[:160]
    if key == "hooks":
        return "runs hook commands: " + json.dumps(value, ensure_ascii=False)[:160]
    if key == "formatter":
        return "runs formatter commands after file writes: " + json.dumps(value, ensure_ascii=False)[:160]
    if key == "instructions":
        return "adds these files/URLs to the system prompt (sent to the model): " + json.dumps(value, ensure_ascii=False)[:160]
    if key == "api_base":
        return f"sends your API key to {value}"
    if key == "permission_mode":
        return f"sets the permission mode to {value!r} (fewer confirmations)"
    if key in ("permissions", "permission", "agents"):
        return "adds allow rules (actions that no longer ask): " + json.dumps(_allow_rules_only(key, value), ensure_ascii=False)[:160]
    if key == PROFILES_KEY:
        return "profile overlays with risky settings: " + json.dumps(value, ensure_ascii=False)[:160]
    return f"sets {key} = " + json.dumps(value, ensure_ascii=False)[:120]


def _allow_rules_only(key: str, value: Any) -> Any:
    if isinstance(value, list):
        return [e for e in value if isinstance(e, dict) and e.get("effect") == "allow"]
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if v == "allow" or isinstance(v, (dict, list))}
    return value


# ------------------------------------------------------------------------- store


class TrustStore:
    """The user-level record of which project settings were trusted."""

    def __init__(self, path: Path | None = None) -> None:
        override = os.environ.get(TRUST_STORE_ENV)
        self.path = Path(override) if override else (path or Path.home() / ".rbit" / "trust.json")

    @staticmethod
    def workspace_key(workspace: Path) -> str:
        key = str(Path(workspace).resolve())
        return key.casefold() if os.name == "nt" else key

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def trusted_for(self, workspace: Path) -> dict[str, str]:
        entry = (self._read().get("workspaces") or {}).get(self.workspace_key(workspace)) or {}
        return {str(k): str(v) for k, v in entry.items()} if isinstance(entry, dict) else {}

    def trust(self, workspace: Path, fingerprints: dict[str, str]) -> None:
        if not fingerprints:
            return
        data = self._read()
        workspaces = data.get("workspaces")
        if not isinstance(workspaces, dict):
            workspaces = {}
        key = self.workspace_key(workspace)
        entry = workspaces.get(key) if isinstance(workspaces.get(key), dict) else {}
        entry.update(fingerprints)
        workspaces[key] = entry
        data["workspaces"] = workspaces
        self._write(data)

    def forget(self, workspace: Path, keys: list[str] | None = None) -> bool:
        """Removes the record (or only `keys`) for a workspace; True when something was removed."""
        data = self._read()
        workspaces = data.get("workspaces")
        key = self.workspace_key(workspace)
        if not isinstance(workspaces, dict) or key not in workspaces:
            return False
        if keys is None:
            del workspaces[key]
        else:
            entry = workspaces[key]
            if not isinstance(entry, dict) or not any(k in entry for k in keys):
                return False
            for k in keys:
                entry.pop(k, None)
        self._write(data)
        return True

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".trust.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2, ensure_ascii=False))
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
