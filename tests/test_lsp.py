"""Language server client: config, diagnostics, hover, and diagnostics tool fallback."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from rbit.lsp import LSPManager
from rbit.models import Session
from rbit.tools import ToolExecutor

FAKE_SERVER = r'''
import json
import sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.strip().lower()] = value.strip()
    body = sys.stdin.buffer.read(int(headers["content-length"]))
    return json.loads(body.decode("utf-8"))

def send(payload):
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {"textDocumentSync": 1}}})
    elif method == "textDocument/didOpen":
        uri = message["params"]["textDocument"]["uri"]
        send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "diagnostics": [{
                    "range": {"start": {"line": 0, "character": 0}},
                    "severity": 1,
                    "message": "Boom",
                    "source": "fake",
                }],
            },
        })
    elif method == "textDocument/hover":
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"contents": {"kind": "markdown", "value": "hover docs"}},
        })
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        break
'''


def _server_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    entry: dict[str, Any] = {
        "command": [sys.executable, str(script)],
        "extensions": [".foo"],
        "language_ids": {".foo": "foo"},
    }
    entry.update(overrides)
    return {"lsp": {"fake": entry}}


@pytest.mark.asyncio
async def test_custom_server_reports_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "note.foo"
    path.write_text("bad\n", encoding="utf-8")
    manager = LSPManager.from_config(tmp_path, _server_config(tmp_path))
    try:
        assert any(server.name == "fake" for server in manager.servers)
        diagnostics = await manager.diagnostics_for(path)
        assert len(diagnostics) == 1
        assert diagnostics[0].severity == "error"
        assert diagnostics[0].message == "Boom"
        assert diagnostics[0].source == "fake"
        assert "fake: running" in manager.status_lines()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_hover_returns_server_text(tmp_path: Path) -> None:
    path = tmp_path / "note.foo"
    path.write_text("bad\n", encoding="utf-8")
    manager = LSPManager.from_config(tmp_path, _server_config(tmp_path))
    try:
        assert await manager.hover(path, 1, 1) == "hover docs"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_disabled_server_is_not_configured(tmp_path: Path) -> None:
    manager = LSPManager.from_config(tmp_path, _server_config(tmp_path, disabled=True))
    assert not any(server.name == "fake" for server in manager.servers)
    assert manager.definition_for(tmp_path / "note.foo") is None
    await manager.close()


@pytest.mark.asyncio
async def test_lsp_is_opt_in(tmp_path: Path) -> None:
    assert LSPManager.from_config(tmp_path, {}).configured_count == 0
    assert LSPManager.from_config(tmp_path, {"lsp": False}).configured_count == 0


@pytest.mark.asyncio
async def test_diagnostics_tool_uses_lsp(tmp_path: Path) -> None:
    path = tmp_path / "note.foo"
    path.write_text("bad\n", encoding="utf-8")
    config = _server_config(tmp_path)
    manager = LSPManager.from_config(tmp_path, config)
    executor = ToolExecutor(
        workspace=tmp_path,
        session=Session(),
        get_config=lambda: config,
        lsp=manager,
    )
    try:
        result = await executor.execute("diagnostics", {"filePath": "note.foo"})
        assert result.ok is True
        assert "Boom" in result.output
        assert "via LSP" in result.summary
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_diagnostics_tool_falls_back_without_lsp(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=dict)
    result = await executor.execute("diagnostics", {"filePath": "ok.py"})
    assert result.ok is True
    assert "via LSP" not in result.summary


@pytest.mark.asyncio
async def test_terminate_kills_running_servers(tmp_path: Path) -> None:
    path = tmp_path / "note.foo"
    path.write_text("bad\n", encoding="utf-8")
    manager = LSPManager.from_config(tmp_path, _server_config(tmp_path))
    await manager.diagnostics_for(path)
    client = manager._clients["fake"]
    process = client.process
    assert process is not None
    manager.terminate()
    await process.wait()
    assert process.returncode is not None
