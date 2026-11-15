"""Skills tool/prompt, custom subagent definitions, and subagent tool wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from rbit.agent import AgentEngine
from rbit.models import ChatMessage, Session, ToolCallRecord, short_id
from rbit.skills import load_skills, skill_tool_schema, skills_prompt
from rbit.subagents import load_subagents
from rbit.tools import ToolExecutor


def _noop_hooks() -> tuple[Any, Any]:
    async def _rec(_rec: ToolCallRecord) -> None:
        return None

    async def _status(_status: str) -> None:
        return None

    return _rec, _status


@pytest.mark.asyncio
async def test_skill_tool_loads_body_with_args(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".rbit" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "deploy.md").write_text(
        "---\ndescription: Deploy helper\n---\nRun `deploy {args}` carefully.\n",
        encoding="utf-8",
    )
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=dict)
    result = await executor.execute("skill", {"name": "deploy", "args": "--prod"})
    assert result.ok is True
    assert "Deploy helper" in result.output
    assert "deploy --prod" in result.output


@pytest.mark.asyncio
async def test_skill_tool_unknown_lists_names(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".rbit" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "one.md").write_text("One body\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=dict)
    result = await executor.execute("skill", {"name": "nope"})
    assert result.ok is False
    assert "one" in result.output


@pytest.mark.asyncio
async def test_opencode_skill_md_layout_is_discovered(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".rbit" / "skills" / "testing"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\ndescription: Test runner\n---\nRun pytest.\n", encoding="utf-8")
    skills = load_skills(tmp_path)
    assert "testing" in skills
    assert skills["testing"]["description"] == "Test runner"


@pytest.mark.asyncio
async def test_skill_schema_and_prompt(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".rbit" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "fmt.md").write_text("---\ndescription: Format code\n---\nUse formatter.\n", encoding="utf-8")
    skills = load_skills(tmp_path)
    schema = skill_tool_schema(skills)
    assert schema is not None and schema["function"]["name"] == "skill"
    prompt = skills_prompt(skills)
    assert "fmt: Format code" in prompt


@pytest.mark.asyncio
async def test_skills_reach_system_prompt_and_tool_list(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".rbit" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "fmt.md").write_text("---\ndescription: Format code\n---\nUse formatter.\n", encoding="utf-8")
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session, get_config=dict)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {"api_key": "test", "api_base": "https://mock.local/v1"},
            http_client=client,
        )
        user_msg = ChatMessage(id=short_id(), role="user", content="hi")
        session.messages.append(user_msg)
        rec, status = _noop_hooks()
        reply = await engine.run_turn(
            session=session,
            user_msg=user_msg,
            agent_mode="build",
            model_id="openai/gpt-4.1",
            on_tool_start=rec,
            on_tool_end=rec,
            on_status=status,
        )

    assert reply.content == "ok"
    system = captured[0]["messages"][0]["content"]
    assert "Available skills" in system
    assert "fmt: Format code" in system
    tool_names = {tool["function"]["name"] for tool in captured[0]["tools"]}
    assert "skill" in tool_names


def test_custom_subagent_file_is_loaded(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".rbit" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "---\ndescription: Review changes\ntools: read, grep\n---\nYou review code carefully.\n",
        encoding="utf-8",
    )
    agents = load_subagents(tmp_path, {})
    assert "explore" in agents
    reviewer = agents["reviewer"]
    assert reviewer.description == "Review changes"
    assert reviewer.tools == {"read", "grep"}
    assert reviewer.read_only is True
    assert "You review code carefully." in reviewer.prompt


@pytest.mark.asyncio
async def test_custom_subagent_rejects_disallowed_tools(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".rbit" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "---\ndescription: Review changes\ntools: read\n---\nYou review code carefully.\n",
        encoding="utf-8",
    )
    requests: list[dict[str, Any]] = []
    step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal step
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        step += 1
        if step == 1:
            call = {
                "id": "c1",
                "type": "function",
                "function": {"name": "write", "arguments": '{"filePath":"x.txt","content":"no"}'},
            }
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [call]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "review done"}}]})

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session, get_config=dict)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {"api_key": "test", "api_base": "https://mock.local/v1"},
            http_client=client,
        )
        result = await engine._run_subagent(
            {"goal": "review", "agent": "reviewer"},
            model_id="gpt-4.1",
            api_base="https://mock.local/v1",
            api_key="test",
        )

    assert result.ok is True
    assert "review done" in result.output
    assert "subagent reviewer" in result.output
    assert not (tmp_path / "x.txt").exists()
    tool_msgs = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert "not allowed for subagent `reviewer`" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_unknown_subagent_lists_available(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=dict)
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=dict)
    result = await engine._run_subagent({"goal": "x", "agent": "ghost"}, model_id="", api_base="", api_key="")
    assert result.ok is False
    assert "explore" in result.output


@pytest.mark.asyncio
async def test_build_subagent_can_write(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".rbit" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "fixer.md").write_text(
        "---\nmode: build\ntools: write\n---\nYou fix code.\n",
        encoding="utf-8",
    )
    step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal step
        step += 1
        if step == 1:
            call = {
                "id": "c1",
                "type": "function",
                "function": {"name": "write", "arguments": '{"filePath":"fixed.txt","content":"ok\\n"}'},
            }
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [call]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "fixed"}}]})

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session, get_config=dict)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = AgentEngine(
            workspace=tmp_path,
            executor=executor,
            get_config=lambda: {"api_key": "test", "api_base": "https://mock.local/v1"},
            http_client=client,
        )
        result = await engine._run_subagent(
            {"goal": "fix", "agent": "fixer"},
            model_id="gpt-4.1",
            api_base="https://mock.local/v1",
            api_key="test",
        )

    assert result.ok is True
    assert (tmp_path / "fixed.txt").read_text(encoding="utf-8") == "ok\n"
