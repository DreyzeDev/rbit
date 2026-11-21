"""Persistent configuration, session storage, project memory, checkpoints, and Markdown export for rbit."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import CheckpointMeta, Session, short_id
from .trust import (
    PERMISSION_KEYS,
    STRIP_KEYS,
    TrustStore,
    fingerprint,
    risky_settings,
    split_config,
    trust_all_requested,
)

log = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_DEFAULT_BASE = "https://openrouter.ai/api/v1"


def _host_of(url: str) -> str:
    try:
        return (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness probe (no /proc dependency: macOS has none)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong(0)
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class StorageManager:
    MAX_CHECKPOINTS = 20
    MAX_AUTO_CHECKPOINTS = 20

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        self.state_dir = self.workspace / ".rbit"
        self.sessions_dir = self.state_dir / "sessions"
        self.checkpoints_dir = self.state_dir / "checkpoints"
        self.config_path = self.state_dir / "config.json"
        self.memory_path = self.state_dir / "memory.md"
        self.commands_dir = self.state_dir / "commands"
        self._checkpoint_cache: tuple[tuple[int, int], list[CheckpointMeta]] | None = None
        #: Set when config.json was unreadable; the original is kept in config.json.bak.
        self.config_warning: str = ""
        #: Where the effective API key came from: "config" | "env" | "".
        self.api_key_source: str = ""
        #: Session files that failed to load during the last list_sessions().
        self.corrupt_sessions: list[str] = []
        #: Settings of this project's config.json that were ignored by the last load_config()
        #: because the user has not trusted them (see rbit/trust.py).
        self.untrusted_keys: list[str] = []
        self.trust_store = TrustStore()

    # --- Trust for project config (see rbit/trust.py) ---
    def _read_raw_config(self) -> dict[str, Any]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def untrusted_notice(self) -> str:
        """A one-paragraph explanation for the user, or "" when everything was trusted."""
        if not self.untrusted_keys:
            return ""
        names = ", ".join(self.untrusted_keys)
        return (
            f"Ignored untrusted settings from this project's .rbit/config.json: {names}. "
            "They can start programs, run code or widen permissions, so they stay off until "
            "you review them: run `rbit trust`."
        )

    def pending_trust(self) -> dict[str, Any]:
        """The risky settings in config.json that are not trusted yet, as {key: value-as-written}."""
        raw = self._read_raw_config()
        trusted = self.trust_store.trusted_for(self.workspace)
        return {k: v for k, v in risky_settings(raw).items() if trusted.get(k) != fingerprint(v)}

    def trust_keys(self, keys: list[str] | None = None) -> list[str]:
        """Trusts the current value of `keys` (default: every pending one); returns what was trusted."""
        pending = self.pending_trust()
        chosen = {k: v for k, v in pending.items() if keys is None or k in keys}
        self.trust_store.trust(self.workspace, {k: fingerprint(v) for k, v in chosen.items()})
        return sorted(chosen)

    def revoke_trust(self) -> bool:
        return self.trust_store.forget(self.workspace)

    def set_setting(self, key: str, value: Any) -> None:
        """Writes ONE setting the user explicitly asked for and trusts it.

        The rest of config.json is kept exactly as it is (nothing else is persisted or
        dropped), which `save_config` cannot promise because it writes the whole dict.
        """
        raw = self._read_raw_config()
        raw[key] = value
        self._atomic_write_json(self.config_path, json.dumps(raw, ensure_ascii=False, indent=2))
        risky = risky_settings({key: value}).get(key)
        if risky is not None and not trust_all_requested():
            self.trust_store.trust(self.workspace, {key: fingerprint(risky)})

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
        for _ in range(5):
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    other = int(lock.read_text(encoding="utf-8").strip() or 0)
                except (ValueError, OSError):
                    other = 0
                if other and other != pid and _pid_alive(other):
                    return False, other
                if other == pid:
                    return True, None
                try:  # stale or unreadable lock: take it over and retry atomically
                    lock.unlink()
                except OSError:
                    time.sleep(0.05)
                continue
            except OSError:
                return True, None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(pid))
            return True, None
        return True, None

    def release_lock(self) -> None:
        try:
            import hashlib
            import tempfile

            key = hashlib.sha256(str(self.workspace).lower().encode("utf-8", errors="replace")).hexdigest()[:16]
            lock = Path(tempfile.gettempdir(), f"rbit-lock-{key}.lock")
            try:
                owner = int(lock.read_text(encoding="utf-8").strip() or 0)
            except (ValueError, OSError):
                owner = 0
            if owner == os.getpid():  # never remove another instance's lock
                lock.unlink(missing_ok=True)
        except OSError:
            pass

    def append_project_memory(self, bullets: list[str]) -> int:
        """Appends deduplicated fact bullets to .rbit/memory.md."""
        if not bullets:
            return 0
        self.ensure_dirs()
        existing = ""
        if self.memory_path.exists():
            existing = self.memory_path.read_text(encoding="utf-8", errors="ignore").lower()
        added = 0
        new_lines = []
        for b in bullets:
            b = str(b).strip().lstrip("-• ").strip()
            if not b or b.lower() in existing:
                continue
            new_lines.append(f"- {b}")
            existing += b.lower() + "\n"
            added += 1
        if new_lines:
            with self.memory_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")
        return added

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
            "api_key": "",
            "sidebar_visible": True,
            "auto_approve_safe": True,
            "allow_secret_reads": False,
        }
        self.config_warning = ""
        self.untrusted_keys = []
        if self.config_path.exists():
            try:
                # utf-8-sig: a BOM (Notepad, PowerShell) must not make the config "empty".
                loaded = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
                if not isinstance(loaded, dict):
                    raise ValueError("config.json must contain a JSON object")
                if not trust_all_requested():
                    # A cloned repository's config may not start programs or widen permissions
                    # until the user trusts those settings.
                    loaded, self.untrusted_keys = split_config(
                        loaded, self.trust_store.trusted_for(self.workspace)
                    )
                    if self.untrusted_keys:
                        log.warning(self.untrusted_notice())
                if loaded.get("theme") in ("opencode", "opencode-dark"):
                    loaded["theme"] = "rbit-dark"
                if loaded.get("theme") == "opencode-light":
                    loaded["theme"] = "rbit-light"
                if loaded.get("model") == "opencode/smart-local":
                    loaded["model"] = "rbit/smart-local"
                default_cfg.update({k: v for k, v in loaded.items() if v is not None})
            except Exception as exc:
                self._backup_corrupt_config(exc)
        file_key = str(default_cfg.get("api_key") or "").strip()
        self.api_key_source = "config" if file_key else ""
        if not file_key:
            env_key = self._env_api_key_for(str(default_cfg.get("api_base") or ""), raw_base)
            if env_key:
                default_cfg["api_key"] = env_key
                self.api_key_source = "env"

        profile = (os.environ.get("RBIT_PROFILE") or "").strip()
        if profile:
            profiles = default_cfg.get("profiles")
            if isinstance(profiles, dict) and isinstance(profiles.get(profile), dict):
                default_cfg.update(
                    {k: v for k, v in profiles[profile].items() if v is not None}
                )
        return default_cfg

    def _backup_corrupt_config(self, exc: Exception) -> None:
        """Keeps an unreadable config.json as config.json.bak instead of overwriting it."""
        bak = self.config_path.with_name(self.config_path.name + ".bak")
        try:
            if bak.exists():
                bak = self.config_path.with_name(f"{self.config_path.name}.{int(time.time())}.bak")
            bak.write_bytes(self.config_path.read_bytes())
            self.config_warning = f"config.json is unreadable ({exc}); original kept as {bak.name}, using defaults."
        except OSError:
            self.config_warning = f"config.json is unreadable ({exc}); using defaults."
        log.warning(self.config_warning)

    @staticmethod
    def _env_api_key_for(api_base: str, env_base: str) -> str:
        """Returns the env API key that is allowed to be sent to `api_base`.

        A key is only used for the host it belongs to: OPENAI_API_KEY never goes to
        openrouter.ai, and no env key follows a project config to a foreign host.
        """
        host = _host_of(api_base)
        env_host = _host_of(env_base)
        explicit_base = bool(os.environ.get("RBIT_API_BASE") or os.environ.get("OPENAI_BASE_URL"))
        same_as_env_base = bool(host) and host == env_host
        candidates = (
            ("RBIT_API_KEY", same_as_env_base),
            ("OPENROUTER_API_KEY", _host_matches(host, "openrouter.ai")),
            ("OPENAI_API_KEY", _host_matches(host, "openai.com") or (explicit_base and same_as_env_base)),
        )
        for name, allowed in candidates:
            value = (os.environ.get(name) or "").strip()
            if value and allowed:
                return value
        return ""

    @classmethod
    def env_key_allowed(cls, key: str, api_base: str) -> bool:
        """False when `key` is an environment key that must not go to `api_base`."""
        key = (key or "").strip()
        env_keys = {
            (os.environ.get(n) or "").strip()
            for n in ("RBIT_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")
        }
        env_keys.discard("")
        if key not in env_keys:
            return True
        env_base = (
            os.environ.get("RBIT_API_BASE") or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_BASE
        ).rstrip("/")
        return cls._env_api_key_for(api_base, env_base) == key

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
        import tempfile

        self.ensure_dirs()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name in the same folder (same filesystem => atomic replace);
        # mkstemp creation gives 0600 on POSIX, which suits config/secrets.
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(payload)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            last_exc: OSError | None = None
            for attempt in range(8):
                try:
                    os.replace(tmp_name, path)
                    last_exc = None
                    break
                except PermissionError as exc:  # Windows: target briefly locked by AV/readers
                    last_exc = exc
                    time.sleep(0.05 * (attempt + 1))
            if last_exc is not None:
                raise last_exc
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass

    def save_config(self, cfg: dict[str, Any]) -> None:
        cfg = dict(cfg)
        # A key that came from the environment is never persisted to disk.
        key = str(cfg.get("api_key") or "").strip()
        if key and key != "keyring":
            env_keys = {
                (os.environ.get(n) or "").strip()
                for n in ("RBIT_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")
            }
            env_keys.discard("")
            if key in env_keys and self.api_key_source != "config":
                cfg["api_key"] = self._existing_file_key()
        profile = (os.environ.get("RBIT_PROFILE") or "").strip()
        if profile:
            raw: dict[str, Any] = {}
            if self.config_path.exists():
                try:
                    raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
                    if not isinstance(raw, dict):
                        raise ValueError("not an object")
                except Exception as exc:
                    self._backup_corrupt_config(exc)
                    raw = {}
            profiles = raw.get("profiles")
            if not isinstance(profiles, dict):
                profiles = {}
            profiles[profile] = {k: v for k, v in cfg.items() if k != "profiles"}
            raw["profiles"] = profiles
            self._atomic_write_json(
                self.config_path,
                json.dumps(raw, ensure_ascii=False, indent=2),
            )
        else:
            to_write, trust_updates = self._apply_trust_on_save(cfg)
            self._atomic_write_json(
                self.config_path,
                json.dumps(to_write, ensure_ascii=False, indent=2),
            )
            if trust_updates:
                self.trust_store.trust(self.workspace, trust_updates)
        if os.name != "nt":
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass

    def _apply_trust_on_save(self, cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        """Decides what to write and which risky settings the user just chose.

        The app saves the dict it got from load_config(), in which untrusted settings are
        missing. Writing that back unchanged would silently delete them from the project's
        file, so a setting the user did not touch is restored from the file and stays
        untrusted. A setting that differs from what load_config() showed was changed on the
        user's behalf (`mcp add`, `/connect`, `plugin enable`, `config set`, ...): it is
        written and its new value is trusted.
        """
        if trust_all_requested():
            return cfg, {}
        shown = self.load_config()  # exactly what the user saw (also refreshes untrusted_keys)
        untrusted = set(self.untrusted_keys)
        raw = self._read_raw_config()
        to_write = dict(cfg)
        updates: dict[str, str] = {}
        for key in (*STRIP_KEYS, *PERMISSION_KEYS, "profiles"):
            changed = cfg.get(key) != shown.get(key)
            if not changed:
                # Untouched: keep exactly what the file had (an untrusted value stays untrusted)
                # and never write a default or an env override into the file, where it would
                # later look like a project-supplied risky value.
                if key in raw:
                    to_write[key] = raw[key]
                else:
                    to_write.pop(key, None)
            elif changed and key in to_write:
                risky = risky_settings({key: to_write[key]}).get(key)
                if risky is not None and key != "profiles":
                    updates[key] = fingerprint(risky)
        return to_write, updates

    def _existing_file_key(self) -> str:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            return str(data.get("api_key") or "") if isinstance(data, dict) else ""
        except Exception:
            return ""

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

    @staticmethod
    def _redacted_session_dict(session: Session) -> dict[str, Any]:
        """Session payload for disk: secrets printed by tools (env/.env dumps) are masked."""
        from .tools import redact_secrets

        data = session.to_dict()
        for message in data.get("messages", []):
            for call in message.get("tool_calls") or []:
                for field in ("output", "diff", "summary"):
                    value = call.get(field)
                    if isinstance(value, str) and value:
                        call[field] = redact_secrets(value)
        return data

    def save_session(self, session: Session) -> None:
        payload = json.dumps(self._redacted_session_dict(session), ensure_ascii=False, indent=2)
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

    def list_sessions(self, include_archived: bool = False) -> list[Session]:
        self.corrupt_sessions = []
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
                data = self._decode_session_file(p.read_text(encoding="utf-8-sig"))
                sessions.append(Session.from_dict(data))
            except Exception as exc:
                self.corrupt_sessions.append(p.name)
                log.warning("skipping unreadable session %s: %s", p.name, exc)
                continue
        if self._use_sqlite():
            conn = self._sqlite_conn()
            try:
                for (raw,) in conn.execute(
                    "SELECT data FROM sessions ORDER BY updated DESC"
                ):
                    try:
                        sessions.append(Session.from_dict(self._decode_session_file(raw)))
                    except Exception as exc:
                        self.corrupt_sessions.append("sessions.db")
                        log.warning("skipping unreadable sqlite session: %s", exc)
                        continue
            finally:
                conn.close()
        if not include_archived:
            sessions = [session for session in sessions if not session.archived]
        return sessions

    def find_session(self, id_or_prefix: str) -> Session | None:
        q = (id_or_prefix or "").strip().lower()
        if not q:
            return None
        sessions = self.list_sessions(include_archived=True)
        for s in sessions:
            if s.id.lower() == q:
                return s
        matches = [s for s in sessions if s.id.lower().startswith(q)]
        # An ambiguous prefix must not silently pick "the first" session.
        return matches[0] if len(matches) == 1 else None

    def delete_session(self, session_id: str) -> bool:
        sid = (session_id or "").strip()
        if not _SAFE_ID_RE.match(sid):
            return False
        deleted = False
        path = self.sessions_dir / f"{sid}.json"
        try:
            if path.resolve().parent != self.sessions_dir.resolve():
                return False
        except OSError:
            return False
        if path.exists():
            path.unlink()
            deleted = True
        if self._sqlite_path().exists():
            conn = self._sqlite_conn()
            try:
                with conn:
                    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                    deleted = deleted or cur.rowcount > 0
            finally:
                conn.close()
        return deleted

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
        line = f"- [{stamp}] {rule.strip()}\n"
        # Append-only under a cross-process lock: no read-modify-write, no lost entries.
        with self._file_lock(self.memory_path):
            data = self.memory_path.read_bytes() if self.memory_path.exists() else b""
            if not data.strip():
                prefix = "# rbit Project Memory\n\n"
            else:
                prefix = "" if data.endswith(b"\n") else "\n"
            with self.memory_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(prefix + line)
        return self.memory_path

    @staticmethod
    def _file_lock(target: Path):
        """Tiny advisory lock (`<file>.lock`, O_EXCL) with retry and stale-lock recovery."""
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            lock = target.with_name(target.name + ".lock")
            started = time.monotonic()
            deadline = started + 10.0
            #: A PermissionError is retried only briefly: on Windows it also means
            #: "the previous holder is still deleting the lock file", but in a folder
            #: we genuinely cannot write to it would otherwise stall every append.
            permission_deadline = started + 2.0
            fd = -1
            while fd < 0:
                try:
                    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    try:
                        if time.time() - lock.stat().st_mtime > 30:
                            lock.unlink()
                            continue
                    except OSError:
                        pass
                    if time.monotonic() > deadline:
                        break  # give up waiting: better a racy append than a hung UI
                    time.sleep(0.005)
                except PermissionError:
                    # Windows reports a lock file that is mid-deletion as "access denied".
                    # Treating that as "no locking possible" let two writers through and
                    # lost memory entries under contention (seen in ~1 of 10 runs).
                    if time.monotonic() > permission_deadline:
                        break
                    time.sleep(0.005)
                except OSError:
                    break
            try:
                yield
            finally:
                if fd >= 0:
                    os.close(fd)
                    try:
                        lock.unlink()
                    except OSError:
                        pass

        return _cm()

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
        is_auto = str(label or "").strip().lower().startswith("auto")
        cid = ("auto-" if is_auto else "") + short_id()
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "id": cid,
            "label": label or f"Checkpoint {cid}",
            "created_at": created,
            "files": files_map,
        }
        self._atomic_write_json(
            self.checkpoints_dir / f"{cid}.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        self.prune_checkpoints(self.MAX_CHECKPOINTS)
        self.invalidate_checkpoint_cache()
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
        # Manual and automatic checkpoints rotate independently, so a burst of
        # auto-checkpoints can never evict the ones the user made on purpose.
        manual = [p for p in files if not p.name.startswith("auto-")]
        auto = [p for p in files if p.name.startswith("auto-")]
        doomed = manual[max(1, keep):] + auto[max(1, self.MAX_AUTO_CHECKPOINTS):]
        removed = 0
        for path in doomed:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        if removed:
            self.invalidate_checkpoint_cache()
        return removed

    def list_checkpoints(self) -> list[CheckpointMeta]:
        """Checkpoint index.

        The TUI refreshes the sidebar on every turn, so this is on a hot path.
        The result is memoised against the directory mtime + entry count: a
        cheap `stat()` instead of re-reading and parsing every checkpoint JSON.
        """
        if not self.checkpoints_dir.exists():
            return []
        try:
            stat = self.checkpoints_dir.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        if signature is not None and self._checkpoint_cache is not None:
            cached_sig, cached_items = self._checkpoint_cache
            if cached_sig == signature:
                return list(cached_items)
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
        if signature is not None:
            self._checkpoint_cache = (signature, list(items))
        return items

    def invalidate_checkpoint_cache(self) -> None:
        self._checkpoint_cache = None

    def checkpoint_count(self) -> int:
        """Cheap checkpoint count for UI refreshes.

        Never parses JSON: checkpoint files embed full file contents and can be
        megabytes each (profiled at ~325 ms for 15 checkpoints on the UI
        thread). Counting directory entries costs a fraction of a millisecond.
        """
        if not self.checkpoints_dir.exists():
            return 0
        try:
            with os.scandir(self.checkpoints_dir) as entries:
                return sum(
                    1
                    for entry in entries
                    if entry.name.endswith(".json") and entry.is_file()
                )
        except OSError:
            return 0

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
