"""Unit tests for the Autopilot soak harness and trace logger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rbit.soak import SoakTraceWriter, default_soak_tasks, load_soak_tasks, redact_secrets, render_soak_report


def test_redact_secrets_scrubs_tokens() -> None:
    raw = "Authorization: Bearer sk-secret1234567890abcdef and ghp_1234567890abcdefghij12345"
    cleaned = redact_secrets(raw)
    assert "sk-secret1234567890abcdef" not in cleaned
    assert "ghp_1234567890abcdefghij12345" not in cleaned
    assert "[REDACTED]" in cleaned


def test_trace_writer_flushes_jsonl_and_counts(tmp_path: Path) -> None:
    trace_file = tmp_path / "logs" / "trace.jsonl"
    writer = SoakTraceWriter(trace_file)
    writer.emit("llm_request", step=0, model="deepseek-v4.1-flash", token="Bearer abcdefghijklmnop")
    writer.emit("tool_start", tool="bash", command="pytest -q")

    lines = trace_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "llm_request"
    assert "[REDACTED]" in first["token"]
    assert writer.counts == {"llm_request": 1, "tool_start": 1}


def test_load_soak_tasks_and_render_report(tmp_path: Path) -> None:
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps([{"id": "t1", "title": "Fix bug", "prompt": "Fix it", "verify_cmd": "pytest -q"}]),
        encoding="utf-8",
    )
    tasks = load_soak_tasks(tasks_file)
    assert len(tasks) == 1
    assert tasks[0].id == "t1"
    assert len(default_soak_tasks()) >= 3

    md = render_soak_report(
        {
            "started_at": "2026-11-26T10:00:00Z",
            "finished_at": "2026-11-26T10:35:00Z",
            "duration_minutes": 35.0,
            "duration_seconds": 2100.0,
            "workspace": str(tmp_path),
            "model": "deepseek-v4.1-flash",
            "api_base": "https://opencode.ai/inference/go/openai/v1",
            "tasks_passed": 3,
            "tasks_total": 3,
            "success_rate_pct": 100.0,
            "trace_path": str(tmp_path / "trace.jsonl"),
            "event_counts": {"llm_request": 12, "tool_start": 18},
            "results": [
                {
                    "id": "t1",
                    "title": "Fix bug",
                    "duration_s": 120.5,
                    "llm_requests": 4,
                    "tool_calls": 6,
                    "verify_passed": True,
                    "verify_exit_code": 0,
                    "errors": [],
                }
            ],
        }
    )
    assert "35.0 min" in md
    assert "100.0%" in md
    assert "PASS" in md


def test_truthfulness_detector_avoids_false_positives_and_false_negatives() -> None:
    from rbit.soak import _claims_success

    # False-positive traps (must NOT be classified as claiming success)
    assert _claims_success("I ran pytest: 2 failed, 1 passed.") is False
    assert _claims_success("0/3 passed — could not complete the migration.") is False
    assert _claims_success("Success criteria were not met; symbol is missing.") is False
    assert _claims_success("**[Max steps]** Достигнут лимит шагов (50). Completed 1/3.") is False
    assert _claims_success("Задача не выполнена, произошла ошибка при сборке.") is False

    # True-positive / false-negative traps (MUST be detected as claiming success)
    assert _claims_success("Готово — все тесты проходят.") is True
    assert _claims_success("Задача выполнена, pytest зелёный.") is True
    assert _claims_success("Done — implemented feature and fixed and verified all checks.") is True
    assert _claims_success("All tests pass and refactoring is completed.") is True


@pytest.mark.asyncio
async def test_soak_checkpoint_and_resume_skips_completed_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rbit.models import ChatMessage
    from rbit.soak import AutopilotSoakRunner, SoakTask

    ws = tmp_path / "repo"
    log_dir = tmp_path / "logs"
    ws.mkdir()
    (ws / "a.txt").write_text("v1\n", encoding="utf-8")

    tasks = [
        SoakTask(id="t1", title="First", prompt="Do first", verify_cmd=""),
        SoakTask(id="t2", title="Second", prompt="Do second", verify_cmd=""),
    ]
    calls: list[str] = []

    async def fake_run_turn(self, session, user_msg, **_kw):  # type: ignore[no-untyped-def]
        calls.append(user_msg.content)
        if user_msg.content == "Do first":
            (ws / "a.txt").write_text("v2-from-t1\n", encoding="utf-8")
            self._turn_in = 100
            self._turn_out = 50
            self.on_trace_event(
                {
                    "event": "llm_response",
                    "ok": True,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "opencode": {"list_cost": 0.0015}},
                }
            )
            return ChatMessage(id="r1", role="assistant", content="Done — first completed.")
        raise RuntimeError("simulated mid-task crash")

    from rbit.agent import AgentEngine

    monkeypatch.setattr(AgentEngine, "run_turn", fake_run_turn)
    runner1 = AutopilotSoakRunner(workspace=ws, log_dir=log_dir, max_tasks=1)
    s1 = await runner1.run(list(tasks))
    assert s1["tasks_total"] == 1
    assert s1["results"][0]["cost_usd"] == 0.0015
    assert s1["results"][0]["cost_source"] == "provider"
    assert s1["results"][0]["usage_source"] == "provider"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "v2-from-t1\n"

    # Now resume with a working t2 and verify t1 is NOT repeated and a.txt is preserved
    async def fake_run_turn_ok(self, session, user_msg, **_kw):  # type: ignore[no-untyped-def]
        calls.append(user_msg.content)
        return ChatMessage(id="r2", role="assistant", content="Done — second completed.")

    monkeypatch.setattr(AgentEngine, "run_turn", fake_run_turn_ok)
    runner2 = AutopilotSoakRunner(workspace=ws, log_dir=log_dir, resume=True)
    s2 = await runner2.run(list(tasks))
    assert s2["tasks_total"] == 2
    assert calls == ["Do first", "Do second"]
    assert (ws / "a.txt").read_text(encoding="utf-8") == "v2-from-t1\n"
