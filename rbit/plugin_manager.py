"""Package-style plugin management for the CLI (`rbit plugin ...`).

Supports local files/directories, git repositories, and importable Python
package names.  Installed paths are registered in `.rbit/plugins/registry.json`
and appended to the workspace `plugins` config so the runtime loader picks them up.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


class PluginManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".rbit" / "plugins"
        self.packages = self.root / "packages"
        self.registry_path = self.root / "registry.json"

    def _read_registry(self) -> list[dict[str, Any]]:
        if not self.registry_path.exists():
            return []
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write_registry(self, entries: list[dict[str, Any]], drop: list[str] | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        self._sync_config(entries, drop=drop)

    def _sync_config(self, entries: list[dict[str, Any]], drop: list[str] | None = None) -> None:
        from rbit.storage import StorageManager

        storage = StorageManager(self.workspace)
        config = storage.load_config()
        dropped = set(drop or [])
        configured = [
            item
            for item in config.get("plugins") or []
            if isinstance(item, str) and item not in dropped
        ]
        for entry in entries:
            target = str(entry.get("target") or "")
            if target and target not in configured:
                configured.append(target)
        config["plugins"] = configured
        storage.save_config(config)

    def list_plugins(self) -> list[dict[str, Any]]:
        entries = self._read_registry()
        for entry in entries:
            target = Path(str(entry.get("target") or "")).expanduser()
            entry["status"] = "installed" if target.exists() else "missing"
        return entries

    @staticmethod
    def _is_git(source: str) -> bool:
        return source.startswith(("git@", "git+")) or source.endswith(".git") or "github.com/" in source

    def _name_for(self, source: str) -> str:
        cleaned = source.rstrip("/\\")
        name = Path(cleaned).name or "plugin"
        name = name.removesuffix(".git")
        return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name)[:64] or "plugin"

    def add(self, source: str) -> tuple[bool, str]:
        source = source.strip()
        if not source:
            return False, "package specifier is required"
        entries = self._read_registry()
        if any(entry.get("source") == source for entry in entries):
            return False, f"already installed: {source}"
        self.packages.mkdir(parents=True, exist_ok=True)
        recording: dict[str, Any] = {"source": source, "installed_at": time.time()}
        if self._is_git(source):
            target = self.packages / self._name_for(source)
            if target.exists():
                return False, f"target exists: {target}"
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", source, str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                return False, (proc.stderr or proc.stdout).strip()[:400]
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(target), capture_output=True, text=True, check=False
            ).stdout.strip()
            recording.update({"target": str(target), "commit": commit})
        elif Path(source).expanduser().exists():
            origin = Path(source).expanduser().resolve()
            target = self.packages / origin.name
            if target.exists():
                return False, f"target exists: {target}"
            if origin.is_dir():
                shutil.copytree(origin, target)
            else:
                shutil.copy2(origin, target)
            recording.update({"target": str(target)})
        else:
            recording.update({"target": source, "kind": "package"})
        entries.append(recording)
        self._write_registry(entries)
        return True, f"installed {source} -> {recording['target']}"

    def check(self, target: str | None = None) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        for entry in self._read_registry():
            if target and target not in (entry.get("target"), entry.get("source")):
                continue
            source = str(entry.get("source") or "")
            path = Path(str(entry.get("target") or ""))
            if not self._is_git(source) or not path.exists():
                continue
            current = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True, text=True, check=False
            ).stdout.strip()
            remote = subprocess.run(
                ["git", "ls-remote", source, "HEAD"], capture_output=True, text=True, check=False
            ).stdout.split()
            latest = remote[0] if remote else ""
            if latest and current and latest != current:
                updates.append({"target": str(path), "current": current[:8], "latest": latest[:8]})
        return updates

    def update(self, target: str | None = None) -> list[str]:
        messages: list[str] = []
        entries = self._read_registry()
        for entry in entries:
            if target and target not in (entry.get("target"), entry.get("source")):
                continue
            source = str(entry.get("source") or "")
            path = Path(str(entry.get("target") or ""))
            if not self._is_git(source) or not path.exists():
                continue
            proc = subprocess.run(
                ["git", "pull", "--ff-only"], cwd=str(path), capture_output=True, text=True, check=False
            )
            if proc.returncode == 0:
                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True, text=True, check=False
                ).stdout.strip()
                entry["commit"] = commit
                messages.append(f"updated {path} -> {commit[:8]}")
            else:
                messages.append(f"update failed for {path}: {(proc.stderr or proc.stdout).strip()[:200]}")
        self._write_registry(entries)
        return messages or ["nothing to update"]

    def remove(self, source: str) -> tuple[bool, str]:
        entries = self._read_registry()
        remaining = [entry for entry in entries if source not in (entry.get("source"), entry.get("target"))]
        if len(remaining) == len(entries):
            return False, f"not installed: {source}"
        removed = [entry for entry in entries if entry not in remaining]
        removed_targets = [str(entry.get("target") or "") for entry in removed]
        for entry in removed:
            path = Path(str(entry.get("target") or ""))
            try:
                if path.is_relative_to(self.packages) and path.exists():
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
            except (OSError, ValueError):
                pass
        self._write_registry(remaining, drop=removed_targets)
        return True, f"removed {source}"
