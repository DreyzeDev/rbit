"""Long-running Autopilot soak & trace-logging harness for RBit.

Runs multi-task autonomous Autopilot campaigns against a target repository using
a real configured LLM provider, recording every LLM request, model response,
tool call, file modification, test run, and error to a flushed JSONL trace log
plus a reproducible Markdown/JSON report.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rbit.agent import AgentEngine
from rbit.models import ChatMessage, Session, ToolCallRecord, get_model_info, short_id
from rbit.storage import StorageManager
from rbit.tools import MUTATING_TOOLS, ToolExecutor
from rbit.usage import extract_provider_cost

_SECRET_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._\-]{12,}|sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{20,})"
)


def redact_secrets(text: str) -> str:
    """Scrub common bearer/API key patterns before writing trace logs."""
    return _SECRET_RE.sub("[REDACTED]", text)


class SoakTraceWriter:
    """Append-only, line-flushed JSONL trace logger for Autopilot runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.counts: dict[str, int] = {}

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "epoch": round(time.time(), 3),
            "event": event,
        }
        for k, v in fields.items():
            if isinstance(v, str):
                payload[k] = redact_secrets(v)
            else:
                payload[k] = v
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        self.counts[event] = self.counts.get(event, 0) + 1
        return payload


@dataclass
class SoakTask:
    """One autonomous engineering task in a soak campaign."""

    id: str
    title: str
    prompt: str
    verify_cmd: str = "python -m pytest -q"


@dataclass
class SoakTaskResult:
    """Observed outcome of one soak task."""

    id: str
    title: str
    session_id: str
    duration_s: float
    verify_passed: bool
    verify_exit_code: int | None
    verify_output: str
    llm_requests: int
    tool_calls: int
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_source: str = "estimated"
    usage_source: str = "estimated"
    retries: int = 0
    agent_claimed_success: bool = False
    false_success_claim: bool = False
    mutated_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reply_preview: str = ""


_FALSE_PASS_CONTEXT_RE = re.compile(
    r"\b(?:not\s+(?:completed|passed|met|fixed|done|verified)|"
    r"could\s+not\s+(?:complete|pass|fix|verify)|"
    r"did\s+not\s+(?:pass|complete|succeed)|"
    r"\d+\s+failed|0/\d+\s+passed)\b",
    re.IGNORECASE,
)


def _claims_success(reply_text: str) -> bool:
    """Detect whether an agent reply claims the task succeeded (minimizing FP/FN)."""
    low = (reply_text or "").lower()
    if not low.strip():
        return False
    neg_markers = (
        "не выполн",
        "не удал",
        "не заверш",
        "не прош",
        "отсутствует",
        "не существует",
        "достигнут лимит",
        "ошибка",
        "partially",
        "cannot",
        "failed",
        "unable to",
        "incomplete",
        "**[loop guard]**",
        "**[budget guard]**",
        "**[fallback]**",
        "**[provider error]**",
        "**[max steps]**",
    )
    if any(m in low for m in neg_markers) or _FALSE_PASS_CONTEXT_RE.search(low):
        return False
    pos_markers = (
        "готово",
        "успешно",
        "выполнено",
        "задача выполнена",
        "всё работает",
        "все тесты проходят",
        "pytest зелёный",
        "all tests pass",
        "tests pass",
        "fixed and verified",
        "done —",
        "done:",
        "completed",
        "success",
    )
    return any(m in low for m in pos_markers)


def load_soak_tasks(path: Path) -> list[SoakTask]:
    """Load tasks from a JSON file (`list[dict]` or `{\"tasks\": [...]}`)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("tasks", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError(f"Expected a non-empty task list in {path}")
    tasks: list[SoakTask] = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Task #{idx} in {path} must be an object")
        tasks.append(
            SoakTask(
                id=str(item.get("id") or f"task-{idx}"),
                title=str(item.get("title") or f"Task {idx}"),
                prompt=str(item["prompt"]),
                verify_cmd=str(item.get("verify_cmd") or "python -m pytest -q"),
            )
        )
    return tasks


def default_soak_tasks() -> list[SoakTask]:
    """Built-in multi-phase engineering curriculum for soak testing."""
    return [
        SoakTask(
            id="phase-1-audit",
            title="Inspect project and fix existing failing tests",
            prompt=(
                "Analyze this repository, run the test suite with `bash` (`python -m pytest -q`), "
                "identify any failing tests or bugs, plan fixes with `todowrite`, fix the source code "
                "(do NOT weaken or delete assertions in existing tests), and re-run `python -m pytest -q` to verify."
            ),
        ),
        SoakTask(
            id="phase-2-feature",
            title="Add new feature module with unit tests",
            prompt=(
                "Plan with `todowrite` and implement a clean extension module/functionality appropriate for this repository, "
                "add comprehensive unit tests covering normal and edge cases, and verify the entire test suite passes."
            ),
        ),
        SoakTask(
            id="phase-3-edge-hardening",
            title="Edge-case hardening and regression tests",
            prompt=(
                "Review recent changes and existing modules for edge cases (empty inputs, invalid types, boundary values), "
                "add regression tests, fix any newly uncovered issues, and verify `python -m pytest -q` passes."
            ),
        ),
    ]


def _run_verify(workspace: Path, cmd: str) -> tuple[bool, int, str]:
    if not cmd.strip():
        return True, 0, ""
    proc = subprocess.run(
        cmd,
        cwd=str(workspace),
        shell=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode == 0, proc.returncode, out.strip()[-2000:]


class AutopilotSoakRunner:
    """Executes a long-running real-LLM Autopilot campaign with full trace logging."""

    def __init__(
        self,
        workspace: Path,
        log_dir: Path,
        model_id: str | None = None,
        target_minutes: float = 0.0,
        max_tasks: int = 0,
        resume: bool = False,
    ) -> None:
        self.workspace = workspace.resolve()
        self.log_dir = log_dir.resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.trace = SoakTraceWriter(self.log_dir / "trace.jsonl")
        self.checkpoint_path = self.log_dir / "checkpoint.json"
        self.model_id = model_id
        self.target_minutes = max(0.0, float(target_minutes))
        self.max_tasks = max(0, int(max_tasks))
        self.resume = bool(resume)
        self.storage = StorageManager(self.workspace)
        self.cfg = self.storage.load_config()

    def _save_checkpoint(
        self,
        session_id: str,
        completed_ids: list[str],
        results: list[SoakTaskResult],
        in_progress_id: str | None = None,
    ) -> None:
        payload = {
            "session_id": session_id,
            "completed_ids": completed_ids,
            "in_progress_id": in_progress_id,
            "results": [asdict(r) for r in results],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self.checkpoint_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.checkpoint_path)

    async def run(self, tasks: list[SoakTask]) -> dict[str, Any]:
        started_iso = datetime.now(timezone.utc).isoformat()
        t_start = time.monotonic()
        effective_model = self.model_id or str(self.cfg.get("model") or "rbit/smart-local")

        results: list[SoakTaskResult] = []
        completed_ids: set[str] = set()
        session: Session | None = None
        if self.resume and self.checkpoint_path.exists():
            try:
                ckpt = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
                completed_ids = set(ckpt.get("completed_ids") or [])
                for raw_r in ckpt.get("results") or []:
                    results.append(SoakTaskResult(**raw_r))
                sid = str(ckpt.get("session_id") or "")
                if sid:
                    session = self.storage.find_session(sid)
            except Exception:
                completed_ids = set()
        if session is None:
            session = Session(title=f"Autopilot Soak ({started_iso[:19]})")

        self.trace.emit(
            "soak_start",
            workspace=str(self.workspace),
            model=effective_model,
            api_base=str(self.cfg.get("api_base") or ""),
            target_minutes=self.target_minutes,
            task_count=len(tasks),
            resumed=self.resume and bool(completed_ids),
            resumed_completed_count=len(completed_ids),
        )

        task_idx = 0
        deadline = t_start + self.target_minutes * 60.0 if self.target_minutes > 0 else None

        while task_idx < len(tasks):
            if self.max_tasks and len(results) >= self.max_tasks:
                break
            if deadline is not None and time.monotonic() >= deadline and len(results) >= 1:
                break

            task = tasks[task_idx]
            task_idx += 1
            if task.id in completed_ids:
                continue
            self._save_checkpoint(session.id, sorted(completed_ids), results, in_progress_id=task.id)
            res = await self._run_single_task(task, session, effective_model)
            results.append(res)
            completed_ids.add(task.id)
            self._save_checkpoint(session.id, sorted(completed_ids), results, in_progress_id=None)

            # If a target duration was requested and we finished the provided list early,
            # run an additional autonomous verification/improvement pass until deadline.
            if (
                task_idx >= len(tasks)
                and deadline is not None
                and time.monotonic() < deadline
                and (not self.max_tasks or len(results) < self.max_tasks)
            ):
                cycle_num = len(results) + 1
                tasks.append(
                    SoakTask(
                        id=f"soak-cycle-{cycle_num}",
                        title=f"Autonomous quality & test expansion pass #{cycle_num}",
                        prompt=(
                            "Inspect the current codebase and git status, find one meaningful untested edge case, "
                            "code-quality improvement, or subtle bug, plan it with `todowrite`, implement it, "
                            "add unit tests, and run `python -m pytest -q` until all tests pass."
                        ),
                        verify_cmd=task.verify_cmd,
                    )
                )

        total_duration_s = round(time.monotonic() - t_start, 2)
        passed_count = sum(1 for r in results if r.verify_passed)
        total_count = len(results)
        success_rate = round((passed_count / total_count * 100.0) if total_count else 0.0, 1)
        total_in = sum(r.input_tokens for r in results)
        total_out = sum(r.output_tokens for r in results)
        total_tok = sum(r.total_tokens for r in results)
        total_cost = round(sum(r.cost_usd for r in results), 6)
        total_retries = sum(r.retries for r in results)
        total_errors = sum(len(r.errors) for r in results)
        false_claims = sum(1 for r in results if r.false_success_claim)

        summary: dict[str, Any] = {
            "started_at": started_iso,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "workspace": str(self.workspace),
            "model": effective_model,
            "api_base": str(self.cfg.get("api_base") or ""),
            "duration_seconds": total_duration_s,
            "duration_minutes": round(total_duration_s / 60.0, 2),
            "tasks_total": total_count,
            "tasks_passed": passed_count,
            "tasks_failed": total_count - passed_count,
            "success_rate_pct": success_rate,
            "input_tokens": total_in,
            "output_tokens": total_out,
            "total_tokens": total_tok,
            "cost_usd": total_cost,
            "total_retries": total_retries,
            "total_errors": total_errors,
            "false_success_claims": false_claims,
            "event_counts": dict(self.trace.counts),
            "trace_path": str(self.trace.path),
            "results": [asdict(r) for r in results],
        }
        self.trace.emit("soak_end", **{k: v for k, v in summary.items() if k != "results"})
        (self.log_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (self.log_dir / "report.md").write_text(
            render_soak_report(summary), encoding="utf-8"
        )
        return summary

    async def _run_single_task(
        self, task: SoakTask, session: Session, model_id: str
    ) -> SoakTaskResult:
        t0 = time.monotonic()
        self.trace.emit("task_start", task_id=task.id, title=task.title, prompt=task.prompt)

        executor = ToolExecutor(
            workspace=self.workspace,
            session=session,
            storage=self.storage,
            get_config=lambda: self.cfg,
        )
        executor.always_allow_bash = True
        executor.allow_outside_paths = False

        engine = AgentEngine(
            workspace=self.workspace,
            executor=executor,
            get_config=lambda: self.cfg,
            storage=self.storage,
        )

        llm_requests = 0
        tool_calls = 0
        retries = 0
        provider_cost_usd = 0.0
        saw_provider_usage = False
        mutated_files: list[str] = []
        errors: list[str] = []

        def on_trace(ev: dict[str, Any]) -> None:
            nonlocal llm_requests, retries, provider_cost_usd, saw_provider_usage
            ev_name = str(ev.pop("event", "trace"))
            if ev_name == "llm_request":
                llm_requests += 1
            if ev_name == "llm_response":
                u = ev.get("usage")
                if isinstance(u, dict) and u:
                    saw_provider_usage = True
                    provider_cost_usd += extract_provider_cost(u)
                if not ev.get("ok", True):
                    retries += 1
                    errors.append(str(ev.get("content_preview") or "llm_response error"))
            self.trace.emit(ev_name, task_id=task.id, **ev)

        engine.on_trace_event = on_trace

        async def on_tool_start(rec: ToolCallRecord) -> None:
            nonlocal tool_calls
            tool_calls += 1
            self.trace.emit(
                "tool_start",
                task_id=task.id,
                tool=rec.name,
                arguments=rec.arguments,
            )

        async def on_tool_end(rec: ToolCallRecord) -> None:
            self.trace.emit(
                "tool_end",
                task_id=task.id,
                tool=rec.name,
                status=rec.status,
                summary=str(rec.summary or "")[:600],
            )
            if rec.name in MUTATING_TOOLS and rec.status == "success":
                path_arg = str(rec.arguments.get("path") or rec.arguments.get("file_path") or "")
                if path_arg and path_arg not in mutated_files:
                    mutated_files.append(path_arg)
                self.trace.emit("file_change", task_id=task.id, tool=rec.name, path=path_arg)
            if rec.name == "bash":
                cmd = str(rec.arguments.get("command") or "")
                if any(tok in cmd for tok in ("pytest", "unittest", "ruff", "mypy")):
                    self.trace.emit(
                        "test_run",
                        task_id=task.id,
                        command=cmd,
                        status=rec.status,
                        summary=str(rec.summary or "")[:800],
                    )
            if rec.status != "success":
                errors.append(f"{rec.name}: {str(rec.summary or '')[:200]}")

        async def on_status(txt: str) -> None:
            nonlocal retries
            if "повтор" in txt.lower() or "retry" in txt.lower():
                retries += 1
            self.trace.emit("status", task_id=task.id, message=txt)

        user_msg = ChatMessage(
            id=short_id(),
            role="user",
            content=task.prompt,
            agent="autopilot",
        )
        session.messages.append(user_msg)
        self.storage.save_session(session)
        reply_preview = ""
        try:
            reply = await engine.run_turn(
                session=session,
                user_msg=user_msg,
                agent_mode="autopilot",
                model_id=model_id,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
                on_status=on_status,
            )
            session.messages.append(reply)
            self.storage.save_session(session)
            reply_preview = str(reply.content or "")[:600]
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            errors.append(err)
            self.trace.emit("task_error", task_id=task.id, error=err)

        verify_ok, verify_code, verify_out = _run_verify(self.workspace, task.verify_cmd)
        self.trace.emit(
            "task_verify",
            task_id=task.id,
            command=task.verify_cmd,
            passed=verify_ok,
            exit_code=verify_code,
            output=verify_out[-800:],
        )
        in_tok = int(getattr(engine, "_turn_in", 0) or 0)
        out_tok = int(getattr(engine, "_turn_out", 0) or 0)
        tot_tok = in_tok + out_tok
        model_info = get_model_info(model_id, getattr(engine, "extra_models", []))
        est_cost = round((tot_tok / 1000.0) * float(getattr(model_info, "cost_per_1k", 0.0) or 0.0), 6)
        cost_source = "provider" if provider_cost_usd > 0 else "estimated"
        usage_source = "provider" if saw_provider_usage else "estimated"
        cost_usd = round(provider_cost_usd, 6) if provider_cost_usd > 0 else est_cost
        claimed_success = _claims_success(reply_preview)
        false_claim = bool(claimed_success and not verify_ok)

        duration_s = round(time.monotonic() - t0, 2)
        self.trace.emit(
            "task_end",
            task_id=task.id,
            duration_s=duration_s,
            verify_passed=verify_ok,
            llm_requests=llm_requests,
            tool_calls=tool_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            cost_usd=cost_usd,
            cost_source=cost_source,
            usage_source=usage_source,
            retries=retries,
            false_success_claim=false_claim,
            errors_count=len(errors),
        )
        return SoakTaskResult(
            id=task.id,
            title=task.title,
            session_id=session.id,
            duration_s=duration_s,
            verify_passed=verify_ok,
            verify_exit_code=verify_code,
            verify_output=verify_out,
            llm_requests=llm_requests,
            tool_calls=tool_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            cost_usd=cost_usd,
            cost_source=cost_source,
            usage_source=usage_source,
            retries=retries,
            agent_claimed_success=claimed_success,
            false_success_claim=false_claim,
            mutated_files=mutated_files,
            errors=errors,
            reply_preview=reply_preview,
        )


def render_soak_report(summary: dict[str, Any]) -> str:
    """Format a reproducible Markdown report from soak summary data."""
    lines = [
        "# Autopilot Live-LLM Soak Report",
        "",
        f"- **Started:** `{summary['started_at']}`",
        f"- **Finished:** `{summary['finished_at']}`",
        f"- **Duration:** `{summary['duration_minutes']} min` (`{summary['duration_seconds']} s`)",
        f"- **Workspace:** `{summary['workspace']}`",
        f"- **Model:** `{summary['model']}` (`{summary['api_base']}`)",
        f"- **Tasks:** `{summary['tasks_passed']}/{summary['tasks_total']}` passed (**{summary['success_rate_pct']}%**)",
        f"- **Tokens:** `{summary.get('total_tokens', 0)}` (in=`{summary.get('input_tokens', 0)}`, out=`{summary.get('output_tokens', 0)}`)",
        f"- **Estimated Cost:** `${summary.get('cost_usd', 0.0):.6f}`",
        f"- **Retries / Errors:** `{summary.get('total_retries', 0)}` retries / `{summary.get('total_errors', 0)}` errors",
        f"- **False SUCCESS claims:** `{summary.get('false_success_claims', 0)}`",
        f"- **Trace log:** `{summary['trace_path']}`",
        "",
        "## Task Results",
        "",
        "| Task | Title | Duration (s) | LLM Req | Tool Calls | Tokens | Retries | Verify | Errors |",
        "|---|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for r in summary.get("results", []):
        status = "PASS" if r.get("verify_passed") else f"FAIL ({r.get('verify_exit_code')})"
        lines.append(
            f"| `{r['id']}` | {r['title']} | {r['duration_s']} | {r['llm_requests']} | {r['tool_calls']} | {r.get('total_tokens', 0)} | {r.get('retries', 0)} | **{status}** | {len(r.get('errors') or [])} |"
        )
    lines.extend(["", "## Event Counts", ""])
    for k, v in sorted((summary.get("event_counts") or {}).items()):
        lines.append(f"- `{k}`: {v}")
    return "\n".join(lines) + "\n"
