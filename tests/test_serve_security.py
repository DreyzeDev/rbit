"""Security regressions for `rbit serve`: a web page must not be able to drive the agent."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from rbit.cli import _mask_secret_values, _run_serve


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def _start_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str = "") -> int:
    class _NoService:
        def __init__(self) -> None:
            raise RuntimeError("no user service settings in tests")

    monkeypatch.setattr("rbit.service.ServiceManager", _NoService)
    monkeypatch.delenv("RBIT_SERVER_TOKEN", raising=False)
    if token:
        monkeypatch.setenv("RBIT_SERVER_TOKEN", token)
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    rbit_dir = tmp_path / ".rbit"
    rbit_dir.mkdir(exist_ok=True)
    (rbit_dir / "config.json").write_text(
        json.dumps(
            {
                "credentials": {"work": {"api_key": "sk-FAKE-SECRET-1"}},
                "mcp_oauth": {"srv": "oauth-FAKE-TOKEN"},
                "mcp": {"s": {"headers": {"Authorization": "Bearer FAKE-HEADER"}}},
                "max_tokens": 4096,
            }
        ),
        encoding="utf-8",
    )
    port = _free_port()
    args = Namespace(dir=str(tmp_path), port=port)
    thread = threading.Thread(
        target=lambda: _run_serve(args, hostname="127.0.0.1"), daemon=True
    )
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if _request(port, "GET", "/health")[0] == 200:
                return port
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server did not start")


def test_tokenless_server_rejects_foreign_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start_server(tmp_path, monkeypatch)

    status, _h, _b = _request(port, "GET", "/v1/sessions")
    assert status == 200  # CLI / same-machine client without an Origin keeps working

    status, _h, _b = _request(port, "GET", "/v1/sessions", {"Origin": "https://evil.example"})
    assert status == 401

    # Simple cross-site POST (no preflight needed): must not reach the agent.
    status, _h, _b = _request(
        port,
        "POST",
        "/v1/turns",
        {"Origin": "https://evil.example", "Content-Type": "text/plain"},
        b'{"prompt": "run something"}',
    )
    assert status == 401

    status, headers, _b = _request(
        port, "OPTIONS", "/v1/turns", {"Origin": "https://evil.example"}
    )
    assert "access-control-allow-origin" not in headers


def test_tokenless_server_rejects_dns_rebinding_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start_server(tmp_path, monkeypatch)
    status, _h, _b = _request(port, "GET", "/v1/sessions", {"Host": "attacker.example:8333"})
    assert status == 401


def test_token_is_required_and_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start_server(tmp_path, monkeypatch, token="s3cret-token")
    assert _request(port, "GET", "/v1/sessions")[0] == 401
    assert _request(port, "GET", "/v1/sessions", {"Authorization": "Bearer wrong"})[0] == 401
    ok = _request(port, "GET", "/v1/sessions", {"Authorization": "Bearer s3cret-token"})
    assert ok[0] == 200


def test_config_endpoint_masks_nested_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start_server(tmp_path, monkeypatch)
    status, _h, body = _request(port, "GET", "/v1/config")
    text = body.decode("utf-8")
    assert status == 200
    for leaked in ("sk-FAKE-SECRET-1", "oauth-FAKE-TOKEN", "FAKE-HEADER"):
        assert leaked not in text
    assert json.loads(text)["max_tokens"] == 4096  # plain setting stays readable


def test_fs_endpoint_hides_secret_and_state_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _start_server(tmp_path, monkeypatch)
    assert _request(port, "GET", "/v1/fs?path=.rbit/config.json")[0] == 403
    assert _request(port, "GET", "/v1/fs?path=.env")[0] == 403
    status, _h, body = _request(port, "GET", "/v1/fs?path=main.py")
    assert status == 200 and "print('hi')" in body.decode("utf-8")


def test_mask_secret_values_handles_nesting_and_keeps_plain_values() -> None:
    data: dict[str, Any] = {
        "api_key": "k",
        "profiles": {"a": {"api_key": "k2", "model": "m"}},
        "list": [{"server_token": "t"}],
        "empty_token": "",
        "max_tokens": 10,
    }
    masked = _mask_secret_values(data)
    assert masked["api_key"] == "***"
    assert masked["profiles"]["a"] == {"api_key": "***", "model": "m"}
    assert masked["list"][0]["server_token"] == "***"
    assert masked["empty_token"] == ""
    assert masked["max_tokens"] == 10
    assert data["api_key"] == "k"  # input is never mutated
