"""Permission rule tests: ordered allow/ask/deny, agents, external dirs, shell."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rbit.models import Session
from rbit.tools import ToolExecutor


def _executor(tmp_path: Path, cfg: dict[str, Any], **kwargs: Any) -> ToolExecutor:
    return ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: cfg, **kwargs)


@pytest.mark.asyncio
async def test_ordered_deny_wins_and_allow_still_works(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")
    cfg = {
        "allow_secret_reads": True,
        "permissions": [
            {"action": "read", "resource": "*", "effect": "allow"},
            {"action": "read", "resource": "*.env", "effect": "deny"},
        ],
    }
    executor = _executor(tmp_path, cfg)

    blocked = await executor.execute("read", {"filePath": ".env"})
    assert blocked.ok is False
    assert "Permission denied" in blocked.output
    assert "SECRET" not in blocked.output

    allowed = await executor.execute("read", {"filePath": "app.py"})
    assert allowed.ok is True
    assert "print(1)" in allowed.output


@pytest.mark.asyncio
async def test_last_matching_rule_wins(tmp_path: Path) -> None:
    (tmp_path / "src" / "secret").mkdir(parents=True)
    cfg = {
        "permissions": [
            {"action": "edit", "resource": "src/*", "effect": "allow"},
            {"action": "edit", "resource": "src/secret/*", "effect": "deny"},
            {"action": "edit", "resource": "src/secret/public.txt", "effect": "allow"},
        ]
    }
    executor = _executor(tmp_path, cfg)
    assert (await executor.execute("write", {"filePath": "src/a.py", "content": "a\n"})).ok is True
    assert (await executor.execute("write", {"filePath": "src/secret/x.py", "content": "x\n"})).ok is False
    assert (
        await executor.execute("write", {"filePath": "src/secret/public.txt", "content": "ok\n"})
    ).ok is True


@pytest.mark.asyncio
async def test_ask_calls_callback_and_can_reject(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("hello\n", encoding="utf-8")
    cfg = {"permissions": [{"action": "read", "resource": "*.md", "effect": "ask"}]}
    asked: list[tuple[str, str]] = []

    async def approve(tool: str, detail: str) -> bool:
        asked.append((tool, detail))
        return True

    executor = _executor(tmp_path, cfg, permission_cb=approve)
    result = await executor.execute("read", {"filePath": "a.md"})
    assert result.ok is True
    assert asked == [("read", "a.md")]

    async def reject(tool: str, detail: str) -> bool:
        return False

    executor.permission_cb = reject
    denied = await executor.execute("read", {"filePath": "a.md"})
    assert denied.ok is False
    assert "rejected by the user" in denied.output


@pytest.mark.asyncio
async def test_ask_without_client_is_denied(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("hello\n", encoding="utf-8")
    cfg = {"permissions": [{"action": "read", "resource": "*", "effect": "ask"}]}
    result = await _executor(tmp_path, cfg).execute("read", {"filePath": "a.md"})
    assert result.ok is False
    assert "no interactive client" in result.output


@pytest.mark.asyncio
async def test_agent_permissions_are_appended_last(tmp_path: Path) -> None:
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "x.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / "public.txt").write_text("ok\n", encoding="utf-8")
    cfg = {
        "permissions": [{"action": "read", "resource": "*", "effect": "allow"}],
        "agents": {"build": {"permissions": [{"action": "read", "resource": "private/*", "effect": "deny"}]}},
    }
    executor = _executor(tmp_path, cfg)
    assert (await executor.execute("read", {"filePath": "private/x.txt"}, "build")).ok is False
    assert (await executor.execute("read", {"filePath": "public.txt"}, "build")).ok is True


@pytest.mark.asyncio
async def test_external_directory_allow_enables_outside_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    cfg = {
        "permissions": [
            {"action": "external_directory", "resource": str(outside.resolve()) + "/*", "effect": "allow"},
            {"action": "edit", "resource": str(outside.resolve()) + "/*", "effect": "allow"},
        ]
    }
    executor = _executor(workspace, cfg)
    result = await executor.execute("write", {"filePath": "../outside/note.txt", "content": "hi\n"})
    assert result.ok is True, result
    assert (outside / "note.txt").read_text(encoding="utf-8") == "hi\n"

    denied_cfg = {
        "permissions": [{"action": "external_directory", "resource": "*", "effect": "deny"}],
    }
    denied = await _executor(workspace, denied_cfg).execute(
        "write", {"filePath": "../outside/blocked.txt", "content": "no\n"}
    )
    assert denied.ok is False
    assert "external path" in f"{denied.summary}\n{denied.output}"
    assert not (outside / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_shell_rule_denies_before_execution_and_allow_skips_prompt(tmp_path: Path) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("keep\n", encoding="utf-8")
    asked: list[str] = []

    async def approve(tool: str, detail: str) -> bool:
        asked.append(detail)
        return True

    cfg = {
        "permissions": [
            {"action": "shell", "resource": "*", "effect": "allow"},
            {"action": "shell", "resource": "rm *", "effect": "deny"},
        ]
    }
    executor = _executor(tmp_path, cfg, permission_cb=approve)
    blocked = await executor.execute("bash", {"command": "rm keep.txt"})
    assert blocked.ok is False
    assert target.exists()
    assert asked == []

    ok = await executor.execute("bash", {"command": "git status"})
    assert asked == []  # explicit allow rule, no prompt
    assert ok is not None


@pytest.mark.asyncio
async def test_legacy_permission_mapping(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    cfg = {"allow_secret_reads": True, "permission": {"read": {"*.env": "deny"}}}
    result = await _executor(tmp_path, cfg).execute("read", {"filePath": ".env"})
    assert result.ok is False


@pytest.mark.asyncio
async def test_apply_patch_checks_every_patched_path(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("two\n", encoding="utf-8")
    cfg = {
        "permissions": [
            {"action": "edit", "resource": "*", "effect": "allow"},
            {"action": "edit", "resource": "b.txt", "effect": "deny"},
        ]
    }
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+ONE\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-two\n+TWO\n"
    )
    executor = _executor(tmp_path, cfg)
    result = await executor.execute("apply_patch", {"patch": patch})
    assert result.ok is False
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "two\n"
