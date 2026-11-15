"""MCP client tests: stdio transport, HTTP transport, tool exposure and permissions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from rbit.mcp import MCPManager
from rbit.models import Session
from rbit.tools import ToolExecutor

FAKE_STDIO_SERVER = r'''
import json
import sys


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail",
        "description": "Always fails",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1.0"},
        }})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}],
                "isError": False,
            }})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "boom"}],
                "isError": True,
            }})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unsupported"}})
'''


@pytest.mark.asyncio
async def test_stdio_server_exposes_tools_and_executes_calls(tmp_path: Path) -> None:
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_STDIO_SERVER, encoding="utf-8")
    cfg = {
        "mcp": {
            "demo": {
                "type": "local",
                "command": [sys.executable, str(script)],
                "timeout": 10,
                "environment": {"MCP_TEST": "1"},
            }
        }
    }
    manager = MCPManager.from_config(cfg, tmp_path)
    await manager.start()
    try:
        status = manager.status["demo"]
        assert status["connected"] is True, status
        assert status["transport"] == "stdio"
        assert manager.tool_count == 2
        assert manager.tool_names == {"demo_echo", "demo_fail"}

        executor = ToolExecutor(workspace=tmp_path, session=Session(), mcp=manager)
        schemas = executor.mcp_tool_schemas()
        assert {s["function"]["name"] for s in schemas} == {"demo_echo", "demo_fail"}
        assert "Echo text back" in next(s for s in schemas if s["function"]["name"] == "demo_echo")["function"]["description"]

        ok = await executor.execute("demo_echo", {"text": "hello"})
        assert ok.ok is True
        assert "echo: hello" in ok.output

        failed = await executor.execute("demo_fail", {})
        assert failed.ok is False
        assert "boom" in failed.output
    finally:
        await manager.close()

    assert manager.status["demo"]["connected"] is True


@pytest.mark.asyncio
async def test_http_server_uses_headers_and_parses_json(tmp_path: Path) -> None:
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append({k.lower(): v for k, v in request.headers.items()})
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        rid = body.get("id")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "remote-fake", "version": "1"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "tools": [
                            {
                                "name": "lookup",
                                "description": "Look something up",
                                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                            }
                        ]
                    },
                },
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"content": [{"type": "text", "text": "result: " + str(body["params"]["arguments"]["q"])}]},
                },
            )
        return httpx.Response(500, text="unexpected")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = {
        "mcp": {
            "remote": {
                "type": "remote",
                "url": "https://mcp.example.test/rpc",
                "headers": {"Authorization": "Bearer token"},
            }
        }
    }
    manager = MCPManager.from_config(cfg, tmp_path, http_client=client)
    try:
        await manager.start()
        assert manager.status["remote"]["connected"] is True, manager.status
        assert manager.status["remote"]["transport"] == "http"
        assert manager.tool_names == {"remote_lookup"}
        assert any(h.get("authorization") == "Bearer token" for h in seen_headers)
        assert any("text/event-stream" in h.get("accept", "") for h in seen_headers)
        result = await manager.call("remote_lookup", {"q": "x"})
        assert result.ok is True
        assert "result: x" in result.output
    finally:
        await manager.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_stdio_failure_is_reported_without_crashing(tmp_path: Path) -> None:
    cfg = {"mcp": {"broken": {"type": "local", "command": ["definitely-not-a-real-command-rbit"]}}}
    manager = MCPManager.from_config(cfg, tmp_path)
    await manager.start()
    try:
        status = manager.status["broken"]
        assert status["connected"] is False
        assert status["error"]
        assert manager.tool_count == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_disabled_server_is_not_started(tmp_path: Path) -> None:
    cfg = {"mcp": {"off": {"type": "local", "command": [sys.executable, "-c", "pass"], "enabled": False}}}
    manager = MCPManager.from_config(cfg, tmp_path)
    await manager.start()
    try:
        assert manager.status["off"]["connected"] is False
        assert manager.status["off"]["error"] == "disabled"
        assert manager.tool_count == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_readonly_session_blocks_mcp_tools(tmp_path: Path) -> None:
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_STDIO_SERVER, encoding="utf-8")
    cfg = {"mcp": {"demo": {"command": [sys.executable, str(script)]}}}
    manager = MCPManager.from_config(cfg, tmp_path)
    await manager.start()
    try:
        executor = ToolExecutor(workspace=tmp_path, session=Session(), mcp=manager)
        executor.session_readonly = True
        result = await executor.execute("demo_echo", {"text": "x"})
        assert result.ok is False
        assert "read-only" in result.summary.lower()
    finally:
        await manager.close()


def test_legacy_mcp_servers_string_config_is_normalized(tmp_path: Path) -> None:
    manager = MCPManager.from_config({"mcp_servers": {"legacy": "python server.py"}}, tmp_path)
    assert manager.configured_count == 1
    assert Path(manager.clients["legacy"].command[0]).name.lower().startswith("python")
    assert manager.clients["legacy"].transport == "stdio"
