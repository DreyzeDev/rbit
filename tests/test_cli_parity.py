"""CLI parity features: service, api, plugin manager, auth, debug, stats, worktrees."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from rbit.agent import AgentEngine
from rbit.cli import (
    API_OPERATIONS,
    _create_worktree,
    _run_auth,
    _run_debug,
    _run_import,
    _run_stats,
)
from rbit.mcp import MCPManager
from rbit.models import ChatMessage, Session, ToolCallRecord, short_id
from rbit.plugin_manager import PluginManager
from rbit.service import ServiceManager, ServiceState, api_request
from rbit.storage import StorageManager
from rbit.tools import ToolExecutor


def test_service_settings_round_trip(tmp_path: Path) -> None:
    manager = ServiceManager(state_path=tmp_path / "service.json")
    assert manager.status()["running"] is False
    manager.set_setting("port", "9123")
    manager.set_setting("host", "127.0.0.1")
    assert manager.get_setting("port") == "9123"
    assert manager.get_setting("host") == "127.0.0.1"
    assert manager.unset_setting("host") is True
    assert manager.get_setting("host") is None
    assert manager.stop() is False
    assert manager.get_setting("port") == "9123"
    assert manager.stop() is False


def test_service_state_serialization(tmp_path: Path) -> None:
    manager = ServiceManager(state_path=tmp_path / "service.json")
    state = ServiceState(pid=123, port=8333, host="127.0.0.1", workspace=str(tmp_path), started_at=1.0)
    manager.write(state)
    loaded = manager.read()
    assert loaded is not None
    assert loaded.url == "http://127.0.0.1:8333"
    assert loaded.workspace == str(tmp_path)


def test_api_operations_cover_server(tmp_path: Path) -> None:
    for operation in ("health", "models.list", "sessions.list", "sessions.create", "turns.create"):
        assert operation in API_OPERATIONS


@pytest.mark.asyncio
async def test_api_request_against_local_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, text='{"status":"ok"}')

    original = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    status, body = api_request("http://localhost:9", "GET", "/health")
    assert status == 200
    assert "ok" in body


def test_plugin_manager_add_list_remove(tmp_path: Path) -> None:
    source = tmp_path / "my_plugin.py"
    source.write_text("def on_event(ctx):\n    return None\n", encoding="utf-8")
    manager = PluginManager(tmp_path)
    ok, message = manager.add(str(source))
    assert ok is True and "installed" in message
    entries = manager.list_plugins()
    assert len(entries) == 1
    target = entries[0]["target"]
    assert Path(target).exists()
    config = StorageManager(tmp_path).load_config()
    assert target in config["plugins"]
    ok, _ = manager.remove(str(source))
    assert ok is True
    assert manager.list_plugins() == []
    config = StorageManager(tmp_path).load_config()
    assert target not in config["plugins"]


def test_auth_switch_export_import(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    storage = StorageManager(tmp_path)
    cfg = storage.load_config()
    cfg["credentials"] = {
        "work": {"api_base": "https://work.example/v1", "api_key": "secret-work", "model": "gpt-4.1"},
        "home": {"api_base": "https://home.example/v1", "api_key": "secret-home", "model": "gpt-4o"},
    }
    cfg["active_credential"] = ""
    storage.save_config(cfg)

    assert _run_auth(tmp_path, "switch", None, name="work") == 0
    cfg = storage.load_config()
    assert cfg["api_base"] == "https://work.example/v1"
    assert cfg["api_key"] == "secret-work"
    assert cfg["active_credential"] == "work"

    assert _run_auth(tmp_path, "list", None) == 0
    capsys.readouterr()

    export_target = tmp_path / "creds.json"
    assert _run_auth(tmp_path, "export", None, file=str(export_target)) == 0
    data = json.loads(export_target.read_text(encoding="utf-8"))
    assert data["home"]["api_key"] == "secret-home"

    cfg = storage.load_config()
    cfg["credentials"] = {}
    storage.save_config(cfg)
    assert _run_auth(tmp_path, "import", None, file=str(export_target)) == 0
    cfg = storage.load_config()
    assert set(cfg["credentials"]) == {"work", "home"}


def test_debug_paths_and_agents(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / ".rbit" / "agents").mkdir(parents=True)
    (tmp_path / ".rbit" / "agents" / "reviewer.md").write_text(
        "---\ndescription: Reviews\n---\nReview carefully.\n", encoding="utf-8"
    )
    from argparse import Namespace

    assert _run_debug(Namespace(dir=str(tmp_path), debug_cmd="agents", output=None)) == 0
    assert "reviewer" in capsys.readouterr().out
    assert _run_debug(Namespace(dir=str(tmp_path), debug_cmd="paths", output=None)) == 0
    assert "workspace" in capsys.readouterr().out


def test_stats_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    session = Session(title="stats")
    session.messages.append(
        ChatMessage(
            id=short_id(),
            role="assistant",
            content="done",
            model="openai/gpt-4.1",
            tokens=1000,
            tool_calls=[
                ToolCallRecord(
                    id="tc1",
                    name="read",
                    arguments={"filePath": "a.py"},
                    status="success",
                    summary="ok",
                )
            ],
        )
    )
    StorageManager(tmp_path).save_session(session)
    assert _run_stats(tmp_path, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"] == 1
    assert payload["tool_calls"] == 1
    assert payload["tools"]["read"]["ok"] == 1


def test_session_archive_filter(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    visible = Session(title="visible")
    hidden = Session(title="hidden", archived=True)
    storage.save_session(visible)
    storage.save_session(hidden)
    assert [s.id for s in storage.list_sessions()] == [visible.id]
    assert {s.id for s in storage.list_sessions(include_archived=True)} == {visible.id, hidden.id}
    assert storage.find_session(hidden.id) is not None


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def test_create_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    created = _create_worktree(repo, "feature")
    assert created is not None
    assert created.exists()
    assert (created / "a.txt").read_text(encoding="utf-8") == "a\n"


def test_mcp_oauth_token_is_injected(tmp_path: Path) -> None:
    config = {
        "mcp": {"remote": {"type": "remote", "url": "https://example.test/mcp"}},
        "mcp_oauth": {"remote": "tok-123"},
    }
    manager = MCPManager.from_config(config, tmp_path)
    assert manager.clients["remote"].headers["Authorization"] == "Bearer tok-123"


@pytest.mark.asyncio
async def test_auto_approve_permission_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RBIT_AUTO_APPROVE", "1")
    config = {"permissions": [{"action": "edit", "resource": "*", "effect": "ask"}]}
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: config)
    result = await executor.execute("write", {"filePath": "a.txt", "content": "ok\n"})
    assert result.ok is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "ok\n"

def test_import_markdown_and_json_autodetect(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    md = tmp_path / "chat.md"
    md.write_text("## User\nhello\n## Assistant\nhi there\n", encoding="utf-8")
    assert _run_import(tmp_path, str(md)) == 0
    sessions = StorageManager(tmp_path).list_sessions()
    assert len(sessions) == 1
    assert [m.content for m in sessions[0].messages] == ["hello", "hi there"]

    data = tmp_path / "chat.data"
    data.write_text(json.dumps([{"role": "user", "content": "json hello"}]), encoding="utf-8")
    assert _run_import(tmp_path, str(data)) == 0
    assert len(StorageManager(tmp_path).list_sessions()) == 2


@pytest.mark.asyncio
async def test_custom_agent_prompt_and_tool_filter(tmp_path: Path) -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    cfg = {
        "api_key": "test",
        "api_base": "https://mock.local/v1",
        "agent_prompt": "CUSTOM AGENT PROMPT",
        "agent_tools": ["read"],
    }
    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session, get_config=lambda: cfg)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = AgentEngine(
            workspace=tmp_path, executor=executor, get_config=lambda: cfg, http_client=client
        )
        user = ChatMessage(id=short_id(), role="user", content="hi")
        session.messages.append(user)

        async def _noop(_rec: ToolCallRecord) -> None:
            return None

        async def _status(_text: str) -> None:
            return None

        await engine.run_turn(
            session=session,
            user_msg=user,
            agent_mode="reviewer",
            model_id="gpt-4.1",
            on_tool_start=_noop,
            on_tool_end=_noop,
            on_status=_status,
        )
    assert "CUSTOM AGENT PROMPT" in captured[0]["messages"][0]["content"]
    assert {tool["function"]["name"] for tool in captured[0]["tools"]} == {"read"}

@pytest.mark.asyncio
async def test_model_variant_suffix_sets_effort_and_strips_model(tmp_path: Path) -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    cfg = {"api_key": "test", "api_base": "https://mock.local/v1", "effort": "auto"}
    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session, get_config=lambda: cfg)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: cfg, http_client=client)
        user = ChatMessage(id=short_id(), role="user", content="hi")
        session.messages.append(user)

        async def _noop(_rec: ToolCallRecord) -> None:
            return None

        async def _status(_text: str) -> None:
            return None

        await engine.run_turn(
            session=session,
            user_msg=user,
            agent_mode="build",
            model_id="deepseek/deepseek-reasoner#high",
            on_tool_start=_noop,
            on_tool_end=_noop,
            on_status=_status,
        )
    assert captured[0]["model"] == "deepseek/deepseek-reasoner"
    assert captured[0]["reasoning_effort"] == "high"
