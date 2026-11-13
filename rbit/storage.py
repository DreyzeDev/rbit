"""Persistent configuration, session storage, project memory, checkpoints, and Markdown export for rbit."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

from .models import CheckpointMeta, Session, short_id


class StorageManager:
    MAX_CHECKPOINTS = 20

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        self.state_dir = self.workspace / ".rbit"
        self.sessions_dir = self.state_dir / "sessions"
        self.checkpoints_dir = self.state_dir / "checkpoints"
        self.config_path = self.state_dir / "config.json"
        self.memory_path = self.state_dir / "memory.md"
        self.commands_dir = self.state_dir / "commands"

    def ensure_dirs(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def store_secret(self, name: str, value: str) -> bool:
        """Stores a secret in the OS keyring when the `keyring` package exists."""
        try:
            import keyring

            keyring.set_password("rbit", name, value)
            return True
        except Exception:
            return False

    def load_secret(self, name: str) -> str | None:
        try:
            import keyring

            return keyring.get_password("rbit", name)
        except Exception:
            return None

    def delete_secret(self, name: str) -> None:
        try:
            import keyring

            keyring.delete_password("rbit", name)
        except Exception:
            pass

    def resolve_api_key(self, cfg: dict[str, Any]) -> str:
        """Returns the API key, resolving the `keyring` placeholder from the OS keyring."""
        raw = str(cfg.get("api_key") or "").strip()
        if raw == "keyring":
            return (self.load_secret("api_key") or "").strip()
        return raw

    def acquire_lock(self) -> tuple[bool, int | None]:
        """Marks a running instance via .rbit/lock; returns (acquired, other_pid)."""
        import hashlib
        import tempfile

        key = hashlib.sha256(str(self.workspace).lower().encode("utf-8", errors="replace")).hexdigest()[:16]
        lock = Path(tempfile.gettempdir()) / f"rbit-lock-{key}.lock"
        self.ensure_dirs()
        pid = os.getpid()
        if lock.exists():
            try:
                other = int(lock.read_text(encoding="utf-8").strip() or 0)
                if other and other != pid and Path(f"/proc/{other}").exists() is False and os.name != "nt":
                    return False, other
                if other and other != pid and os.name == "nt":
                    # Windows: check the process exists via tasklist-free heuristic
                    import ctypes

                    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                    handle = kernel32.OpenProcess(0x100000, False, other)  # PROCESS_QUERY_LIMITED_INFORMATION
                    if handle:
                        kernel32.CloseHandle(handle)
                        return False, other
            except (ValueError, OSError):
                pass
        lock.write_text(str(pid), encoding="utf-8")
        return True, None

    def release_lock(self) -> None:
        try:
            import hashlib
            import tempfile

            key = hashlib.sha256(str(self.workspace).lower().encode("utf-8", errors="replace")).hexdigest()[:16]
            Path(tempfile.gettempdir(), f"rbit-lock-{key}.lock").unlink(missing_ok=True)
        except OSError:
            pass

    def load_config(self) -> dict[str, Any]:
        raw_base = (
            os.environ.get("RBIT_API_BASE")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://openrouter.ai/api/v1"
        ).rstrip("/")
        if raw_base.endswith("/chat/completions"):
            raw_base = raw_base[: -len("/chat/completions")]

        default_cfg = {
            "theme": "rbit-dark",
            "model": self._detect_default_model(),
            "api_base": raw_base,
            "api_key": os.environ.get("RBIT_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or "",
            "sidebar_visible": True,
            "auto_approve_safe": True,
            "allow_secret_reads": False,
        }
        if self.config_path.exists():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if loaded.get("theme") in ("opencode", "opencode-dark"):
                    loaded["theme"] = "rbit-dark"
                if loaded.get("theme") == "opencode-light":
                    loaded["theme"] = "rbit-light"
                if loaded.get("model") == "opencode/smart-local":
                    loaded["model"] = "rbit/smart-local"
                default_cfg.update({k: v for k, v in loaded.items() if v is not None})
            except Exception:
                pass

        profile = (os.environ.get("RBIT_PROFILE") or "").strip()
        if profile:
            profiles = default_cfg.get("profiles")
            if isinstance(profiles, dict) and isinstance(profiles.get(profile), dict):
                default_cfg.update(
                    {k: v for k, v in profiles[profile].items() if v is not None}
                )
        return default_cfg

    def _detect_default_model(self) -> str:
        if (
            os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("RBIT_API_KEY")
        ):
            return "anthropic/claude-sonnet-4.5"
        return "rbit/smart-local"

    def _atomic_write_json(self, path: Path, payload: str) -> None:
        """Writes via a temp file + os.replace: a crash mid-save never truncates
        the target into unreadable JSON (which list_sessions silently skips)."""
        self.ensure_dirs()
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    def save_config(self, cfg: dict[str, Any]) -> None:
        self._atomic_write_json(
            self.config_path,
            json.dumps(cfg, ensure_ascii=False, indent=2),
        )
        if os.name != "nt":
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass

    # --- Sessions ---
    def _use_sqlite(self) -> bool:
        return str(self.load_config().get("storage_backend") or "json").lower() == "sqlite"

    def _sqlite_path(self) -> Path:
        return self.state_dir / "sessions.db"

    def _sqlite_conn(self):
        import sqlite3

        conn = sqlite3.connect(self._sqlite_path())
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, updated REAL, data TEXT)"
        )
        return conn

    def _encrypt(self, text: str) -> str:
        """Fernet-encrypts a session payload when configured and a passphrase is set."""
        if not self.load_config().get("encrypt_sessions"):
            return text
        import base64
        import hashlib
        import os as _os

        passphrase = _os.environ.get("RBIT_PASSPHRASE")
        if not passphrase:
            return text
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            return text
        key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())
        token = Fernet(key).encrypt(text.encode("utf-8")).decode("ascii")
        return "rbit-enc:v1:" + token

    def _decrypt(self, stored: str) -> str:
        if not stored.startswith("rbit-enc:v1:"):
            return stored
        import base64
        import hashlib
        import os as _os

        passphrase = _os.environ.get("RBIT_PASSPHRASE")
        if not passphrase:
            raise ValueError("Session is encrypted; set RBIT_PASSPHRASE.")
        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())
        return Fernet(key).decrypt(stored.split(":", 2)[2].encode("ascii")).decode("utf-8")

    def save_session(self, session: Session) -> None:
        payload = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
        if self._use_sqlite():
            import time as _time

            conn = self._sqlite_conn()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sessions (id, updated, data) VALUES (?, ?, ?)",
                    (session.id, _time.time(), self._encrypt(payload)),
                )
            conn.close()
            return
        self._atomic_write_json(
            self.sessions_dir / f"{session.id}.json",
            self._encrypt(payload),
        )

    def _decode_session_file(self, raw: str) -> dict[str, Any]:
        return json.loads(self._decrypt(raw))

    def list_sessions(self) -> list[Session]:
        if not self.sessions_dir.exists():
            return []
        sessions: list[Session] = []
        for p in sorted(
            self.sessions_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                if self._use_sqlite():
                    break
                data = self._decode_session_file(p.read_text(encoding="utf-8"))
                sessions.append(Session.from_dict(data))
            except Exception:
                continue
        if self._use_sqlite():
            conn = self._sqlite_conn()
            try:
                for (raw,) in conn.execute(
                    "SELECT data FROM sessions ORDER BY updated DESC"
                ):
                    try:
                        sessions.append(Session.from_dict(self._decode_session_file(raw)))
                    except Exception:
                        continue
            finally:
                conn.close()
        return sessions

    def find_session(self, id_or_prefix: str) -> Session | None:
        q = id_or_prefix.strip().lower()
        for s in self.list_sessions():
            if s.id.lower() == q or s.id.lower().startswith(q):
                return s
        return None

    def delete_session(self, session_id: str) -> bool:
        path = self.sessions_dir / f"{session_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    # --- Project Memory (.rbit/memory.md + RBIT.md) ---
    def load_project_memory(self) -> str:
        parts: list[str] = []
        root_doc = self.workspace / "RBIT.md"
        if root_doc.exists():
            try:
                parts.append(root_doc.read_text(encoding="utf-8", errors="ignore").strip())
            except Exception:
                pass
        if self.memory_path.exists():
            try:
                parts.append(self.memory_path.read_text(encoding="utf-8", errors="ignore").strip())
            except Exception:
                pass
        return "\n\n".join(p for p in parts if p)

    def append_memory_rule(self, rule: str) -> Path:
        self.ensure_dirs()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        existing = self.memory_path.read_text(encoding="utf-8") if self.memory_path.exists() else "# rbit Project Memory\n\n"
        updated = existing.rstrip() + f"\n- [{stamp}] {rule.strip()}\n"
        self.memory_path.write_text(updated, encoding="utf-8")
        return self.memory_path

    # --- Custom Project Slash Commands (.rbit/commands/*.md) ---
    def load_custom_commands(self) -> dict[str, dict[str, str]]:
        """Loads project-local prompt templates: one Markdown file per command."""
        commands: dict[str, dict[str, str]] = {}
        if not self.commands_dir.exists():
            return commands
        for path in sorted(self.commands_dir.glob("*.md")):
            try:
                raw = path.read_text(encoding="utf-8")
            except Exception:
                continue
            name = path.stem.lower().strip()
            if not name:
                continue
            lines = raw.splitlines()
            description = ""
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                description = stripped.lstrip("#").strip()[:90]
                break
            body_lines = lines[1:] if lines and lines[0].strip().startswith("# ") else lines
            template = "\n".join(body_lines).strip()
            if not template:
                continue
            commands[name] = {
                "description": description or f"Project command /{name}",
                "template": template,
            }
        return commands

    # --- Workspace Checkpoints (Git-independent time travel) ---
    def save_checkpoint(self, label: str, files_map: dict[str, str]) -> CheckpointMeta:
        self.ensure_dirs()
        cid = short_id()
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "id": cid,
            "label": label or f"Checkpoint {cid}",
            "created_at": created,
            "files": files_map,
        }
        (self.checkpoints_dir / f"{cid}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.prune_checkpoints(self.MAX_CHECKPOINTS)
        return CheckpointMeta(
            id=cid,
            label=str(payload["label"]),
            created_at=created,
            file_count=len(files_map),
        )

    def prune_checkpoints(self, keep: int = 20) -> int:
        """Keeps only the newest `keep` checkpoints, deleting older ones."""
        if not self.checkpoints_dir.exists():
            return 0
        files = sorted(
            self.checkpoints_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for path in files[max(1, keep):]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def list_checkpoints(self) -> list[CheckpointMeta]:
        if not self.checkpoints_dir.exists():
            return []
        items: list[CheckpointMeta] = []
        for p in sorted(
            self.checkpoints_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                items.append(
                    CheckpointMeta(
                        id=data["id"],
                        label=data.get("label", data["id"]),
                        created_at=data.get("created_at", ""),
                        file_count=len(data.get("files", {})),
                    )
                )
            except Exception:
                continue
        return items

    def load_checkpoint_files(self, cid: str) -> tuple[CheckpointMeta, dict[str, str]] | None:
        path = self.checkpoints_dir / f"{cid}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            meta = CheckpointMeta(
                id=data["id"],
                label=data.get("label", cid),
                created_at=data.get("created_at", ""),
                file_count=len(data.get("files", {})),
            )
            return meta, data.get("files", {})
        except Exception:
            # A corrupted checkpoint (truncated by a crash) must not crash the
            # /rewind worker — surface it as "checkpoint not found" instead.
            return None

    # --- Session Markdown Export ---
    def export_session_markdown(self, session: Session, out_path: Path | None = None) -> Path:
        from .tools import redact_secrets

        target = out_path or (self.workspace / f"rbit-session-{session.id}.md")
        lines: list[str] = [
            f"# {session.title}",
            f"- **Session ID:** `{session.id}`",
            f"- **Created:** {session.created_at}",
            f"- **Total Tokens:** {session.total_tokens:,}",
            "",
        ]
        if session.todos:
            lines.append("## Todo Checklist")
            for t in session.todos:
                mark = "x" if t.status == "completed" else " "
                lines.append(f"- [{mark}] {t.content} *({t.status}, {t.priority})*")
            lines.append("")

        if session.modified_files:
            lines.append("## Modified Files")
            for rel, st in session.modified_files.items():
                lines.append(f"- `{rel}` (+{st.added} / -{st.removed})")
            lines.append("")

        lines.append("## Conversation")
        for m in session.messages:
            lines.append("")
            if m.role == "user":
                lines.append(f"### User ({m.timestamp} · `{m.agent}`)")
                lines.append(m.content)
            else:
                lines.append(f"### rbit (`{m.model}` · `{m.agent}` · {m.duration_s:.1f}s)")
                for tc in m.tool_calls:
                    lines.append(f"- **Tool `{tc.name}`**: {tc.summary}")
                    if tc.diff:
                        lines.append("```diff")
                        lines.append(tc.diff[:2500])
                        lines.append("```")
                lines.append("")
                lines.append(m.content)

        target.write_text(redact_secrets("\n".join(lines) + "\n"), encoding="utf-8")
        return target
