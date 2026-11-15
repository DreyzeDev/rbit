"""ACP server protocol handling and OpenAPI document."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from rbit.acp import ACPServer
from rbit.cli import build_openapi_spec


def test_openapi_has_core_paths() -> None:
    spec = build_openapi_spec()
    assert spec["openapi"].startswith("3.")
    for path in (
        "/health",
        "/v1/models",
        "/v1/sessions",
        "/v1/turns",
        "/v1/artifacts",
        "/v1/schedules",
        "/v1/fs",
        "/v1/usage",
        "/v1/config",
        "/openapi.json",
    ):
        assert path in spec["paths"]


@pytest.mark.asyncio
async def test_acp_initialize_and_new_session(tmp_path: Path) -> None:
    server = ACPServer(workspace=tmp_path)
    notifications: list[dict[str, Any]] = []

    async def notify(payload: dict[str, Any]) -> None:
        notifications.append(payload)

    init = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        notify,
    )
    assert init is not None and init["result"]["agentInfo"]["name"] == "rbit"
    created = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path)}},
        notify,
    )
    assert created is not None
    session_id = created["result"]["sessionId"]
    assert session_id in server.sessions


@pytest.mark.asyncio
async def test_acp_prompt_streams_chunks(tmp_path: Path) -> None:
    chunks: list[str] = []

    async def runner(prompt: str, session_id: str, send_chunk: Any) -> str:
        assert prompt == "hello world"
        await send_chunk("Hel")
        await send_chunk("lo")
        chunks.extend(["Hel", "lo"])
        return "end_turn"

    server = ACPServer(runner=runner, workspace=tmp_path)
    notifications: list[dict[str, Any]] = []

    async def notify(payload: dict[str, Any]) -> None:
        notifications.append(payload)

    created = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}}, notify
    )
    session_id = created["result"]["sessionId"]
    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "hello world"}],
            },
        },
        notify,
    )
    assert response is not None and response["result"] == {"stopReason": "end_turn"}
    assert chunks == ["Hel", "lo"]
    updates = [n for n in notifications if n.get("method") == "session/update"]
    assert len(updates) == 2
    assert updates[0]["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert updates[0]["params"]["update"]["content"]["text"] == "Hel"


@pytest.mark.asyncio
async def test_acp_prompt_unknown_session_errors(tmp_path: Path) -> None:
    server = ACPServer(workspace=tmp_path)

    async def notify(_payload: dict[str, Any]) -> None:
        return None

    response = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": {"sessionId": "nope", "prompt": "hi"}},
        notify,
    )
    assert response is not None and response["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_acp_serve_reads_line_delimited_json(tmp_path: Path) -> None:
    async def runner(prompt: str, session_id: str, send_chunk: Any) -> str:
        await send_chunk(f"echo:{prompt}")
        return "end_turn"

    server = ACPServer(runner=runner, workspace=tmp_path)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
    stdout = io.StringIO()
    await server.serve(stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert lines[0]["result"]["agentInfo"]["name"] == "rbit"
    assert "sessionId" in lines[1]["result"]


def test_extract_prompt_merges_text_parts() -> None:
    assert ACPServer._extract_prompt([{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert ACPServer._extract_prompt("hi") == "hi"
    assert ACPServer._extract_prompt([]) == ""

@pytest.mark.asyncio
async def test_acp_modes_and_model_state(tmp_path: Path) -> None:
    server = ACPServer(workspace=tmp_path)

    async def notify(_payload: dict[str, Any]) -> None:
        return None

    created = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}}, notify
    )
    assert created["result"]["modes"]["currentModeId"] == "build"
    session_id = created["result"]["sessionId"]
    mode = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/set_mode",
            "params": {"sessionId": session_id, "modeId": "plan"},
        },
        notify,
    )
    assert mode["result"] is None
    assert server.modes[session_id] == "plan"
    model = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/set_model",
            "params": {"sessionId": session_id, "modelId": "openai/gpt-4.1"},
        },
        notify,
    )
    assert model["result"] is None
    assert server.models[session_id] == "openai/gpt-4.1"
