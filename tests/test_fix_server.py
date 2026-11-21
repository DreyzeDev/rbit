"""`rbit serve` behavior over real HTTP: concurrent turns, body handling, routes, artifacts, abort.

The agent is replaced by a slow fake so a "turn" lasts long enough to race it. The server runs
in a thread on a free loopback port without a token (same-machine clients without an Origin).
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from rbit.cli import _run_serve
from rbit.models import ChatMessage, short_id


class _SlowAgent:
    """Stands in for AgentEngine.run_turn."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    async def __call__(self, session: Any, user_msg: Any, agent_mode: str, model_id: str,
                       on_tool_start: Any, on_tool_end: Any, on_status: Any, **_kw: Any) -> ChatMessage:
        if self.delay:
            await asyncio.sleep(self.delay)
        return ChatMessage(id=short_id(), role="assistant", content="reply", agent=agent_mode, model=model_id)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delay: float = 0.0) -> int:
    class _NoService:
        def __init__(self) -> None:
            raise RuntimeError("no user service settings in tests")

    monkeypatch.setattr("rbit.service.ServiceManager", _NoService)
    monkeypatch.delenv("RBIT_SERVER_TOKEN", raising=False)
    monkeypatch.setattr("rbit.agent.AgentEngine.run_turn", _SlowAgent(delay))
    port = _free_port()
    threading.Thread(
        target=lambda: _run_serve(Namespace(dir=str(tmp_path), port=port), hostname="127.0.0.1"), daemon=True
    ).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if _call(port, "GET", "/health")[0] == 200:
                return port
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server did not start")


def _call(
    port: int, method: str, path: str, body: Any = None, timeout: float = 8.0
) -> tuple[int, dict[str, str], Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        payload = None if body is None else (body if isinstance(body, (bytes, str)) else json.dumps(body))
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        head = {k.lower(): v for k, v in resp.getheaders()}
        try:
            data = json.loads(raw) if raw and "json" in head.get("content-type", "") else raw
        except ValueError:
            data = raw
        return resp.status, head, data
    finally:
        conn.close()


def _raw(port: int, request: bytes, timeout: float = 5.0) -> tuple[int, bytes]:
    """Sends hand-written bytes (for headers http.client refuses to produce) and returns (status, body)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        chunks = b""
        try:
            while b"\r\n\r\n" not in chunks:
                part = sock.recv(4096)
                if not part:
                    break
                chunks += part
        except OSError:
            pass
    first = chunks.split(b"\r\n", 1)[0].decode("ascii", "replace")
    status = int(first.split()[1]) if first.startswith("HTTP/") else 0
    return status, chunks.split(b"\r\n\r\n", 1)[-1]


def _new_session(port: int, title: str = "t") -> str:
    status, _h, data = _call(port, "POST", "/v1/sessions", {"title": title})
    assert status == 201
    return str(data["id"])


# --------------------------------------------------------------------- turns


def test_concurrent_turns_on_one_session_run_one_and_reject_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start(tmp_path, monkeypatch, delay=1.0)
    sid = _new_session(port)
    statuses: list[int] = []
    lock = threading.Lock()

    def fire() -> None:
        status, _h, _d = _call(port, "POST", "/v1/turns", {"prompt": "go", "session_id": sid}, timeout=15)
        with lock:
            statuses.append(status)

    threads = [threading.Thread(target=fire) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sorted(statuses) == [200, 409, 409, 409, 409, 409]
    _s, _h, session = _call(port, "GET", f"/v1/sessions/{sid}")
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]  # not six interleaved turns


def test_a_turn_for_an_unknown_or_blank_session_is_a_404_not_a_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start(tmp_path, monkeypatch)
    before = len(_call(port, "GET", "/v1/sessions")[2]["sessions"])
    assert _call(port, "POST", "/v1/turns", {"prompt": "x", "session_id": "zzzz"})[0] == 404
    assert _call(port, "POST", "/v1/turns", {"prompt": "x", "session_id": ""})[0] == 404
    assert _call(port, "POST", "/v1/turns", {"prompt": "  "})[0] == 400
    assert len(_call(port, "GET", "/v1/sessions")[2]["sessions"]) == before


def test_abort_cancels_a_running_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _start(tmp_path, monkeypatch, delay=8.0)
    sid = _new_session(port)
    result: dict[str, Any] = {}

    def turn() -> None:
        result["status"], _h, result["body"] = _call(
            port, "POST", "/v1/turns", {"prompt": "slow", "session_id": sid}, timeout=20
        )

    thread = threading.Thread(target=turn)
    thread.start()
    aborted = False
    began = time.time()
    while time.time() - began < 5 and not aborted:  # the turn registers its loop a moment after start
        time.sleep(0.2)
        aborted = bool(_call(port, "POST", f"/v1/sessions/{sid}/abort")[2].get("aborted"))
    thread.join(timeout=10)

    assert aborted is True
    assert time.time() - began < 7.5  # ended long before the 8 s fake turn would have
    assert b"aborted" in result["body"]


def test_a_per_request_effort_does_not_change_the_server_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start(tmp_path, monkeypatch)
    default = _call(port, "GET", "/v1/status")[2].get("effort")
    assert _call(port, "POST", "/v1/turns", {"prompt": "x", "effort": "high"})[0] == 200
    assert _call(port, "GET", "/v1/status")[2].get("effort") == default


# ----------------------------------------------------------------- the body


@pytest.mark.parametrize("body", ["[]", "null", '"abc"', "123", "{", "not json"])
def test_a_bad_json_body_is_a_400_with_a_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    port = _start(tmp_path, monkeypatch)
    for path in ("/v1/sessions", "/v1/artifacts", "/v1/schedules", "/v1/approvals", "/v1/turns"):
        status, head, data = _call(port, "POST", path, body)
        assert status == 400, (path, status)
        assert "json" in head["content-type"] and "error" in data


def test_content_length_that_is_not_a_number_or_is_negative_is_a_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start(tmp_path, monkeypatch)
    for value in (b"abc", b"-5", b"1e3"):
        status, _body = _raw(
            port, b"POST /v1/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: " + value + b"\r\n\r\n"
        )
        assert status == 400, value


def test_a_huge_declared_body_is_refused_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start(tmp_path, monkeypatch)
    began = time.time()
    status, _body = _raw(
        port, b"POST /v1/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 99999999999\r\n\r\n"
    )
    assert status == 413
    assert time.time() - began < 4  # answered at once; the server did not wait for 99 GB


# ------------------------------------------------------------------- routes


@pytest.mark.parametrize(
    "path", ["/health?x=1", "/v1/status?x=1", "/v1/models?x=1", "/v1/config?x=1", "/v1/usage?x=1",
             "/v1/schedules?x=1", "/v1/skills?x=1", "/v1/sessions?x=1"],
)
def test_routes_accept_a_query_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    port = _start(tmp_path, monkeypatch)
    assert _call(port, "GET", path)[0] == 200, path


def test_unknown_routes_and_unsupported_methods_answer_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start(tmp_path, monkeypatch)
    status, head, data = _call(port, "GET", "/v1/nope")
    assert status == 404 and "json" in head["content-type"] and "error" in data
    for method in ("PUT", "PATCH"):
        status, head, data = _call(port, method, "/v1/sessions", "{}")
        assert status == 405 and "json" in head["content-type"] and data["error"] == "method not allowed"
        assert "GET" in head["allow"]


def test_session_lookup_delete_and_rename_report_errors_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start(tmp_path, monkeypatch)
    sid = _new_session(port, "first")

    assert _call(port, "GET", "/v1/sessions/")[0] == 404  # an empty id must not return "the latest session"
    assert _call(port, "POST", f"/v1/sessions/{sid}/rename", {"title": "  "})[0] == 422
    status, _h, data = _call(port, "POST", "/v1/sessions/zzzz/rename", {"title": "x"})
    assert status == 404 and data["error"] == "session not found"
    assert _call(port, "POST", f"/v1/sessions/{sid}/rename", {"title": "renamed"})[0] == 200
    assert _call(port, "GET", f"/v1/sessions/{sid}")[2]["title"] == "renamed"

    assert _call(port, "DELETE", "/v1/sessions/zzzz")[0] == 404
    assert _call(port, "DELETE", f"/v1/sessions/{sid}")[0] == 200
    assert _call(port, "GET", f"/v1/sessions/{sid}")[0] == 404
    # the turn endpoint must not resurrect a deleted session from memory
    assert _call(port, "POST", "/v1/turns", {"prompt": "x", "session_id": sid})[0] == 404


# ---------------------------------------------------------------- artifacts


def test_artifacts_with_non_ascii_names_do_not_overwrite_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start(tmp_path, monkeypatch)
    one = _call(port, "POST", "/v1/artifacts", {"name": "отчёт", "content": "первый"})
    two = _call(port, "POST", "/v1/artifacts", {"name": "итог", "content": "второй"})
    assert one[0] == 201 and two[0] == 201
    assert one[2]["name"] != two[2]["name"]
    from urllib.parse import quote

    assert _call(port, "GET", "/v1/artifacts/" + quote(one[2]["name"]))[2]["content"] == "первый"
    assert _call(port, "GET", "/v1/artifacts/" + quote(two[2]["name"]))[2]["content"] == "второй"


def test_an_existing_artifact_is_not_silently_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _start(tmp_path, monkeypatch)
    assert _call(port, "POST", "/v1/artifacts", {"name": "plan", "content": "v1"})[0] == 201
    status, _h, data = _call(port, "POST", "/v1/artifacts", {"name": "plan", "content": "v2"})
    assert status == 409 and "overwrite" in data["hint"]
    assert _call(port, "GET", "/v1/artifacts/plan.md")[2]["content"] == "v1"
    assert _call(port, "POST", "/v1/artifacts", {"name": "plan", "content": "v2", "overwrite": True})[0] == 201
    assert _call(port, "GET", "/v1/artifacts/plan.md")[2]["content"] == "v2"


def test_artifact_names_cannot_escape_the_artifact_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _start(tmp_path, monkeypatch)
    status, _h, data = _call(port, "POST", "/v1/artifacts", {"name": "../../evil", "content": "x"})
    assert status == 201 and "/" not in data["name"] and "\\" not in data["name"]
    assert not (tmp_path.parent / "evil.md").exists() and not (tmp_path / "evil.md").exists()
