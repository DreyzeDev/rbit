"""Native Anthropic Messages API path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from rbit.agent import AgentEngine
from rbit.models import ChatMessage, Session, ToolCallRecord, short_id
from rbit.tools import ToolExecutor


@pytest.mark.asyncio
async def test_anthropic_native_tool_loop(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello-anthropic\n", encoding="utf-8")
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages")
        assert request.headers["x-api-key"] == "key"
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "read", "input": {"filePath": "a.txt"}}
                    ]
                },
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": "done"}]})

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session, get_config=lambda: {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {"api_key": "key", "protocol": "anthropic", "api_base": "https://api.anthropic.com/v1"},
            http_client=client,
        )
        user = ChatMessage(id=short_id(), role="user", content="read a.txt")
        session.messages.append(user)

        async def _noop(_record: ToolCallRecord) -> None:
            return None

        async def _status(_text: str) -> None:
            return None

        reply = await engine.run_turn(
            session=session,
            user_msg=user,
            agent_mode="build",
            model_id="anthropic/claude-sonnet-4-5",
            on_tool_start=_noop,
            on_tool_end=_noop,
            on_status=_status,
        )
    assert reply.content == "done"
    assert requests[0]["model"] == "claude-sonnet-4-5"
    assert requests[0]["tools"]
    assert requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    assert "hello-anthropic" in requests[1]["messages"][-1]["content"][0]["content"]

@pytest.mark.asyncio
async def test_gemini_native_tool_loop(tmp_path: Path) -> None:
    (tmp_path / "g.txt").write_text("hello-gemini\n", encoding="utf-8")
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "generateContent" in request.url.path
        assert request.url.params["key"] == "g-key"
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"functionCall": {"name": "read", "args": {"filePath": "g.txt"}}}]}}]},
            )
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "gemini done"}]}}]})

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session, get_config=lambda: {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {
                "api_key": "g-key",
                "protocol": "gemini",
                "api_base": "https://generativelanguage.googleapis.com/v1beta",
            },
            http_client=client,
        )
        user = ChatMessage(id=short_id(), role="user", content="read g.txt")
        session.messages.append(user)

        async def _noop(_record: ToolCallRecord) -> None:
            return None

        async def _status(_text: str) -> None:
            return None

        reply = await engine.run_turn(
            session=session,
            user_msg=user,
            agent_mode="build",
            model_id="google/gemini-2.5-pro",
            on_tool_start=_noop,
            on_tool_end=_noop,
            on_status=_status,
        )
    assert reply.content == "gemini done"
    assert requests[0]["tools"][0]["functionDeclarations"]
    parts = requests[1]["contents"][-1]["parts"]
    assert parts[0]["functionResponse"]["name"] == "read"
