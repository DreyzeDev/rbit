"""Unit tests for the Autopilot soak harness and trace logger."""

from __future__ import annotations

import json
from pathlib import Path

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
