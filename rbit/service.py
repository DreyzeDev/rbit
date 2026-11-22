"""Background service management and OpenAPI-driven local API client."""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _state_path() -> Path:
    return Path.home() / ".rbit" / "service.json"


@dataclass
class ServiceState:
    pid: int
    port: int
    host: str
    workspace: str
    started_at: float
    log: str = ""
    token: str = ""

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "port": self.port,
            "host": self.host,
            "workspace": self.workspace,
            "started_at": self.started_at,
            "log": self.log,
            "token": self.token,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServiceState:
        return cls(
            pid=int(data.get("pid") or 0),
            port=int(data.get("port") or 8333),
            host=str(data.get("host") or "127.0.0.1"),
            workspace=str(data.get("workspace") or ""),
            started_at=float(data.get("started_at") or 0),
            log=str(data.get("log") or ""),
            token=str(data.get("token") or ""),
        )


class ServiceStartError(RuntimeError):
    """The background service died or never became healthy."""


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Access denied still means the process exists.
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    # `kill(pid, 0)` also succeeds for a zombie: a process that has finished but whose parent
    # has not collected it yet. For our purposes it is gone.
    try:
        done, _status = os.waitpid(pid, os.WNOHANG)  # collects our own finished child
        if done == pid:
            return False
    except (ChildProcessError, OSError):
        pass  # not our child: its own parent will collect it
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            return handle.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


pid_alive = _alive


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port(start: int = 8333, host: str = "127.0.0.1") -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port found")


def _health(url: str, timeout: float = 1.0) -> bool:
    try:
        with _no_proxy_opener().open(f"{url}/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _authenticated(url: str, token: str, timeout: float = 1.5) -> bool:
    """True when the server at `url` accepts our service token (identity check)."""
    request = urllib.request.Request(f"{url}/v1/status", headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with _no_proxy_opener().open(request, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path`, creating it with 0600 permissions (no chmod race)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _cmdline_mentions_rbit(pid: int) -> bool:
    """Best-effort process identity check where /proc is available."""
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    # Our server is `python -m rbit serve ...` (or the `rbit` entry point with `serve`). A mere
    # "rbit" somewhere in the command line (an editor on rbit.py, a path like /home/me/rbit/...)
    # does not make a process ours: SIGTERM must never hit an unrelated process that got the PID.
    args = data.split(b"\0")
    if b"serve" not in args:
        return False
    for index, arg in enumerate(args[:-2]):
        if arg == b"-m" and args[index + 1] == b"rbit":
            return True
    return any(os.path.basename(arg.decode("utf-8", "replace")) in ("rbit", "rbit.exe") for arg in args[:2])


class ServiceManager:
    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or _state_path()

    def read(self) -> ServiceState | None:
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return ServiceState.from_dict(data)
        except (OSError, ValueError):
            return None

    def write(self, state: ServiceState) -> None:
        data = state.to_dict()
        settings = self.get_settings()
        if settings:
            data["settings"] = settings
        _write_private(self.state_path, json.dumps(data, indent=2))

    def clear(self) -> None:
        try:
            self.state_path.unlink()
        except OSError:
            pass

    def running(self) -> ServiceState | None:
        state = self.read()
        if state is None:
            return None
        if not _alive(state.pid):
            return None
        return state

    def status(self) -> dict[str, Any]:
        state = self.read()
        if state is None:
            return {"running": False, "detail": "not started"}
        alive = _alive(state.pid)
        healthy = alive and _health(state.url)
        return {
            "running": alive,
            "healthy": healthy,
            "pid": state.pid,
            "url": state.url,
            "workspace": state.workspace,
            "log": state.log,
            "started_at": state.started_at,
        }

    def start(self, workspace: Path, host: str = "127.0.0.1", port: int | None = None) -> ServiceState:
        active = self.running()
        if active is not None:
            return active
        chosen = port or _free_port(host=host if host != "0.0.0.0" else "127.0.0.1")
        log_path = self.state_path.parent / "service.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - handed to the child
        token = secrets.token_urlsafe(32)
        command = [
            sys.executable,
            "-m",
            "rbit",
            "serve",
            "--port",
            str(chosen),
            "--hostname",
            host,
            "--dir",
            str(workspace),
        ]
        kwargs: dict[str, Any] = {
            "stdout": log_file,
            "stderr": log_file,
            "stdin": subprocess.DEVNULL,
            "cwd": str(workspace),
            "env": {**os.environ, "RBIT_SERVER_TOKEN": token},
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        log_file.close()
        state = ServiceState(
            pid=process.pid,
            port=chosen,
            host=host,
            workspace=str(workspace),
            started_at=time.time(),
            log=str(log_path),
            token=token,
        )
        self.write(state)
        for _ in range(50):
            if _health(state.url, timeout=0.4):
                return state
            if process.poll() is not None:
                break
            time.sleep(0.2)
        died = process.poll() is not None
        if not died:
            try:
                process.terminate()
            except OSError:
                pass
        settings = self.get_settings()
        self.clear()
        if settings:
            self._write_raw({"settings": settings})
        reason = "the process exited" if died else "the health check never succeeded"
        raise ServiceStartError(f"service failed to start ({reason}); see log: {log_path}")

    def stop(self) -> bool:
        state = self.read()
        if state is None:
            return False
        was_running = _alive(state.pid)
        # Never kill a PID we cannot prove is our server (PIDs get reused).
        if was_running and not (_authenticated(state.url, state.token) or _cmdline_mentions_rbit(state.pid)):
            was_running = False
            killable = False
        else:
            killable = was_running
        if killable:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(state.pid), "/T", "/F"], capture_output=True, check=False
                    )
                else:
                    os.kill(state.pid, signal.SIGTERM)
                    for _ in range(20):
                        if not _alive(state.pid):
                            break
                        time.sleep(0.1)
            except OSError:
                pass
        settings = self.get_settings()
        self.clear()
        if settings:
            self._write_raw({"settings": settings})
        return was_running

    def restart(self, workspace: Path, host: str = "127.0.0.1", port: int | None = None) -> ServiceState:
        self.stop()
        return self.start(workspace, host=host, port=port)

    def _raw(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_raw(self, data: dict[str, Any]) -> None:
        _write_private(self.state_path, json.dumps(data, indent=2))

    def get_settings(self) -> dict[str, Any]:
        settings = self._raw().get("settings")
        return settings if isinstance(settings, dict) else {}

    def get_setting(self, key: str) -> Any:
        return self.get_settings().get(key)

    def set_setting(self, key: str, value: str) -> None:
        data = self._raw()
        settings = data.get("settings")
        if not isinstance(settings, dict):
            settings = {}
        settings[key] = value
        data["settings"] = settings
        self._write_raw(data)

    def unset_setting(self, key: str) -> bool:
        data = self._raw()
        settings = data.get("settings")
        if not isinstance(settings, dict) or key not in settings:
            return False
        del settings[key]
        data["settings"] = settings
        self._write_raw(data)
        return True


def api_request(
    url: str,
    method: str,
    path: str,
    *,
    data: Any = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, str]:
    import httpx

    query = f"?{httpx.QueryParams(params)}" if params else ""
    body = None if data is None else (data if isinstance(data, (bytes, str)) else json.dumps(data))
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    with httpx.Client(timeout=timeout) as client:
        response = client.request(method.upper(), f"{url}{path}{query}", content=body, headers=request_headers)
    return response.status_code, response.text
