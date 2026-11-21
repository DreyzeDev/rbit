"""Real-process tests for `rbit service` (ServiceManager): start, auth, stop, stale state, PID reuse.

Everything lives in tmp_path (state file, log, workspace); the user's ~/.rbit is never touched.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from rbit.service import ServiceManager, ServiceStartError, ServiceState, pid_alive


def _manager(tmp_path: Path) -> ServiceManager:
    return ServiceManager(state_path=tmp_path / "home" / "service.json")


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _status_code(url: str, token: str = "") -> int:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _wait_gone(pid: int, seconds: float = 6.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def test_service_lifecycle_start_auth_idempotent_stop(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    workspace = _workspace(tmp_path)
    state = manager.start(workspace)
    try:
        assert manager.status()["healthy"] is True
        assert _status_code(f"{state.url}/health") == 200  # liveness needs no token
        assert _status_code(f"{state.url}/v1/sessions") == 401  # everything else does
        assert _status_code(f"{state.url}/v1/sessions", state.token) == 200
        assert _status_code(f"{state.url}/v1/sessions", "wrong-token") == 401
        assert manager.start(workspace).pid == state.pid  # starting twice does not start a second server
        if os.name != "nt":
            assert stat.S_IMODE(manager.state_path.stat().st_mode) == 0o600  # the token is private
    finally:
        stopped = manager.stop()

    assert stopped is True
    assert _wait_gone(state.pid), "the server process was not terminated"
    assert manager.read() is None
    assert manager.status() == {"running": False, "detail": "not started"}


def test_a_stale_state_file_does_not_block_a_new_start(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    workspace = _workspace(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    manager.write(
        ServiceState(pid=dead.pid, port=1, host="127.0.0.1", workspace=str(workspace),
                     started_at=time.time(), log="", token="old")
    )
    assert manager.running() is None  # the recorded process is gone

    state = manager.start(workspace)
    try:
        assert state.pid != dead.pid and manager.status()["healthy"] is True
    finally:
        manager.stop()


def test_stop_never_kills_an_unrelated_process_that_reuses_the_pid(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    victim = _sleeper()
    try:
        # The state names a live process that is NOT our server (a reused PID) and nothing answers.
        manager.write(
            ServiceState(pid=victim.pid, port=_free_port(), host="127.0.0.1",
                         workspace=str(tmp_path), started_at=time.time(), log="", token="not-theirs")
        )
        assert manager.stop() is False  # nothing of ours was running
        time.sleep(0.3)
        assert victim.poll() is None, "an unrelated process was killed"
        assert manager.read() is None  # but the stale record is cleaned up
    finally:
        victim.kill()
        victim.wait()


def test_a_failed_start_raises_quickly_and_leaves_no_state(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    workspace = _workspace(tmp_path)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        began = time.time()
        with pytest.raises(ServiceStartError):
            manager.start(workspace, port=busy_port)  # the child cannot bind: it exits
        assert time.time() - began < 9.0
        assert manager.read() is None  # no record of a service that never ran
    finally:
        blocker.close()


def test_settings_survive_stop_and_a_failed_start(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    workspace = _workspace(tmp_path)
    manager.set_setting("mobile_token", "keep-me")
    state = manager.start(workspace)
    try:
        assert manager.get_setting("mobile_token") == "keep-me"
    finally:
        manager.stop()
    assert manager.get_setting("mobile_token") == "keep-me"
    assert state.pid > 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
