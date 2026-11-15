"""Plugin hooks: local modules, before/after tool hooks, chat.params, legacy loader."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from rbit.agent import AgentEngine
from rbit.models import ChatMessage, Session, ToolCallRecord, short_id
from rbit.plugins import PluginRegistry, load_plugins
from rbit.tools import ToolExecutor

PLUGIN = r'''
def on_tool_before(ctx):
    if ctx["tool"] == "bash":
        ctx["deny"] = True
        ctx["reason"] = "bash is disabled by policy plugin"

def on_tool_after(ctx):
    if ctx["tool"] == "write":
        ctx["result"].output += "\n[plugin] checked"

def on_chat_params(ctx):
    ctx["payload"]["temperature"] = 0.33

def setup(app):
    app.plugin_setup_called = True
'''


def _plugin(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / "policy_plugin.py"
    path.write_text(PLUGIN, encoding="utf-8")
    return path, {"plugins": [str(path)]}


@pytest.mark.asyncio
async def test_local_plugin_hooks_in_tool_executor(tmp_path: Path) -> None:
    path, config = _plugin(tmp_path)
    app = SimpleNamespace(workspace=tmp_path)
    registry = PluginRegistry()
    loaded = load_plugins(app, config, registry)
    assert loaded == [str(path)]
    assert app.plugin_setup_called is True

    executor = ToolExecutor(
        workspace=tmp_path,
        session=Session(),
        get_config=lambda: config,
        plugins=registry,
    )
    denied = await executor.execute("bash", {"command": "echo hi"})
    assert denied.ok is False
    assert "disabled by policy plugin" in denied.output

    written = await executor.execute("write", {"filePath": "a.txt", "content": "hi\n"})
    assert written.ok is True
    assert "[plugin] checked" in written.output


@pytest.mark.asyncio
async def test_chat_params_hook_mutates_request(tmp_path: Path) -> None:
    _path, config = _plugin(tmp_path)
    registry = PluginRegistry()
    load_plugins(SimpleNamespace(workspace=tmp_path), config, registry)
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session, get_config=lambda: config, plugins=registry)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {"api_key": "test", "api_base": "https://mock.local/v1"},
            http_client=client,
        )
        user_msg = ChatMessage(id=short_id(), role="user", content="hi")
        session.messages.append(user_msg)

        async def _noop_rec(_rec: ToolCallRecord) -> None:
            return None

        async def _noop_status(_status: str) -> None:
            return None

        reply = await engine.run_turn(
            session=session,
            user_msg=user_msg,
            agent_mode="build",
            model_id="openai/gpt-4.1",
            on_tool_start=_noop_rec,
            on_tool_end=_noop_rec,
            on_status=_noop_status,
        )

    assert reply.content == "ok"
    assert captured[0]["temperature"] == 0.33


def test_load_plugins_without_config_returns_empty() -> None:
    assert load_plugins(object()) == []


def test_missing_plugin_file_is_skipped(tmp_path: Path) -> None:
    registry = PluginRegistry()
    loaded = load_plugins(SimpleNamespace(workspace=tmp_path), {"plugins": ["missing_plugin.py"]}, registry)
    assert loaded == []
