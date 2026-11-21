"""Regression tests for the agent tool fixes in rbit/tools.py and friends.

Each block names the review item it pins down: shell gate, read-only detection,
deny/allow rules, protected paths, SSRF, secret files, checkpoints, encoding
safety, edit/patch/glob/grep correctness, notebooks and formatter approval.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from rbit.formatters import build_formatters, formatter_is_custom
from rbit.models import Session
from rbit.tools import (
    ToolExecutor,
    ToolResult,
    UnsafeURL,
    check_url_safe,
    is_dangerous_command,
    is_readonly_bash_command,
    is_secret_file,
    redact_secrets,
    safe_http_get,
)


def _executor(
    tmp_path: Path,
    cfg: dict[str, Any] | None = None,
    cb: Any = None,
) -> ToolExecutor:
    config = cfg if cfg is not None else {}
    return ToolExecutor(
        workspace=tmp_path,
        session=Session(),
        get_config=lambda: config,
        permission_cb=cb,
    )


async def _yes(tool: str, resource: str) -> bool:
    return True


async def _no(tool: str, resource: str) -> bool:
    return False


# --- 1-2: shell gate shared by bash/terminal, git bisect -------------------------


@pytest.mark.asyncio
async def test_terminal_refuses_mutations_without_prompt_channel(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    result = await ex.execute("terminal", {"command": "touch marker.txt"})
    assert result.ok is False
    assert "no interactive client" in result.output
    assert not (tmp_path / "marker.txt").exists()


@pytest.mark.asyncio
async def test_terminal_uses_gate_dangerous_and_plan_mode(tmp_path: Path) -> None:
    ex = _executor(tmp_path, cb=_yes)
    dangerous = await ex.execute("terminal", {"command": "rm -rf /"})
    assert dangerous.ok is False and "dangerous" in dangerous.summary
    plan = await ex.execute("terminal", {"command": "touch x.txt"}, agent_mode="plan")
    assert plan.ok is False and "Plan mode" in plan.summary


@pytest.mark.asyncio
async def test_terminal_asks_user_when_prompt_rejected(tmp_path: Path) -> None:
    ex = _executor(tmp_path, cb=_no)
    result = await ex.execute("terminal", {"command": "touch y.txt"})
    assert result.ok is False
    assert not (tmp_path / "y.txt").exists()


@pytest.mark.asyncio
async def test_git_bisect_blocked_in_plan_and_refused_without_prompt(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    plan = await ex.execute("git", {"action": "bisect", "subaction": "start"}, agent_mode="plan")
    assert plan.ok is False and "Plan mode" in plan.summary
    readonly_ex = _executor(tmp_path)
    readonly_ex.session_readonly = True
    ro = await readonly_ex.execute("git", {"action": "bisect", "subaction": "good", "rev": "HEAD"})
    assert ro.ok is False and "read-only" in ro.summary
    build = await ex.execute("git", {"action": "bisect", "subaction": "start"})
    assert build.ok is False and "needs approval" in build.output


@pytest.mark.asyncio
async def test_git_bisect_run_is_rejected(tmp_path: Path) -> None:
    ex = _executor(tmp_path, cb=_yes)
    result = await ex.execute("git", {"action": "bisect", "subaction": "run", "rev": "x"})
    assert result.ok is False


# --- 3: read-only detection ------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "ls & rm x",
        "ls $(rm x)",
        "echo `rm x`",
        "cat <(rm x)",
        "echo x; rm y",
        "ls > f",
        "ls\nrm x",
        "ls | rm",
        "git diff --output=out.txt",
        "git branch -D feature",
        "pytest",
    ],
)
def test_readonly_detection_rejects_mutating_forms(command: str) -> None:
    assert is_readonly_bash_command(command) is False


@pytest.mark.parametrize("command", ["ls", "git status", "git branch", "git branch --show-current"])
def test_readonly_detection_accepts_plain_reads(command: str) -> None:
    assert is_readonly_bash_command(command) is True


def test_readonly_matches_whole_tokens_only() -> None:
    assert is_readonly_bash_command("lsof") is False


# --- 4: prefix allow-list --------------------------------------------------------


def test_prefix_allow_matches_tokens_and_rejects_chaining(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    ex.allowed_bash_prefixes = ["npm test"]
    assert ex._prefix_allowed("npm test --silent")
    assert not ex._prefix_allowed("npm testing")
    for chained in ("npm test; rm x", "npm test\nrm x", "npm test $(x)", "npm test > f", "npm test & rm x"):
        assert not ex._prefix_allowed(chained), chained


def test_empty_prefix_allows_nothing(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    ex.allowed_bash_prefixes = [""]
    assert not ex._prefix_allowed("rm -rf build")


# --- 5: deny rules over normalised shell statements ------------------------------


@pytest.mark.parametrize(
    "command",
    ["sudo  rm   -rf x", "FOO=1 rm -rf x", "ls; rm -rf x", "env A=1 git  push origin"],
)
def test_deny_rule_sees_through_wrappers_and_chains(tmp_path: Path, command: str) -> None:
    cfg = {"permissions": [{"action": "shell", "resource": "rm *", "effect": "deny"}]}
    cfg["permissions"].append({"action": "shell", "resource": "git push *", "effect": "deny"})
    ex = _executor(tmp_path, cfg)
    assert ex.resolve_permission_effect("shell", command) == "deny"


def test_deny_rule_does_not_touch_unrelated_commands(tmp_path: Path) -> None:
    cfg = {"permissions": [{"action": "shell", "resource": "rm *", "effect": "deny"}]}
    ex = _executor(tmp_path, cfg)
    assert ex.resolve_permission_effect("shell", "ls") is None


# --- 6: dangerous command list ---------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "rm -r -f /",
        "rm --recursive --force /",
        "rm --no-preserve-root -rf /",
        "rm -rf /*",
        "rm -rf ~/",
        'rm -rf "$HOME"',
        "rd /s /q C:\\",
        "Remove-Item -Recurse -Force C:\\",
        "git push +main",
        "git push -fu origin main",
        "sudo  rm   -rf   /",
        "FOO=1 git  push --force",
        "mkfs.ext4 /dev/sda",
        "echo ok; shutdown now",
    ],
)
def test_dangerous_commands_are_refused(command: str) -> None:
    assert is_dangerous_command(command)


@pytest.mark.parametrize("command", ["grep shutdown log", "npm run shutdown", "rm -rf build", "git push origin main"])
def test_dangerous_list_has_no_false_positives(command: str) -> None:
    assert is_dangerous_command(command) == ""


# --- 7: protected agent paths ----------------------------------------------------


@pytest.mark.asyncio
async def test_protected_paths_need_approval_even_in_auto_mode(tmp_path: Path) -> None:
    ex = _executor(tmp_path, cfg={"permission_mode": "auto"}, cb=_no)
    for rel in (".rbit/config.json", ".rbit/audit/a.jsonl", ".git/HEAD"):
        result = await ex.execute("write", {"filePath": rel, "content": "x"})
        assert result.ok is False, rel
        assert not (tmp_path / rel).exists()


@pytest.mark.asyncio
async def test_protected_paths_refused_without_prompt_channel(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    result = await ex.execute("write", {"filePath": ".rbit/config.json", "content": "{}"})
    assert result.ok is False


@pytest.mark.asyncio
async def test_ordinary_files_still_writable_in_auto_mode(tmp_path: Path) -> None:
    ex = _executor(tmp_path, cfg={"permission_mode": "auto"}, cb=_no)
    result = await ex.execute("write", {"filePath": "notes.txt", "content": "hi"})
    assert result.ok is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hi"


# --- 8: SSRF guard ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8000",
        "http://169.254.169.254/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://[fc00::1]/",
        "http://2130706433/",
    ],
)
async def test_ssrf_guard_blocks_private_hosts(url: str) -> None:
    assert await check_url_safe(url) is not None


@pytest.mark.asyncio
async def test_ssrf_guard_allows_public_literal() -> None:
    assert await check_url_safe("https://93.184.216.34/") is None


@pytest.mark.asyncio
async def test_redirect_hops_are_rechecked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9/admin"})
        return httpx.Response(200, text="should not be reached")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeURL):
            await safe_http_get(client, "https://93.184.216.34/start")


@pytest.mark.asyncio
async def test_webfetch_refuses_loopback(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    result = await ex.execute("webfetch", {"url": "http://127.0.0.1:1/"})
    assert result.ok is False and "unsafe URL" in result.summary


@pytest.mark.asyncio
async def test_schedule_create_blocked_in_plan_mode(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    result = await ex.execute("schedule", {"action": "create", "prompt": "x", "schedule": "daily"}, agent_mode="plan")
    assert result.ok is False


# --- 9: secret files and redaction -----------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".env::$DATA",
        ".env ",
        ".env.",
        "home/.aws/credentials",
        ".git-credentials",
        ".pgpass",
        ".htpasswd",
        ".docker/config.json",
        "infra/a.tfstate",
        "infra/a.tfstate.backup",
        ".rbit/config.json",
        ".rbit/config.backup-1.json",
    ],
)
def test_secret_files_detected(name: str) -> None:
    assert is_secret_file(Path(name)) is True


@pytest.mark.parametrize("name", [".env.example", "src/app.py", ".rbit/notes.md"])
def test_ordinary_files_not_flagged(name: str) -> None:
    assert is_secret_file(Path(name)) is False


def test_code_redaction_keeps_function_calls() -> None:
    assert redact_secrets("PASSWORD = load_password()", code=True) == "PASSWORD = load_password()"
    assert "hunter2hunter2" not in redact_secrets("PASSWORD = 'hunter2hunter2'", code=True)


def test_plain_redaction_still_masks_literal_secrets() -> None:
    assert "abcdef123456789" not in redact_secrets("API_KEY=abcdef123456789xyz")


# --- 10: checkpoints -------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_removes_files_created_after_checkpoint(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    (tmp_path / "keep.txt").write_text("v1", encoding="utf-8")
    meta = ex.create_checkpoint("baseline")
    (tmp_path / "keep.txt").write_text("v2", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("new", encoding="utf-8")
    message = ex.restore_checkpoint(meta.id)
    assert "Rewound" in message
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "v1"
    assert not (tmp_path / "extra.txt").exists()
    assert ex.undo_stack == [] and ex.redo_stack == []


def test_restore_keeps_extra_files_when_manifest_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ex = _executor(tmp_path)
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    monkeypatch.setattr(ex, "MAX_LISTED_FILES", 1)
    meta = ex.create_checkpoint("truncated")
    monkeypatch.undo()
    (tmp_path / "later.txt").write_text("later", encoding="utf-8")
    message = ex.restore_checkpoint(meta.id)
    assert (tmp_path / "later.txt").exists()
    assert "truncated" in message


def test_restore_never_follows_symlinks_out_of_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("keep", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    ex = _executor(ws)
    try:
        os.symlink(outside, ws / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    listed = ex.list_workspace_files(limit=100)
    assert not any(item.startswith("link") for item in listed)


# --- 11: encoding and line endings -----------------------------------------------


def test_write_undo_restores_cp1251_bytes_exactly(tmp_path: Path) -> None:
    original = "привет\r\nмир\n".encode("cp1251")
    target = tmp_path / "cp.txt"
    target.write_bytes(original)
    ex = _executor(tmp_path)
    result = asyncio.run(ex.execute("write", {"filePath": "cp.txt", "content": "новое\nтекст\n"}))
    assert result.ok is True
    ex.undo_last()
    assert target.read_bytes() == original


def test_mixed_line_endings_survive_edit(tmp_path: Path) -> None:
    target = tmp_path / "mixed.txt"
    target.write_bytes(b"one\r\ntwo\nthree\r\nfour\n")
    ex = _executor(tmp_path)
    result = asyncio.run(ex.execute("edit", {"filePath": "mixed.txt", "oldString": "two", "newString": "TWO"}))
    assert result.ok is True
    data = target.read_bytes()
    assert data.startswith(b"one\r\nTWO\n")
    assert b"three\r\nfour\n" in data


def test_file_mode_is_kept_on_edit(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits")
    target = tmp_path / "run.sh"
    target.write_text("echo a\n", encoding="utf-8")
    os.chmod(target, 0o755)
    ex = _executor(tmp_path)
    asyncio.run(ex.execute("edit", {"filePath": "run.sh", "oldString": "echo a", "newString": "echo b"}))
    assert os.stat(target).st_mode & 0o777 == 0o755


def test_undo_does_not_overwrite_external_edit(tmp_path: Path) -> None:
    target = tmp_path / "doc.txt"
    target.write_text("one\n", encoding="utf-8")
    ex = _executor(tmp_path)
    asyncio.run(ex.execute("edit", {"filePath": "doc.txt", "oldString": "one", "newString": "two"}))
    target.write_text("someone else\n", encoding="utf-8")
    message = ex.undo_last()
    assert "Cannot undo" in message
    assert target.read_text(encoding="utf-8") == "someone else\n"


# --- 12: edit semantics ----------------------------------------------------------


def test_replace_all_replaces_every_occurrence(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\nif x:\n    y = 2\n    y = 2\n", encoding="utf-8")
    ex = _executor(tmp_path)
    result = asyncio.run(
        ex.execute("edit", {"filePath": "a.py", "oldString": "y = 2", "newString": "z = 3", "replaceAll": True})
    )
    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "x = 1\nif x:\n    z = 3\n    z = 3\n"


def test_fuzzy_edit_keeps_indentation_of_file(tmp_path: Path) -> None:
    target = tmp_path / "b.py"
    target.write_text("def f():\n    if a:\n        return 1\n", encoding="utf-8")
    ex = _executor(tmp_path)
    result = asyncio.run(
        ex.execute("edit", {"filePath": "b.py", "oldString": "if a:\n    return 1", "newString": "if a:\n    return 2"})
    )
    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "def f():\n    if a:\n        return 2\n"


# --- 13: apply_patch -------------------------------------------------------------


def test_apply_patch_accepts_unnumbered_hunk_header(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")
    ex = _executor(tmp_path)
    patch = "@@ def foo(): @@\n def foo():\n-    return 1\n+    return 2\n"
    result = ex._tool_apply_patch({"filePath": "m.py", "patch": patch})
    assert result.ok is True, result.output
    assert "return 2" in target.read_text(encoding="utf-8")


def test_apply_patch_multi_hunk_offsets_accumulate(tmp_path: Path) -> None:
    target = tmp_path / "n.txt"
    target.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")
    ex = _executor(tmp_path)
    patch = (
        "@@ -2,1 +2,3 @@\n-line2\n+a\n+b\n+c\n"
        "@@ -8,1 +10,1 @@\n-line8\n+LINE8\n"
    )
    result = ex._tool_apply_patch({"filePath": "n.txt", "patch": patch})
    assert result.ok is True, result.output
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[1:4] == ["a", "b", "c"]
    assert "LINE8" in lines


def test_apply_patch_treats_comment_like_lines_as_body(tmp_path: Path) -> None:
    target = tmp_path / "sql.txt"
    target.write_text("-- old comment\nSELECT 1;\n", encoding="utf-8")
    ex = _executor(tmp_path)
    patch = "@@ -1,2 +1,2 @@\n--- old comment\n+-- new comment\n SELECT 1;\n"
    result = ex._tool_apply_patch({"filePath": "sql.txt", "patch": patch})
    assert result.ok is True, result.output
    assert target.read_text(encoding="utf-8").startswith("-- new comment")


# --- 14: glob --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glob_nested_pattern_and_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "m.py").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "q.py").write_text("x", encoding="utf-8")
    ex = _executor(tmp_path)
    result = await ex.execute("glob", {"pattern": "src/**/*.py"})
    assert result.ok is True
    assert "src/pkg/m.py" in result.output
    assert "node_modules" not in result.output


@pytest.mark.asyncio
async def test_glob_rejects_parent_and_absolute_patterns(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    for pattern in ("../*", "/etc/*", "C:/Windows/*"):
        result = await ex.execute("glob", {"pattern": pattern})
        assert result.ok is False, pattern


@pytest.mark.asyncio
async def test_glob_truncation_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    ex = _executor(tmp_path)
    monkeypatch.setattr(ex, "MAX_GLOB_RESULTS", 2)
    result = await ex.execute("glob", {"pattern": "*.txt"})
    assert "truncated" in result.output


# --- 15: grep fallback -----------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_fallback_searches_nested_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "deep.txt").write_text("needle here\n", encoding="utf-8")
    ex = _executor(tmp_path)
    monkeypatch.setattr(ex, "_grep_with_ripgrep", lambda *a, **k: None)
    result = await ex.execute("grep", {"pattern": "needle"})
    assert "deep.txt:1" in result.output


@pytest.mark.asyncio
async def test_grep_fallback_refuses_nested_quantifiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ex = _executor(tmp_path)
    monkeypatch.setattr(ex, "_grep_with_ripgrep", lambda *a, **k: None)
    result = await ex.execute("grep", {"pattern": "(a+)+$"})
    assert result.ok is False


# --- 16: bash process handling ---------------------------------------------------


@pytest.mark.asyncio
async def test_bash_timeout_returns_promptly(tmp_path: Path) -> None:
    ex = _executor(tmp_path, cfg={"permission_mode": "auto"})
    command = "ping -n 30 127.0.0.1" if os.name == "nt" else "sleep 30"
    result = await asyncio.wait_for(
        ex._run_subprocess(command, timeout_s=1.0), timeout=60
    )
    assert result.ok is False
    assert "timed out" in result.output or "timeout" in result.summary


@pytest.mark.asyncio
async def test_bash_output_is_capped_head_and_tail(tmp_path: Path) -> None:
    ex = _executor(tmp_path, cfg={"permission_mode": "auto"})
    script = "print('x' * 300000)"
    prefix = "& " if os.name == "nt" else ""
    command = f"{prefix}'{sys.executable}' -c \"{script}\"" if os.name == "nt" else f"{sys.executable} -c \"{script}\""
    result = await ex._run_subprocess(command, timeout_s=60.0)
    assert len(result.output) < 10_000
    assert "truncated" in result.output


# --- 17: notebooks ---------------------------------------------------------------


def _notebook(cells: list[dict[str, Any]]) -> str:
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": cells}
    return json.dumps(nb, indent=1)


@pytest.mark.asyncio
async def test_notebook_markdown_cell_loses_code_fields_and_gets_id(tmp_path: Path) -> None:
    nb_path = tmp_path / "n.ipynb"
    nb_path.write_text(
        _notebook([{"cell_type": "code", "metadata": {}, "source": ["1"], "outputs": [{"x": 1}], "execution_count": 3, "id": "c1"}]),
        encoding="utf-8",
    )
    ex = _executor(tmp_path)
    ex.redo_stack.append(object())  # type: ignore[arg-type]
    result = await ex.execute("notebook", {"filePath": "n.ipynb", "action": "set_cell", "index": 0, "source": "# md"})
    assert result.ok is True, result.output
    result = await ex.execute(
        "notebook", {"filePath": "n.ipynb", "action": "insert_cell", "cellType": "markdown", "source": "hi"}
    )
    assert result.ok is True, result.output
    data = json.loads(nb_path.read_text(encoding="utf-8"))
    inserted = data["cells"][-1]
    assert "outputs" not in inserted and "execution_count" not in inserted
    assert inserted.get("id")
    assert nb_path.read_text(encoding="utf-8").endswith("\n")
    assert ex.redo_stack == []


# --- 18: small robustness fixes --------------------------------------------------


@pytest.mark.asyncio
async def test_read_tolerates_odd_offset_and_limit(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    ex = _executor(tmp_path)
    for args in ({"offset": None, "limit": None}, {"offset": "abc", "limit": "abc"}):
        result = await ex.execute("read", {"filePath": "a.txt", **args})
        assert result.ok is True, args


@pytest.mark.asyncio
async def test_todowrite_invalid_input_keeps_existing_list(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    await ex.execute("todowrite", {"todos": [{"content": "keep me", "status": "pending"}]})
    for bad in ("abc", None):
        result = await ex.execute("todowrite", {"todos": bad})
        assert result.ok is False
    assert [t.content for t in ex.session.todos] == ["keep me"]


def test_mojibake_literals_are_gone() -> None:
    source = (Path(__file__).resolve().parents[1] / "rbit" / "tools.py").read_text(encoding="utf-8")
    for bad in ("Â·", "â€”", "âœ“", "â€¢"):
        assert bad not in source


# --- formatter approval (review item 18) -----------------------------------------


def test_stock_formatter_is_not_custom() -> None:
    config = {"formatter": True}
    defs = build_formatters(config, Path("."))
    assert all(not formatter_is_custom(d) for d in defs if d.name in ("ruff", "prettier", "gofmt"))


def test_config_command_counts_as_custom(tmp_path: Path) -> None:
    config = {"formatter": {"echofmt": {"command": [sys.executable, "-c", "pass", "$FILE"], "extensions": [".txt"]}}}
    defs = [d for d in build_formatters(config, tmp_path) if d.name == "echofmt"]
    assert defs and formatter_is_custom(defs[0])


@pytest.mark.asyncio
async def test_custom_formatter_from_config_needs_approval(tmp_path: Path) -> None:
    config = {"formatter": {"echofmt": {"command": [sys.executable, "-c", "pass", "$FILE"], "extensions": [".txt"]}}}
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    refused = _executor(tmp_path, cfg=config)
    result = await refused.execute("edit", {"filePath": "a.txt", "oldString": "x", "newString": "y"})
    assert result.ok is True
    assert "echofmt" not in result.output

    asked: list[str] = []

    async def approve(tool: str, command: str) -> bool:
        asked.append(command)
        return True

    approved = _executor(tmp_path, cfg=config, cb=approve)
    result = await approved.execute("edit", {"filePath": "a.txt", "oldString": "y", "newString": "z"})
    assert result.ok is True
    assert "echofmt" in result.output
    assert asked and sys.executable in asked[0]


def test_formatter_output_decoding_does_not_crash(tmp_path: Path) -> None:
    from rbit.formatters import format_file

    target = tmp_path / "x.txt"
    target.write_text("hi\n", encoding="utf-8")
    config = {"formatter": {"noisy": {"command": [sys.executable, "-c", "print('привет')", "$FILE"], "extensions": [".txt"]}}}
    result = format_file(target, config, tmp_path, allowed={"noisy"})
    assert result.ran and result.ok


@pytest.mark.asyncio
async def test_formatter_failure_never_fails_the_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    config = {"formatter": True}
    ex = _executor(tmp_path, cfg=config)

    async def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("formatter exploded")

    monkeypatch.setattr(ex, "_format_mutated_files", boom)
    result: ToolResult = await ex.execute("edit", {"filePath": "a.txt", "oldString": "x", "newString": "y"})
    assert result.ok is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "y"
