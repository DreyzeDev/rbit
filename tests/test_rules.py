"""AGENTS.md / CLAUDE.md discovery, instructions config, and system-prompt wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from rbit.agent import AgentEngine
from rbit.models import ChatMessage, Session, ToolCallRecord, short_id
from rbit.rules import load_rules
from rbit.tools import ToolExecutor


@pytest.mark.asyncio
async def test_project_agents_md_is_loaded(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("ALWAYS run pytest before finishing.\n", encoding="utf-8")
    text = await load_rules(tmp_path, {})
    assert "Project Instructions" not in text  # loader returns only the labelled block
    assert "<rules source=\"AGENTS.md\">" in text
    assert "ALWAYS run pytest" in text


@pytest.mark.asyncio
async def test_nearest_agents_md_wins_over_claude_md(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("AGENTS wins\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("CLAUDE loses\n", encoding="utf-8")
    text = await load_rules(tmp_path, {})
    assert "AGENTS wins" in text
    assert "CLAUDE loses" not in text


@pytest.mark.asyncio
async def test_claude_md_is_fallback(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("Claude rules\n", encoding="utf-8")
    text = await load_rules(tmp_path, {})
    assert "Claude rules" in text
    assert 'source="CLAUDE.md"' in text


@pytest.mark.asyncio
async def test_parent_directory_rules_are_found(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    (project / "AGENTS.md").write_text("Parent rule\n", encoding="utf-8")
    text = await load_rules(nested, {})
    assert "Parent rule" in text


@pytest.mark.asyncio
async def test_instructions_glob_and_relative_files(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("Doc A rule\n", encoding="utf-8")
    (tmp_path / "docs" / "b.md").write_text("Doc B rule\n", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("Contribution rule\n", encoding="utf-8")
    cfg = {"instructions": ["CONTRIBUTING.md", "docs/*.md"]}
    text = await load_rules(tmp_path, cfg)
    assert "Contribution rule" in text
    assert "Doc A rule" in text
    assert "Doc B rule" in text


@pytest.mark.asyncio
async def test_remote_instruction_is_fetched_with_timeout_client(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://rules.example.test/style.md"
        return httpx.Response(200, text="Remote style rule\n")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        text = await load_rules(
            tmp_path,
            {"instructions": ["https://rules.example.test/style.md"]},
            http_client=client,
        )
    finally:
        await client.aclose()
    assert "Remote style rule" in text
    assert 'source="https://rules.example.test/style.md"' in text


@pytest.mark.asyncio
async def test_missing_and_directories_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()
    text = await load_rules(tmp_path, {"instructions": ["missing.md", "folder", ""]})
    assert text == ""


@pytest.mark.asyncio
async def test_agents_md_reaches_the_system_prompt(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("PROJECT RULE: always use tabs.\n", encoding="utf-8")
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    session = Session()
    executor = ToolExecutor(workspace=tmp_path, session=session)
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
    system = captured[0]["messages"][0]["content"]
    assert "Project Instructions:" in system
    assert "PROJECT RULE: always use tabs." in system
