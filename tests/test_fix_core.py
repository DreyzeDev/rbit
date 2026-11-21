"""Regression tests for the agent core: storage, usage, router, skills, SSE and native protocols."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from rbit.agent import AgentEngine
from rbit.models import (
    ChatMessage,
    FileChangeStat,
    ModelInfo,
    Session,
    ToolCallRecord,
    get_model_info,
    short_id,
)
from rbit.storage import StorageManager
from rbit.tools import ToolExecutor, ToolResult
from rbit.usage import BudgetGuard, UsageLedger

ENV_KEYS = ("RBIT_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "RBIT_API_BASE", "OPENAI_BASE_URL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def _engine(tmp_path: Path, cfg: dict[str, Any] | None = None, handler: Any = None) -> AgentEngine:
    config = cfg if cfg is not None else {}
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: config)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    engine = AgentEngine(workspace=tmp_path, executor=executor, get_config=lambda: config, http_client=client)
    engine.retry_base_delay = 0.0
    engine.retry_attempts = 3
    return engine


def _sse(*events: Any) -> httpx.Response:
    body = "".join(
        f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n" for e in events
    )
    return httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": "text/event-stream"})


async def _noop(*_a: Any) -> None:
    return None


async def _stream(engine: AgentEngine, messages: list[dict[str, Any]] | None = None) -> tuple[bool, str, list[dict[str, Any]]]:
    return await engine._stream_chat_step(
        client=engine.http_client,
        url="https://mock.local/v1/chat/completions",
        headers={},
        model_id="openai/gpt-4.1",
        messages=messages or [{"role": "user", "content": "hi"}],
        on_stream_chunk=_noop,
        on_status=_noop,
    )


# ----------------------------------------------------------------- 1. config
def test_config_with_bom_is_read(tmp_path: Path) -> None:
    s = StorageManager(tmp_path)
    s.ensure_dirs()
    s.config_path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"theme": "rbit-light"}).encode())
    assert s.load_config()["theme"] == "rbit-light"


def test_corrupt_config_is_backed_up_not_lost(tmp_path: Path) -> None:
    s = StorageManager(tmp_path)
    s.ensure_dirs()
    s.config_path.write_text('{"api_key": "mine", broken', encoding="utf-8")
    cfg = s.load_config()
    assert cfg["theme"] == "rbit-dark"
    assert s.config_warning
    assert (tmp_path / ".rbit" / "config.json.bak").read_text(encoding="utf-8").startswith('{"api_key"')


# ------------------------------------------------------------- 2. env keys
def test_env_key_is_never_saved_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    s = StorageManager(tmp_path)
    cfg = s.load_config()
    assert cfg["api_key"] == "sk-or-secret"
    s.save_config(cfg)
    assert "sk-or-secret" not in s.config_path.read_text(encoding="utf-8")


def test_openai_key_is_not_paired_with_openrouter_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    assert StorageManager(tmp_path).load_config()["api_key"] == ""


def test_env_key_does_not_follow_project_api_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    s = StorageManager(tmp_path)
    s.ensure_dirs()
    s.config_path.write_text(json.dumps({"api_base": "https://evil.example/v1"}), encoding="utf-8")
    assert s.load_config()["api_key"] == ""


@pytest.mark.asyncio
async def test_engine_does_not_send_env_key_to_foreign_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    cfg = {"api_key": "sk-openai", "api_base": "https://mock.local/v1", "model": "openai/gpt-4.1"}
    engine = _engine(tmp_path, cfg, handler)
    user = ChatMessage(id=short_id(), role="user", content="hello")
    session = Session(messages=[user])
    await engine.run_turn(session, user, "build", "openai/gpt-4.1", _noop, _noop, _noop)
    assert not any("sk-openai" in h for h in seen)


# ------------------------------------------------------------------ 4. writes
def test_atomic_write_uses_unique_tmp_and_leaves_no_files(tmp_path: Path) -> None:
    s = StorageManager(tmp_path)
    s.ensure_dirs()
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(10):
                s.save_config({"theme": f"t{n}-{i}"})
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert json.loads(s.config_path.read_text(encoding="utf-8"))["theme"].startswith("t")
    assert [p.name for p in s.state_dir.glob(".*.tmp")] == []


def test_append_memory_rule_keeps_every_entry(tmp_path: Path) -> None:
    s = StorageManager(tmp_path)
    threads = [threading.Thread(target=s.append_memory_rule, args=(f"rule-{i}",)) for i in range(25)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    text = s.memory_path.read_text(encoding="utf-8")
    assert all(f"rule-{i}" in text for i in range(25))


# ------------------------------------------------------------------- 5. locks
def test_lock_is_exclusive_and_release_only_removes_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    s = StorageManager(tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    ok, other = s.acquire_lock()
    assert ok and other is None
    lock = next(tmp_path.glob("rbit-lock-*.lock"))
    # A live foreign owner (our parent process) blocks us.
    lock.write_text(str(os.getppid()), encoding="utf-8")
    ok, other = s.acquire_lock()
    assert ok is False and other == os.getppid()
    s.release_lock()
    assert lock.exists(), "release must not delete someone else's lock"
    # A dead owner is taken over.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock.write_text(str(dead.pid), encoding="utf-8")
    ok, _ = s.acquire_lock()
    assert ok
    assert lock.read_text(encoding="utf-8") == str(os.getpid())
    s.release_lock()
    assert not lock.exists()


# --------------------------------------------------------------- 6. sessions
def test_delete_session_rejects_traversal(tmp_path: Path) -> None:
    s = StorageManager(tmp_path)
    s.ensure_dirs()
    s.save_config({"theme": "keep"})
    assert s.delete_session("../config") is False
    assert s.delete_session("..\\config") is False
    assert s.config_path.exists()


def test_delete_session_removes_sqlite_row(tmp_path: Path) -> None:
    s = StorageManager(tmp_path)
    s.ensure_dirs()
    s.save_config({"storage_backend": "sqlite"})
    session = Session(id="abc12345")
    s.save_session(session)
    assert s.find_session("abc12345") is not None
    assert s.delete_session("abc12345") is True
    assert s.find_session("abc12345") is None


def test_find_session_empty_and_ambiguous(tmp_path: Path) -> None:
    s = StorageManager(tmp_path)
    s.save_session(Session(id="abcd0001"))
    s.save_session(Session(id="abcd0002"))
    assert s.find_session("") is None
    assert s.find_session("abcd") is None  # ambiguous: no silent "first match"
    assert s.find_session("abcd0002").id == "abcd0002"


# ----------------------------------------------------------------- 7. models
def test_models_from_dict_are_tolerant() -> None:
    msg = ChatMessage.from_dict(
        {"id": "m", "role": "user", "content": "x", "pinned": True, "tool_calls": None}
    )
    assert msg.pinned is True and msg.tool_calls == []
    rec = ToolCallRecord.from_dict({"id": "t", "name": "read", "arguments": {}, "future_field": 1})
    assert rec.name == "read"
    stat = FileChangeStat.from_dict({"path": "a", "added": 1, "removed": None, "extra": 2})
    assert (stat.added, stat.removed) == (1, 0)


def test_list_sessions_reports_unreadable_files(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    s = StorageManager(tmp_path)
    s.save_session(Session(id="good0001"))
    (s.sessions_dir / "bad00001.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        sessions = s.list_sessions()
    assert [x.id for x in sessions] == ["good0001"]
    assert s.corrupt_sessions == ["bad00001.json"]
    assert "bad00001" in caplog.text


# ------------------------------------------------------------ 8. total tokens
def test_total_tokens_does_not_double_count_tool_output() -> None:
    call = ToolCallRecord(id="t", name="bash", arguments={}, output="word " * 400)
    msg = ChatMessage(id="a", role="assistant", content="ok", tokens=100, tool_calls=[call])
    assert Session(messages=[msg]).total_tokens == 100


# --------------------------------------------------------------- 9. checkpoints
def test_auto_checkpoints_do_not_evict_manual_ones(tmp_path: Path) -> None:
    s = StorageManager(tmp_path)
    manual = [s.save_checkpoint(f"manual {i}", {"a.txt": "x"}).id for i in range(3)]
    for i in range(25):
        s.save_checkpoint(f"auto: before edit {i}", {"a.txt": "x"})
    ids = {c.id for c in s.list_checkpoints()}
    assert set(manual) <= ids
    assert len([i for i in ids if i.startswith("auto-")]) == StorageManager.MAX_AUTO_CHECKPOINTS


# --------------------------------------------------------------------- 10. usage
def test_daily_usage_uses_local_date() -> None:
    assert UsageLedger.today() == datetime.now().date()


def test_model_budget_compares_with_that_models_spend(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path)
    ledger.record("cheap/model", output_tokens=10, cost_usd=0.10)
    ledger.record("big/model", output_tokens=10, cost_usd=5.0)
    cfg = {"budget_mode": "strict", "model_budgets": {"cheap/model": 1.0}}
    allowed, _ = BudgetGuard(ledger, cfg).check("cheap/model", estimated_usd=0.01)
    assert allowed is True  # 0.10 spent on this model, not 5.10
    allowed, _ = BudgetGuard(ledger, {**cfg, "model_budgets": {"cheap/model": 0.05}}).check(
        "cheap/model", estimated_usd=0.01
    )
    assert allowed is False


def test_budget_check_never_raises(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path)
    guard = BudgetGuard(
        ledger,
        {"budget_mode": "strict", "daily_budget_usd": None, "daily_token_limit": "abc", "model_budgets": {"m": "x"}},
    )
    assert guard.check("m")[0] is True
    assert BudgetGuard(ledger, {"budget_mode": "strict", "daily_budget_usd": "oops"}).check("m", estimated_usd=None)[0]


def test_migrate_keeps_legacy_budget_when_daily_exists(tmp_path: Path) -> None:
    from rbit.migrate import migrate_config

    cfg = tmp_path / ".rbit" / "config.json"
    cfg.parent.mkdir()
    cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"budget_usd": 7, "daily_budget_usd": 3}).encode())
    result = migrate_config(tmp_path)
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert result.backup is not None
    assert saved["budget_usd"] == 7 and saved["daily_budget_usd"] == 3


@pytest.mark.asyncio
async def test_agent_budget_mode_none_alias_disables_guard(tmp_path: Path) -> None:
    cfg = {"budget_usd": 0.0001, "budget_mode": "none", "api_key": ""}
    engine = _engine(tmp_path, cfg)
    big = ChatMessage(id="a", role="assistant", content="x", tokens=10_000_000, model="openai/gpt-4.1")
    user = ChatMessage(id="u", role="user", content="hello")
    reply = await engine.run_turn(
        Session(messages=[big, user]), user, "build", "rbit/smart-local", _noop, _noop, _noop
    )
    assert "Budget guard" not in reply.content


# ------------------------------------------------------- 11. skills / agents / router
def test_project_skill_and_agent_override_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rbit import skills, subagents

    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "agents").mkdir()
    (home / "skills" / "deploy.md").write_text("---\ndescription: global\n---\nG", encoding="utf-8")
    (home / "agents" / "rev.md").write_text("---\ndescription: global\n---\nG", encoding="utf-8")
    monkeypatch.setattr(skills, "global_skill_dirs", lambda: [home / "skills"])
    monkeypatch.setattr(subagents, "global_agent_dirs", lambda: [home / "agents"])
    ws = tmp_path / "ws"
    (ws / ".rbit" / "skills" / "deploy").mkdir(parents=True)
    (ws / ".rbit" / "skills" / "deploy" / "skill.md").write_text(  # lower-case file name
        "---\nname: deploy\ndescription: project\n---\nP", encoding="utf-8"
    )
    (ws / ".rbit" / "agents").mkdir()
    (ws / ".rbit" / "agents" / "rev.md").write_text("---\ndescription: project\n---\nP", encoding="utf-8")
    assert skills.load_skills(ws)["deploy"]["description"] == "project"
    assert subagents.load_subagents(ws)["rev"].description == "project"


def test_never_switch_mid_task_keeps_model() -> None:
    from rbit.router import apply_live_route

    cfg = {"model_router": {"enabled": True, "tiers": {"deep": "big/model", "fast": "small/model"}}}
    kept = apply_live_route("refactor the architecture", "small/model", cfg)
    assert kept.model == "small/model"
    cfg["model_router"]["never_switch_mid_task"] = False
    assert apply_live_route("refactor the architecture", "small/model", cfg).model == "big/model"


# ------------------------------------------------------------ 12. cost / usage
def test_unknown_model_is_not_priced() -> None:
    assert get_model_info("acme/mystery-1").priced is False
    assert get_model_info("openai/gpt-4.1").priced is True
    assert get_model_info("acme/x", [ModelInfo(id="acme/x", name="x", provider="p", cost_per_1k=0.1)]).priced


@pytest.mark.asyncio
async def test_stream_requests_usage_and_records_input_tokens(tmp_path: Path) -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return _sse(
            {"choices": [{"delta": {"content": "hello"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 1234, "completion_tokens": 7}},
            "[DONE]",
        )

    ledger = UsageLedger(tmp_path)
    config = {"api_key": "k", "api_base": "https://mock.local/v1"}
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: config)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = AgentEngine(tmp_path, executor, lambda: config, http_client=client, usage=ledger)
    user = ChatMessage(id="u", role="user", content="hi")
    await engine.run_turn(
        Session(messages=[user]), user, "build", "openai/gpt-4.1", _noop, _noop, _noop,
        on_stream_chunk=_noop,
    )
    assert payloads[0]["stream_options"] == {"include_usage": True}
    record = ledger.read_day()[0]
    assert record.input_tokens == 1234 and record.output_tokens == 7


@pytest.mark.asyncio
async def test_input_tokens_estimated_without_provider_usage(tmp_path: Path) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "fine"}}]})

    ledger = UsageLedger(tmp_path)
    config = {"api_key": "k", "api_base": "https://mock.local/v1"}
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: config)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = AgentEngine(tmp_path, executor, lambda: config, http_client=client, usage=ledger)
    user = ChatMessage(id="u", role="user", content="hi there")
    await engine.run_turn(Session(messages=[user]), user, "build", "acme/mystery-1", _noop, _noop, _noop)
    record = ledger.read_day()[0]
    assert record.input_tokens > 50  # the system prompt alone is large
    assert record.priced is False and record.cost_usd == 0.0


# --------------------------------------------------------------------- 13. SSE
@pytest.mark.asyncio
async def test_sse_error_event_is_retried(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _sse({"error": {"message": "upstream overloaded"}})
        return _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}, "[DONE]")

    ok, text, _ = await _stream(_engine(tmp_path, {}, handler))
    assert ok and text == "ok" and calls["n"] == 2


@pytest.mark.asyncio
async def test_sse_error_event_is_reported_when_persistent(tmp_path: Path) -> None:
    ok, text, _ = await _stream(_engine(tmp_path, {}, lambda _r: _sse({"error": {"message": "quota exceeded"}})))
    assert ok is False and "quota exceeded" in text


@pytest.mark.asyncio
async def test_same_index_different_id_makes_two_calls(tmp_path: Path) -> None:
    def tc(idx: int, cid: str | None, name: str | None, args: str) -> dict[str, Any]:
        fn: dict[str, Any] = {"arguments": args}
        if name:
            fn["name"] = name
        item: dict[str, Any] = {"index": idx, "function": fn}
        if cid:
            item["id"] = cid
        return {"choices": [{"delta": {"tool_calls": [item]}}]}

    handler = lambda _r: _sse(  # noqa: E731
        tc(0, "A", "read", '{"filePath":"a"}'),
        tc(0, "B", "read", '{"filePath":"b"}'),
        tc(1, None, "read", ""),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )
    ok, _, calls = await _stream(_engine(tmp_path, {}, handler))
    assert ok
    assert [c["id"] for c in calls[:2]] == ["A", "B"]
    assert all(c["function"]["name"] in ("read", "readread") for c in calls)
    assert calls[0]["function"]["arguments"] == '{"filePath":"a"}'
    assert calls[1]["function"]["arguments"] == '{"filePath":"b"}'


@pytest.mark.asyncio
async def test_resent_tool_name_is_not_concatenated(tmp_path: Path) -> None:
    ev = lambda args: {"choices": [{"delta": {"tool_calls": [  # noqa: E731
        {"index": 0, "id": "A", "function": {"name": "read", "arguments": args}}]}}]}
    handler = lambda _r: _sse(ev('{"filePath"'), ev(':"a"}'), "[DONE]")  # noqa: E731
    _, _, calls = await _stream(_engine(tmp_path, {}, handler))
    assert calls[0]["function"]["name"] == "read"
    assert json.loads(calls[0]["function"]["arguments"]) == {"filePath": "a"}


class _CutStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield b'data: {"choices":[{"delta":{"content":"partial text"}}]}\n\n'
        raise httpx.ReadError("connection reset")


@pytest.mark.asyncio
async def test_broken_stream_returns_partial_text_marked(tmp_path: Path) -> None:
    handler = lambda _r: httpx.Response(200, stream=_CutStream())  # noqa: E731
    ok, text, calls = await _stream(_engine(tmp_path, {}, handler))
    assert ok and text.startswith("partial text") and "оборван" in text and calls == []


@pytest.mark.asyncio
async def test_invalid_json_arguments_are_not_executed(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {})
    ran: list[str] = []

    async def invoke(name: str, args: dict[str, Any]) -> ToolResult:
        ran.append(name)
        return ToolResult(True, "ok", "ran")

    messages: list[dict[str, Any]] = []
    broken = {"type": "function", "function": {"name": "write", "arguments": '{"filePath": "a", "content": "unfini'}}
    await engine._dispatch_tool_calls([broken], messages, invoke, _noop)
    assert ran == []
    assert "invalid JSON arguments" in messages[0]["content"]
    assert broken["id"] and messages[0]["tool_call_id"] == broken["id"]


@pytest.mark.asyncio
async def test_length_truncated_flag_never_reaches_provider(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return _sse(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"name": "read", "arguments": '{"filePath": "a'}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "length"}]},
                "[DONE]",
            )
        return _sse({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}, "[DONE]")

    engine = _engine(tmp_path, {"api_base": "https://mock.local/v1"}, handler)

    async def invoke(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "ok", "ran")

    text = await engine._run_openai_compatible(
        Session(), ChatMessage(id="u", role="user", content="hi"), "build", "openai/gpt-4.1",
        "https://mock.local/v1", "k", invoke, _noop, on_stream_chunk=_noop,
    )
    assert text.endswith("done")
    assistant = [m for m in requests[1]["messages"] if m["role"] == "assistant"][-1]
    assert all("length_truncated" not in tc for tc in assistant["tool_calls"])
    tool_msg = [m for m in requests[1]["messages"] if m["role"] == "tool"][-1]
    assert tool_msg["tool_call_id"] == assistant["tool_calls"][0]["id"]


# ----------------------------------------------------------------- 14. retries
def test_retry_after_is_capped_dated_and_nan_safe(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert engine._retry_delay(1, httpx.Response(429, headers={"retry-after": "9999"})) <= 60.0
    assert engine._retry_delay(1, httpx.Response(429, headers={"retry-after": "nan"})) == 0.0
    future = format_datetime(datetime.now().astimezone().astimezone(__import__("datetime").timezone.utc)
                             + __import__("datetime").timedelta(seconds=20), usegmt=True)
    delay = engine._retry_delay(1, httpx.Response(429, headers={"retry-after": future}))
    assert 15.0 < delay <= 22.0


@pytest.mark.asyncio
async def test_native_anthropic_retries_429(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    cfg = {"api_key": "k", "protocol": "anthropic", "api_base": "https://api.anthropic.com/v1"}
    engine = _engine(tmp_path, cfg, handler)
    user = ChatMessage(id="u", role="user", content="hi")
    reply = await engine.run_turn(
        Session(messages=[user]), user, "build", "anthropic/claude-sonnet-4-5", _noop, _noop, _noop
    )
    assert reply.content == "ok" and calls["n"] == 2


# ------------------------------------------------------------ 15. native paths
@pytest.mark.asyncio
async def test_anthropic_history_and_steps_keep_tool_results(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        n = len(requests)
        if n <= 12:  # more than the old hard 10-iteration cap
            return httpx.Response(200, json={"content": [
                {"type": "text", "text": f"step{n}"},
                {"type": "tool_use", "id": f"t{n}", "name": "todowrite", "input": {"todos": []}},
            ]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "final"}]})

    cfg = {"api_key": "k", "protocol": "anthropic", "api_base": "https://api.anthropic.com/v1", "max_steps": 30}
    engine = _engine(tmp_path, cfg, handler)
    user = ChatMessage(id="u", role="user", content="go")
    history = ChatMessage(
        id="h", role="assistant", content="before",
        tool_calls=[ToolCallRecord(id="old1", name="bash", arguments={}, output="")],
    )
    reply = await engine.run_turn(
        Session(messages=[history, user]), user, "build", "anthropic/claude-sonnet-4-5", _noop, _noop, _noop
    )
    assert "step1" in reply.content and "step12" in reply.content and reply.content.endswith("final")
    first = requests[0]["messages"]
    result_block = [m for m in first if m["role"] == "user" and isinstance(m["content"], list)][0]["content"][0]
    assert result_block["tool_use_id"] == "old1" and result_block["content"] == "(no output)"


@pytest.mark.asyncio
async def test_anthropic_profile_readonly_rejects_writes(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={"content": [
                {"type": "tool_use", "id": "w1", "name": "write",
                 "input": {"filePath": "x.txt", "content": "hi"}}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "done"}]})

    cfg = {"api_key": "k", "protocol": "anthropic", "api_base": "https://api.anthropic.com/v1",
           "prompt_profile": "review"}
    from rbit.agent import PROMPT_PROFILES

    if not PROMPT_PROFILES.get("review", {}).get("read_only"):
        cfg["agent_tools"] = ["read"]
    engine = _engine(tmp_path, cfg, handler)
    user = ChatMessage(id="u", role="user", content="write x")
    await engine.run_turn(
        Session(messages=[user]), user, "build", "anthropic/claude-sonnet-4-5", _noop, _noop, _noop
    )
    assert not (tmp_path / "x.txt").exists()
    assert requests[1]["messages"][-1]["content"][0]["is_error"] is True


@pytest.mark.asyncio
async def test_gemini_history_merges_function_responses(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    cfg = {"api_key": "g", "protocol": "gemini", "api_base": "https://generativelanguage.googleapis.com/v1beta"}
    engine = _engine(tmp_path, cfg, handler)
    user = ChatMessage(id="u", role="user", content="go")
    history = ChatMessage(
        id="h", role="assistant", content="",
        tool_calls=[
            ToolCallRecord(id="1", name="read", arguments={}, output="a"),
            ToolCallRecord(id="2", name="bash", arguments={}, output=""),
        ],
    )
    await engine.run_turn(Session(messages=[history, user]), user, "build", "google/gemini-2.5", _noop, _noop, _noop)
    contents = requests[0]["contents"]
    responses = [c for c in contents if any("functionResponse" in p for p in c["parts"])]
    assert len(responses) == 1 and len(responses[0]["parts"]) == 2
    assert responses[0]["parts"][1]["functionResponse"]["response"]["content"] == "(no output)"


# ---------------------------------------------------------------- 16. invoke_tool
@pytest.mark.asyncio
async def test_invoke_tool_marks_error_and_calls_end_hook(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {})

    async def boom(*_a: Any, **_k: Any) -> ToolResult:
        raise RuntimeError("disk on fire")

    engine.executor.execute = boom  # type: ignore[assignment]
    ended: list[ToolCallRecord] = []

    async def on_end(rec: ToolCallRecord) -> None:
        ended.append(rec)

    user = ChatMessage(id="u", role="user", content="run `echo hi`")
    with pytest.raises(RuntimeError):
        await engine.run_turn(Session(messages=[user]), user, "build", "rbit/smart-local", _noop, on_end, _noop)
    assert ended and ended[0].status == "error" and "disk on fire" in ended[0].output


# ------------------------------------------------------------------ 17. fallback
@pytest.mark.asyncio
async def test_fallback_on_401_shows_the_error(tmp_path: Path) -> None:
    handler = lambda _r: httpx.Response(401, json={"error": "unauthorized"})  # noqa: E731
    engine = _engine(tmp_path, {"api_key": "bad", "api_base": "https://mock.local/v1"}, handler)
    user = ChatMessage(id="u", role="user", content="что такое python?")
    reply = await engine.run_turn(Session(messages=[user]), user, "build", "openai/gpt-4.1", _noop, _noop, _noop)
    assert "401" in reply.content and "Ошибка провайдера" in reply.content


@pytest.mark.asyncio
async def test_smart_agent_run_pattern_needs_word_boundary(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {})
    ran: list[str] = []

    async def invoke(name: str, args: dict[str, Any]) -> ToolResult:
        ran.append(name)
        return ToolResult(True, "ok", "out")

    user = ChatMessage(id="u", role="user", content="please prune old branches")
    await engine._run_smart_local_agent(Session(messages=[user]), user, "build", invoke, _noop)
    assert "bash" not in ran


# ------------------------------------------------------------------- 18. subagent
@pytest.mark.asyncio
async def test_subagent_final_summary_without_tools_and_ledger(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if "tools" in body:
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
                {"id": f"c{len(requests)}", "type": "function",
                 "function": {"name": "glob", "arguments": '{"pattern": "*"}'}}]}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "SUMMARY"}}],
                                         "usage": {"prompt_tokens": 120, "completion_tokens": 9}})

    cfg = {"subagent_max_steps": 2}
    ledger = UsageLedger(tmp_path)
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: cfg)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = AgentEngine(tmp_path, executor, lambda: cfg, http_client=client, usage=ledger)
    text = await engine._explore_with_llm("find things", "openai/gpt-4.1", "https://mock.local/v1", "k")
    assert text.startswith("SUMMARY")
    assert "tools" not in requests[-1]
    kinds = [r.kind for r in ledger.read_day()]
    assert kinds == ["subagent"] * 3


# -------------------------------------------------------------------- 19. context
def test_transcript_keeps_the_tail() -> None:
    msgs = [ChatMessage(id=str(i), role="user", content=f"decision-{i} " + "x" * 1000) for i in range(40)]
    text = AgentEngine._transcript_for_summary(msgs)
    assert "decision-39" in text and "decision-0 " not in text


def test_image_base64_is_not_counted_as_tokens() -> None:
    huge = "A" * 2_000_000
    msg = {"role": "user", "content": [
        {"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + huge}}]}
    assert AgentEngine._message_tokens(msg) < 2000


def test_ollama_images_and_num_ctx(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {})
    out = engine._ollama_messages([{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}])
    assert out[0]["images"] == ["QUJD"] and out[0]["content"] == "what is this"
    assert engine._ollama_num_ctx("qwen2.5-coder:7b") == 32_768
    assert _engine(tmp_path, {"num_ctx": 16384})._ollama_num_ctx("x") == 16384


@pytest.mark.asyncio
async def test_history_is_trimmed_by_tokens_not_unit_count(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    cfg = {"api_key": "k", "api_base": "https://mock.local/v1"}
    engine = _engine(tmp_path, cfg, handler)
    msgs = []
    for i in range(60):  # 120 short units: all fit in a 128k window
        msgs.append(ChatMessage(id=f"u{i}", role="user", content=f"q{i}"))
        msgs.append(ChatMessage(id=f"a{i}", role="assistant", content=f"r{i}"))
    user = ChatMessage(id="last", role="user", content="final")
    msgs.append(user)
    await engine.run_turn(Session(messages=msgs), user, "build", "openai/gpt-4.1", _noop, _noop, _noop)
    sent = [m["content"] for m in requests[0]["messages"] if m["role"] != "system"]
    assert "q0" in sent and sent[-1] == "final"


# ---------------------------------------------------------------------- 20. redaction
def test_saved_session_redacts_secrets_from_tool_output(tmp_path: Path) -> None:
    s = StorageManager(tmp_path)
    call = ToolCallRecord(
        id="t", name="bash", arguments={"command": "printenv"},
        output="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\nHOME=/x",
    )
    s.save_session(Session(id="sec00001", messages=[
        ChatMessage(id="a", role="assistant", content="ok", tool_calls=[call])]))
    raw = (s.sessions_dir / "sec00001.json").read_text(encoding="utf-8")
    assert "abcdefghijklmnopqrstuvwxyz123456" not in raw and "HOME=/x" in raw


# ------------------------------------------------- 21. follow-up hardening
def test_skill_and_agent_names_match_case_insensitively(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rbit import skills, subagents

    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "agents").mkdir()
    (home / "skills" / "Deploy.md").write_text("---\ndescription: global\n---\nG", encoding="utf-8")
    (home / "agents" / "Reviewer.md").write_text("---\ndescription: global\n---\nG", encoding="utf-8")
    monkeypatch.setattr(skills, "global_skill_dirs", lambda: [home / "skills"])
    monkeypatch.setattr(subagents, "global_agent_dirs", lambda: [home / "agents"])
    ws = tmp_path / "ws"
    (ws / ".rbit" / "skills").mkdir(parents=True)
    (ws / ".rbit" / "agents").mkdir()
    (ws / ".rbit" / "skills" / "deploy.md").write_text("---\ndescription: project\n---\nP", encoding="utf-8")
    (ws / ".rbit" / "agents" / "reviewer.md").write_text("---\ndescription: project\n---\nP", encoding="utf-8")

    loaded_skills = skills.load_skills(ws)
    same = [meta for key, meta in loaded_skills.items() if key.lower() == "deploy"]
    assert len(same) == 1 and same[0]["description"] == "project"

    loaded_agents = subagents.load_subagents(ws)
    same_agents = [d for key, d in loaded_agents.items() if key.lower() == "reviewer"]
    assert len(same_agents) == 1 and same_agents[0].description == "project"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_config_is_written_owner_only(tmp_path: Path) -> None:
    import stat

    s = StorageManager(tmp_path)
    s.ensure_dirs()
    s.save_config({"theme": "rbit-dark"})
    assert stat.S_IMODE(s.config_path.stat().st_mode) == 0o600


def test_live_route_switches_when_there_is_no_history() -> None:
    from rbit.router import apply_live_route

    cfg = {"model_router": {"enabled": True, "tiers": {"deep": "big/model", "fast": "small/model"}}}
    fresh = apply_live_route("refactor the architecture", "small/model", cfg, has_history=False)
    assert fresh.model == "big/model"


@pytest.mark.asyncio
async def test_gemini_stops_at_configured_max_steps(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "todowrite", "args": {"todos": []}}}]}}]})

    cfg = {"api_key": "g", "protocol": "gemini",
           "api_base": "https://generativelanguage.googleapis.com/v1beta", "max_steps": 2}
    engine = _engine(tmp_path, cfg, handler)
    user = ChatMessage(id="u", role="user", content="go")
    reply = await engine.run_turn(Session(messages=[user]), user, "build", "google/gemini-2.5", _noop, _noop, _noop)
    assert calls["n"] == 2
    assert "step limit (2)" in reply.content


@pytest.mark.asyncio
async def test_anthropic_tool_exception_does_not_abort_turn(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={"content": [
                {"type": "tool_use", "id": "x1", "name": "read", "input": {"filePath": "a.txt"}}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "recovered"}]})

    cfg = {"api_key": "k", "protocol": "anthropic", "api_base": "https://api.anthropic.com/v1"}
    engine = _engine(tmp_path, cfg, handler)

    async def boom(*_a: Any, **_k: Any) -> ToolResult:
        raise RuntimeError("disk on fire")

    engine.executor.execute = boom  # type: ignore[assignment]
    user = ChatMessage(id="u", role="user", content="read a.txt")
    reply = await engine.run_turn(
        Session(messages=[user]), user, "build", "anthropic/claude-sonnet-4-5", _noop, _noop, _noop
    )
    assert reply.content.endswith("recovered")
    result = requests[1]["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "x1" and result["is_error"] is True


@pytest.mark.asyncio
async def test_ollama_transient_503_is_retried(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "local ok"}, "done": True})

    engine = _engine(tmp_path, {}, handler)
    ok, text, _ = await engine._ollama_chat_step(
        engine.http_client, "http://localhost:11434/v1", "qwen2.5-coder:7b",
        [{"role": "user", "content": "hi"}], on_status=_noop,
    )
    assert ok and text == "local ok" and calls["n"] == 2
