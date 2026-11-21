"""Installing skills and plugins from a path or Git URL — with an audit trail.

Both features in the blueprint ("publish/install a skill from a Git URL" and
"install/update/disable/remove plugins") are supply-chain operations: whatever
lands in `.rbit/skills` or `.rbit/plugins` runs inside the agent. So installation
records where the code came from, refuses to overwrite a locally modified
install without `--force`, and never executes anything during installation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GIT_URL_RE = re.compile(r"^(https://|git@|ssh://)")
#: Schemes that are refused outright (unauthenticated / local-file transports).
_REFUSED_URL_RE = re.compile(r"^(http://|git://|file://|ext::|-)", re.IGNORECASE)

#: Files that make up a skill; anything else in the source is ignored.
SKILL_FILES = ("SKILL.md", "skill.md")
MANIFEST_NAME = "plugin.json"


class InstallError(RuntimeError):
    """Raised for a refused or failed installation."""


@dataclass
class InstallResult:
    name: str
    path: Path
    source: str
    kind: str
    replaced: bool = False

    def describe(self) -> str:
        verb = "replaced" if self.replaced else "installed"
        return f"{verb} {self.kind} `{self.name}` from {self.source} → {self.path}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _origin_path(target_dir: Path) -> Path:
    # One file per kind: skills and plugins used to share `.rbit/.origin.json`,
    # so a same-named skill and plugin overwrote each other's origin.
    return target_dir / ".origin.json"


def _legacy_origin_path(target_dir: Path) -> Path:
    return target_dir.parent / ".origin.json"


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        # A BOM here would make every installed item look unmanaged.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def read_origins(target_dir: Path) -> dict[str, Any]:
    kind = "skill" if target_dir.name == "skills" else "plugin"
    legacy = {
        name: meta
        for name, meta in _load_json_dict(_legacy_origin_path(target_dir)).items()
        if isinstance(meta, dict) and meta.get("kind") == kind
    }
    legacy.update(_load_json_dict(_origin_path(target_dir)))
    return legacy


def write_origin(target_dir: Path, name: str, source: str, kind: str, digest: str = "") -> None:
    data = read_origins(target_dir)
    data[name] = {
        "source": source,
        "kind": kind,
        "installed_at": _now(),
        "digest": digest,
        # Remote installs are flagged so the UI can warn about their provenance.
        "remote": bool(GIT_URL_RE.match(source)),
    }
    try:
        _origin_path(target_dir).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    """Remove a directory tree; read-only files are made writable; failures are loud."""

    def _onerror(func: Any, target: str, _exc: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)
    if path.exists():
        raise InstallError(f"could not remove {path}; close programs using it and retry")


def _is_link_like(path: Path) -> bool:
    """Symlinks AND Windows junctions / reparse points: all of them can lead outside the source.

    `Path.is_symlink()` is False for a junction (`mklink /J`, no admin rights needed), and
    copytree follows it, which copied the contents of an arbitrary outside folder into the
    install, where the agent then reads it.
    """
    try:
        if path.is_symlink():
            return True
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _ignore_links_and_git(directory: str, names: list[str]) -> list[str]:
    """copytree ignore: never copy links/junctions (they could point outside the source) or .git."""
    return [n for n in names if n == ".git" or _is_link_like(Path(directory) / n)]


def _name_from_source(source: str) -> str:
    tail = re.split(r"[/:\\]", source.rstrip("/\\"))[-1]
    return re.sub(r"\.git$", "", tail)


def fetch_source(source: str, workdir: Path, *, timeout: float = 60.0) -> Path:
    """Materialises `source` locally: a git URL is cloned, a path is copied."""
    if _REFUSED_URL_RE.match(source):
        raise InstallError("only https:// and ssh git URLs (or a local path) are accepted as a source")
    if GIT_URL_RE.match(source):
        dest = workdir / "source"
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", "--", source, str(dest)],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError(f"git clone failed: {exc}") from exc
        if proc.returncode != 0:
            raise InstallError(f"git clone failed: {proc.stderr.strip()[:300]}")
        return dest
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise InstallError(f"source not found: {source}")
    return path


def _find_skill_entry(root: Path) -> Path | None:
    for name in SKILL_FILES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    # A repo may keep skills under skills/<name>/SKILL.md.
    for candidate in sorted(root.glob("*/S*")):
        if candidate.is_file() and candidate.name.lower() == "skill.md":
            return candidate
    return None


def install_skill(
    source: str,
    *,
    workspace: Path,
    name: str = "",
    force: bool = False,
) -> InstallResult:
    """Installs a skill from a Git URL or a local path into `.rbit/skills`."""
    skills_dir = workspace / ".rbit" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rbit-skill-") as tmp:
        root = fetch_source(source, Path(tmp))
        entry = _find_skill_entry(root)
        if entry is None:
            raise InstallError(
                "no SKILL.md found at the source root or one directory below"
            )
        if entry.is_symlink():
            raise InstallError("SKILL.md must be a regular file, not a symlink")
        # Name priority: explicit --name, then the directory holding SKILL.md.
        # When SKILL.md sits at the source root, the root directory names it —
        # using the file stem would produce the generic name "SKILL".
        if name.strip():
            skill_name = name.strip()
        elif entry.parent != root:
            skill_name = entry.parent.name
        elif GIT_URL_RE.match(source):
            # A clone lands in a generic "source" dir; name the skill after the repo.
            skill_name = _name_from_source(source) or entry.stem
        else:
            skill_name = root.name or entry.stem
        # A leading dot would let `.` / `..` name the skills dir (or `.rbit`) itself,
        # and the replace branch below deletes the target directory.
        skill_name = re.sub(r"[^A-Za-z0-9._-]+", "-", skill_name).lstrip(".") or "skill"
        target_dir = skills_dir / skill_name
        if target_dir.resolve().parent != skills_dir.resolve():
            raise InstallError(f"invalid skill name `{skill_name}`")
        if target_dir.exists():
            existing = target_dir / "SKILL.md"
            changed = existing.is_file() and _digest(existing) != _digest(entry)
            if changed and not force:
                raise InstallError(
                    f"skill `{skill_name}` already exists and differs from the source; "
                    "re-run with force to replace it"
                )
            replaced = True
            _rmtree(target_dir)
        else:
            replaced = False
        target_dir.mkdir(parents=True, exist_ok=True)
        # Copy the skill file plus any sibling references, but nothing executable.
        shutil.copy2(entry, target_dir / "SKILL.md")
        for sibling in entry.parent.iterdir():
            if sibling == entry or sibling == target_dir:
                continue
            if _is_link_like(sibling):
                continue
            if sibling.is_dir() and (sibling.name in {"assets", "references", "templates"}):
                shutil.copytree(
                    sibling, target_dir / sibling.name, dirs_exist_ok=True, ignore=_ignore_links_and_git
                )
        write_origin(skills_dir, skill_name, source, "skill", _digest(target_dir / "SKILL.md"))
    return InstallResult(skill_name, target_dir, source, "skill", replaced)


_PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _require_plugin_name(name: str) -> None:
    """Rejects names that could escape `.rbit/plugins` (`.`, `..`, separators)."""
    if not _PLUGIN_NAME_RE.fullmatch(name or "") or ".." in name:
        raise InstallError(
            "plugin name must start with a letter or digit and may only contain "
            "letters, digits, dot, dash, underscore"
        )


def validate_plugin_manifest(root: Path) -> dict[str, Any]:
    """Reads and checks `plugin.json`; raises when it is unusable."""
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise InstallError(f"missing {MANIFEST_NAME} in the plugin source")
    try:
        # `utf-8-sig` strips a BOM: Windows editors routinely write one, and a
        # manifest that parses everywhere except rbit would look like our bug.
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise InstallError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{MANIFEST_NAME} must contain a JSON object")
    name = str(data.get("name") or "").strip()
    if not name:
        raise InstallError(f"{MANIFEST_NAME} must declare a `name`")
    _require_plugin_name(name)
    entry = str(data.get("entry") or "").strip()
    if entry and not (root / entry).is_file():
        raise InstallError(f"declared entry `{entry}` does not exist in the source")
    permissions = data.get("permissions")
    if permissions is not None and not isinstance(permissions, list):
        raise InstallError("`permissions` must be a list of strings")
    return data


def install_plugin(
    source: str,
    *,
    workspace: Path,
    force: bool = False,
    approve: bool = False,
) -> InstallResult:
    """Installs a plugin into `.rbit/plugins/<name>`, disabled until approved."""
    plugins_dir = workspace / ".rbit" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rbit-plugin-") as tmp:
        root = fetch_source(source, Path(tmp))
        manifest = validate_plugin_manifest(root)
        name = str(manifest["name"])
        target_dir = plugins_dir / name
        replaced = target_dir.exists()
        if replaced and not force:
            raise InstallError(
                f"plugin `{name}` is already installed; re-run with force to replace it"
            )
        if replaced:
            _rmtree(target_dir)
        shutil.copytree(root, target_dir, dirs_exist_ok=True, ignore=_ignore_links_and_git)
        write_origin(plugins_dir, name, source, "plugin", "")
    if approve:
        _set_plugin_enabled(workspace, name, True)
    else:
        # The blueprint requires an explicit step before third-party code runs.
        _set_plugin_enabled(workspace, name, False)
    return InstallResult(name, target_dir, source, "plugin", replaced)


def _plugins_config_path(workspace: Path) -> Path:
    return workspace / ".rbit" / "config.json"


def _read_config(workspace: Path) -> dict[str, Any]:
    path = _plugins_config_path(workspace)
    if not path.is_file():
        return {}
    try:
        # `utf-8-sig` so a BOM written by a Windows editor cannot hide the config.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_config(workspace: Path, config: dict[str, Any]) -> None:
    path = _plugins_config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _set_plugin_enabled(workspace: Path, name: str, enabled: bool) -> None:
    from .storage import StorageManager

    storage = StorageManager(workspace)
    # Start from the plugins the user has TRUSTED, not from the raw file: a repository's own
    # plugin list must not be swept into the trusted list by enabling one plugin.
    config = storage.load_config()
    entries = [e for e in (config.get("plugins") or []) if isinstance(e, str)]
    rel = f".rbit/plugins/{name}"
    if enabled and rel not in entries:
        entries.append(rel)
    if not enabled:
        entries = [e for e in entries if Path(e).name != name and e != rel]
    # The user just enabled/disabled this plugin: write the list and trust it.
    storage.set_setting("plugins", entries)


def set_plugin_enabled(workspace: Path, name: str, enabled: bool) -> None:
    _require_plugin_name(name)
    target = workspace / ".rbit" / "plugins" / name
    if not target.is_dir():
        raise InstallError(f"plugin `{name}` is not installed")
    _set_plugin_enabled(workspace, name, enabled)


def remove_plugin(workspace: Path, name: str) -> None:
    _require_plugin_name(name)  # `rbit plugin remove ../..` must not delete the workspace
    target = workspace / ".rbit" / "plugins" / name
    if not target.is_dir():
        raise InstallError(f"plugin `{name}` is not installed")
    _set_plugin_enabled(workspace, name, False)
    _rmtree(target)
    data = read_origins(target.parent)
    data.pop(name, None)
    try:
        _origin_path(target.parent).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def list_installed(workspace: Path) -> list[dict[str, Any]]:
    """Lists installed skills and plugins with their recorded origin."""
    rows: list[dict[str, Any]] = []
    for kind, sub in (("skill", "skills"), ("plugin", "plugins")):
        base = workspace / ".rbit" / sub
        if not base.is_dir():
            continue
        origins = read_origins(base)
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            origin = origins.get(entry.name) or {}
            rows.append(
                {
                    "kind": kind,
                    "name": entry.name,
                    "path": str(entry),
                    "source": origin.get("source", "local"),
                    "remote": bool(origin.get("remote")),
                    "installed_at": origin.get("installed_at", ""),
                }
            )
    return rows


def verify_sources(workspace: Path) -> list[str]:
    """Warns about skills/plugins whose file contents changed after install."""
    warnings: list[str] = []
    skills = workspace / ".rbit" / "skills"
    if skills.is_dir():
        origins = read_origins(skills)
        for name, meta in origins.items():
            digest = str(meta.get("digest") or "")
            target = skills / name / "SKILL.md"
            if digest and target.is_file() and _digest(target) != digest:
                warnings.append(f"skill `{name}` changed on disk since install")
            if meta.get("remote"):
                warnings.append(f"skill `{name}` came from a remote source: {meta.get('source')}")
    return warnings
