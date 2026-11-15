"""Image generation tool via OpenAI-compatible images API."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from rbit.models import Session
from rbit.tools import ToolExecutor


@pytest.mark.asyncio
async def test_image_generate_saves_artifact(tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\nfake"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/images/generations")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode("ascii")}]})

    config = {"api_key": "k", "api_base": "https://api.example/v1"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: config, http_client=client)
        result = await executor.execute("image_generate", {"prompt": "a tree", "name": "tree.png"})
    assert result.ok is True
    path = tmp_path / ".rbit" / "artifacts" / "tree.png"
    assert path.read_bytes() == png
