"""Web search tool: DuckDuckGo parsing, limits, cache, and disable switch."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rbit.models import Session
from rbit.tools import ToolExecutor

HTML = """
<html><body>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&rut=x">First result</a>
  <a class="result__snippet">Snippet one about Python.</a>
</div>
<div class="result">
  <a class="result__a" href="https://example.org/two">Second result</a>
  <a class="result__snippet">Snippet two about testing.</a>
</div>
</body></html>
"""


@pytest.mark.asyncio
async def test_websearch_parses_results(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = ToolExecutor(
            workspace=tmp_path,
            session=Session(),
            get_config=dict,
            http_client=client,
        )
        result = await executor.execute("websearch", {"query": "python testing", "num_results": 5})

    assert result.ok is True
    assert "First result" in result.output
    assert "https://example.com/one" in result.output
    assert "Snippet two" in result.output
    assert "Second result" in result.output
    assert calls and calls[0].url.params["q"] == "python testing"


@pytest.mark.asyncio
async def test_websearch_respects_limit(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=dict, http_client=client)
        result = await executor.execute("websearch", {"query": "x", "num_results": 1})
    assert result.ok is True
    assert "Second result" not in result.output


@pytest.mark.asyncio
async def test_websearch_can_be_disabled(tmp_path: Path) -> None:
    executor = ToolExecutor(
        workspace=tmp_path,
        session=Session(),
        get_config=lambda: {"websearch": False},
    )
    result = await executor.execute("websearch", {"query": "x"})
    assert result.ok is False
    assert "disabled" in result.summary


@pytest.mark.asyncio
async def test_websearch_requires_query(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=dict)
    result = await executor.execute("websearch", {})
    assert result.ok is False
    assert "query" in result.output


@pytest.mark.asyncio
async def test_websearch_cache_avoids_second_request(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=dict, http_client=client)
        first = await executor.execute("websearch", {"query": "cache me"})
        second = await executor.execute("websearch", {"query": "cache me"})
    assert first.ok is True and second.ok is True
    assert calls["n"] == 1
    assert second.summary.startswith("cached")

@pytest.mark.asyncio
async def test_tavily_provider(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.tavily.com"
        body = json.loads(request.content.decode("utf-8"))
        assert body["query"] == "agentic coding"
        return httpx.Response(
            200,
            json={"results": [{"title": "Tavily hit", "url": "https://t.example/a", "content": "Snippet A"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = ToolExecutor(
            workspace=tmp_path,
            session=Session(),
            get_config=lambda: {"websearch": {"provider": "tavily", "api_key": "k"}},
            http_client=client,
        )
        result = await executor.execute("websearch", {"query": "agentic coding"})
    assert result.ok is True
    assert "Tavily hit" in result.output
    assert "https://t.example/a" in result.output


@pytest.mark.asyncio
async def test_exa_provider(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "exa-key"
        return httpx.Response(
            200,
            json={"results": [{"title": "Exa hit", "url": "https://e.example/a", "text": "Exa snippet"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = ToolExecutor(
            workspace=tmp_path,
            session=Session(),
            get_config=lambda: {"websearch": {"provider": "exa", "api_key": "exa-key"}},
            http_client=client,
        )
        result = await executor.execute("websearch", {"query": "x"})
    assert result.ok is True
    assert "Exa hit" in result.output


@pytest.mark.asyncio
async def test_missing_provider_key_falls_back_to_ddg(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(401, text="no key")
        return httpx.Response(200, text=HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = ToolExecutor(
            workspace=tmp_path,
            session=Session(),
            get_config=lambda: {"websearch": {"provider": "tavily"}},
            http_client=client,
        )
        result = await executor.execute("websearch", {"query": "fallback"})
    assert result.ok is True
    assert "First result" in result.output
