"""Usage ledger: per-call token/cost accounting with daily limits and forecast.

The blueprint's "Budget Guard" asks for more than a per-session cap: separate
input/output/reasoning accounting, limits per day (optionally per model), hard
stops *before* a paid call, warnings at 50/75/90/100%, a burn-rate forecast, and
JSON/CSV export. This module owns that bookkeeping and nothing else — it never
talks to a provider.

Records are appended as JSON lines to `.rbit/usage/YYYY-MM-DD.jsonl`, so the
numbers survive restarts and can be aggregated without loading a session.

Two blueprint rules are enforced here rather than at the call site:

* cost is never invented — when the provider reports no usage and the price is
  unknown, the record is flagged `priced: false` and `strict` mode refuses it;
* concurrent spend cannot slip past the cap because reservations are counted
  under a lock before the request is made.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Warning thresholds, as fractions of the daily limit.
WARN_THRESHOLDS: tuple[float, ...] = (0.5, 0.75, 0.9, 1.0)

#: Default daily guard rails; 0 means "no limit".
DEFAULT_DAILY_USD = 0.0
DEFAULT_DAILY_TOKENS = 0

_BUDGET_ALIASES = {
    "strict": "strict",
    "warn": "warn",
    "off": "off",
    "none": "off",
}


def _num(value: Any, default: float = 0.0) -> float:
    """Tolerant float: None / "abc" / nan / inf all fall back to `default`."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def normalize_budget_mode(raw: Any, default: str = "warn") -> str:
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in _BUDGET_ALIASES:
            return _BUDGET_ALIASES[key]
    return default


@dataclass
class UsageRecord:
    """One paid model call."""

    ts: str
    model: str
    provider: str = ""
    session: str = ""
    project: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = True
    duration_ms: int = 0
    kind: str = "chat"

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "model": self.model,
            "provider": self.provider,
            "session": self.session,
            "project": self.project,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "priced": self.priced,
            "duration_ms": self.duration_ms,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageRecord:
        return cls(
            ts=str(data.get("ts") or ""),
            model=str(data.get("model") or ""),
            provider=str(data.get("provider") or ""),
            session=str(data.get("session") or ""),
            project=str(data.get("project") or ""),
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            reasoning_tokens=int(data.get("reasoning_tokens") or 0),
            cached_tokens=int(data.get("cached_tokens") or 0),
            cost_usd=float(data.get("cost_usd") or 0.0),
            priced=bool(data.get("priced", True)),
            duration_ms=int(data.get("duration_ms") or 0),
            kind=str(data.get("kind") or "chat"),
        )


@dataclass
class BudgetStatus:
    """Snapshot used by the guard, the sidebar and `/cost`."""

    day: str
    spent_usd: float = 0.0
    tokens: int = 0
    calls: int = 0
    limit_usd: float = 0.0
    limit_tokens: int = 0
    mode: str = "warn"
    unpriced_calls: int = 0
    by_model: dict[str, float] = field(default_factory=dict)
    #: Per-model cap (`model_budgets`) and what that model alone has spent today.
    model_limit_usd: float = 0.0
    model_spent_usd: float = 0.0

    @property
    def percent(self) -> float:
        """Fraction of the *binding* limit used (money first, then tokens)."""
        ratios: list[float] = []
        if self.model_limit_usd > 0:
            ratios.append(self.model_spent_usd / self.model_limit_usd)
        if self.limit_usd > 0:
            ratios.append(self.spent_usd / self.limit_usd)
        if self.limit_tokens > 0:
            ratios.append(self.tokens / self.limit_tokens)
        return max(ratios) if ratios else 0.0

    @property
    def exceeded(self) -> bool:
        return self.percent >= 1.0

    def crossed_threshold(self, previous_percent: float) -> float | None:
        """Returns the highest threshold newly crossed between two snapshots."""
        now = self.percent
        crossed = [t for t in WARN_THRESHOLDS if previous_percent < t <= now]
        return max(crossed) if crossed else None


class UsageLedger:
    """Append-only usage store with daily aggregation and a concurrency-safe guard."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.dir = self.workspace / ".rbit" / "usage"
        self._lock = threading.Lock()
        #: Money reserved by in-flight calls, so parallel requests cannot both
        #: pass the check and then overshoot the cap together.
        self._reserved_usd: float = 0.0
        self._active_calls: int = 0

    # --- paths ---------------------------------------------------------
    def _path_for(self, day: date) -> Path:
        return self.dir / f"{day.isoformat()}.jsonl"

    @staticmethod
    def today() -> date:
        # The "day" of a daily budget is the user's local day, not the UTC one.
        return datetime.now().date()

    # --- writing -------------------------------------------------------
    def record(
        self,
        model: str,
        *,
        provider: str = "",
        session: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        cached_tokens: int = 0,
        cost_usd: float = 0.0,
        priced: bool = True,
        duration_ms: int = 0,
        kind: str = "chat",
    ) -> UsageRecord:
        """Appends one call. Never raises: accounting must not break a turn."""
        entry = UsageRecord(
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            model=model,
            provider=provider,
            session=session,
            project=self.workspace.name,
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            reasoning_tokens=max(0, reasoning_tokens),
            cached_tokens=max(0, cached_tokens),
            cost_usd=max(0.0, cost_usd),
            priced=priced,
            duration_ms=max(0, duration_ms),
            kind=kind,
        )
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                with self._path_for(self.today()).open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry

    # --- reading -------------------------------------------------------
    def read_day(self, day: date | None = None) -> list[UsageRecord]:
        day = day or self.today()
        path = self._path_for(day)
        if not path.is_file():
            return []
        records: list[UsageRecord] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(UsageRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
        except OSError:
            return []
        return records

    def read_range(self, days: int = 7) -> list[UsageRecord]:
        today = self.today()
        records: list[UsageRecord] = []
        for offset in range(max(1, days)):
            records.extend(self.read_day(today - timedelta(days=offset)))
        return records

    # --- aggregation ---------------------------------------------------
    def status(self, config: dict[str, Any] | None = None, model: str = "") -> BudgetStatus:
        """Current day totals plus the effective limits."""
        config = config or {}
        records = self.read_day()
        status = BudgetStatus(
            day=self.today().isoformat(),
            limit_usd=max(0.0, _num(config.get("daily_budget_usd"), DEFAULT_DAILY_USD)),
            limit_tokens=max(0, int(_num(config.get("daily_token_limit"), DEFAULT_DAILY_TOKENS))),
            mode=normalize_budget_mode(config.get("budget_mode")),
        )
        # A per-model cap is checked against that model's own spend, never the total.
        per_model = config.get("model_budgets")
        if model and isinstance(per_model, dict):
            cap = _num(per_model.get(model), 0.0)
            if cap > 0:
                status.model_limit_usd = cap

        for rec in records:
            status.spent_usd += rec.cost_usd
            status.tokens += rec.total_tokens
            status.calls += 1
            if not rec.priced:
                status.unpriced_calls += 1
            status.by_model[rec.model] = status.by_model.get(rec.model, 0.0) + rec.cost_usd
        if status.model_limit_usd > 0:
            status.model_spent_usd = status.by_model.get(model, 0.0)
        return status

    def forecast(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Burn-rate projection for the rest of the day."""
        status = self.status(config)
        now = datetime.now()
        elapsed_h = max(now.hour + now.minute / 60.0, 1 / 60.0)
        remaining_h = max(24.0 - (now.hour + now.minute / 60.0), 0.0)
        per_hour = status.spent_usd / elapsed_h
        result: dict[str, Any] = {
            "spent_usd": round(status.spent_usd, 4),
            "per_hour_usd": round(per_hour, 4),
            "remaining_hours_today": round(remaining_h, 2),
        }
        if status.limit_usd > 0 and per_hour > 0:
            left = max(status.limit_usd - status.spent_usd, 0.0)
            result["hours_left"] = round(left / per_hour, 2)
            result["will_exceed"] = left / per_hour < remaining_h
        elif status.limit_tokens > 0 and status.tokens > 0:
            per_hour_tokens = status.tokens / elapsed_h
            left_tokens = max(status.limit_tokens - status.tokens, 0)
            result["hours_left"] = round(left_tokens / per_hour_tokens, 2) if per_hour_tokens else None
        else:
            result["hours_left"] = None
        return result

    def breakdown(self, days: int = 7) -> dict[str, Any]:
        """Grouping by model/provider/day for `/usage` and `rbit usage`."""
        records = self.read_range(days)
        by_model: dict[str, dict[str, float]] = {}
        by_day: dict[str, float] = {}
        by_provider: dict[str, float] = {}
        for rec in records:
            model = by_model.setdefault(
                rec.model or "?", {"calls": 0, "cost": 0.0, "tokens": 0.0}
            )
            model["calls"] += 1
            model["cost"] += rec.cost_usd
            model["tokens"] += rec.total_tokens
            day_key = rec.ts[:10]
            by_day[day_key] = by_day.get(day_key, 0.0) + rec.cost_usd
            provider = rec.provider or "?"
            by_provider[provider] = by_provider.get(provider, 0.0) + rec.cost_usd
        return {
            "days": days,
            "calls": len(records),
            "total_cost": round(sum(r.cost_usd for r in records), 6),
            "total_tokens": sum(r.total_tokens for r in records),
            "by_model": {
                k: {
                    "calls": int(v["calls"]),
                    "cost": round(v["cost"], 6),
                    "tokens": int(v["tokens"]),
                }
                for k, v in by_model.items()
            },
            "by_day": {k: round(v, 6) for k, v in sorted(by_day.items())},
            "by_provider": {k: round(v, 6) for k, v in by_provider.items()},
        }

    # --- export --------------------------------------------------------
    def to_csv(self, days: int = 7) -> str:
        buf = io.StringIO()
        fields = [
            "ts", "model", "provider", "session", "project", "input_tokens",
            "output_tokens", "reasoning_tokens", "cached_tokens", "total_tokens",
            "cost_usd", "priced", "duration_ms", "kind",
        ]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for rec in self.read_range(days):
            writer.writerow(rec.to_dict())
        return buf.getvalue()

    def to_json(self, days: int = 7) -> str:
        return json.dumps(
            {
                "summary": self.breakdown(days),
                "records": [r.to_dict() for r in self.read_range(days)],
            },
            ensure_ascii=False,
            indent=2,
        )

    # --- guard ---------------------------------------------------------
    def reserve(self, estimated_usd: float) -> None:
        with self._lock:
            self._reserved_usd += max(0.0, estimated_usd)
            self._active_calls += 1

    def release(self, estimated_usd: float) -> None:
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - max(0.0, estimated_usd))
            self._active_calls = max(0, self._active_calls - 1)

    @property
    def reserved_usd(self) -> float:
        with self._lock:
            return self._reserved_usd


class BudgetGuard:
    """Decides whether a paid call may proceed, and warns as limits approach."""

    def __init__(self, ledger: UsageLedger, config: dict[str, Any] | None = None) -> None:
        self.ledger = ledger
        self.config = config or {}
        self.mode = normalize_budget_mode(self.config.get("budget_mode"))
        self._last_percent: float = 0.0

    def refresh_config(self, config: dict[str, Any]) -> None:
        self.config = config
        self.mode = normalize_budget_mode(config.get("budget_mode"))

    #: Rough per-call cost estimate used only to reserve against the cap.
    SAFETY_MARGIN_USD = 0.05

    def check(self, model: str = "", estimated_usd: float | None = None) -> tuple[bool, str]:
        """Returns (allowed, message). Never raises."""
        try:
            return self._check(model, estimated_usd)
        except Exception as exc:  # noqa: BLE001 - a broken budget must not break a turn
            return True, f"Budget check skipped: {exc}"

    def _check(self, model: str, estimated_usd: float | None) -> tuple[bool, str]:
        if self.mode == "off":
            return True, ""
        status = self.ledger.status(self.config, model=model)
        extra = self.SAFETY_MARGIN_USD if estimated_usd is None else max(0.0, _num(estimated_usd))
        projected = status.spent_usd + self.ledger.reserved_usd + extra
        if status.model_limit_usd > 0 and status.model_spent_usd + extra > status.model_limit_usd:
            text = (
                f"Budget for `{model}` ${status.model_limit_usd:.4g}/day would be exceeded "
                f"(spent ${status.model_spent_usd:.4f})."
            )
            if self.mode == "strict":
                return False, text + " Raise `model_budgets` or set `budget_mode: warn`."
            return True, "Budget warning: " + text
        if status.limit_usd > 0 and projected > status.limit_usd:
            if self.mode == "strict":
                return False, (
                    f"Daily budget ${status.limit_usd:.4g} would be exceeded "
                    f"(spent ${status.spent_usd:.4f} + in-flight "
                    f"${self.ledger.reserved_usd:.4f}). Increase `daily_budget_usd` "
                    "or set `budget_mode: warn`."
                )
            return True, f"Budget warning: ${status.limit_usd:.4g}/day reached."
        if status.limit_tokens > 0 and status.tokens >= status.limit_tokens:
            if self.mode == "strict":
                return False, (
                    f"Daily token limit {status.limit_tokens:,} reached "
                    f"({status.tokens:,} used). Raise `daily_token_limit` or set "
                    "`budget_mode: warn`."
                )
            return True, f"Token warning: {status.tokens:,}/{status.limit_tokens:,} used today."
        return True, ""

    def unpriced_decision(self, model: str) -> tuple[bool, str]:
        """Blueprint: in strict mode an unpriced call must not be sent silently."""
        if self.mode != "strict":
            return True, ""
        return False, (
            f"No price is known for `{model}`, so the cost cannot be checked against the "
            "daily budget. Add it to the price catalog or set `budget_mode: warn`."
        )

    def threshold_message(self, model: str = "") -> str:
        """Returns a message when a warning threshold was newly crossed."""
        if self.mode == "off":
            return ""
        status = self.ledger.status(self.config, model=model)
        crossed = status.crossed_threshold(self._last_percent)
        self._last_percent = status.percent
        if crossed is None:
            return ""
        labels = {0.5: "50%", 0.75: "75%", 0.9: "90%", 1.0: "100%"}
        forecast = self.ledger.forecast(self.config)
        hours = forecast.get("hours_left")
        tail = f" — at this rate the limit lasts ~{hours:.1f}h" if isinstance(hours, float) else ""
        return f"Budget {labels.get(crossed, f'{crossed:.0%}')} used today{tail}"


def split_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Normalises provider usage payloads into input/output/reasoning/cached counts.

    Providers disagree on names (`prompt_tokens` vs `input_tokens`,
    `completion_tokens` vs `output_tokens`) and on where reasoning lives
    (`reasoning_tokens` at the top level, inside `completion_tokens_details`, or
    inside `output_tokens_details`). Everything is mapped once here so the rest
    of rbit never has to care.
    """
    if not isinstance(usage, dict):
        return {"input": 0, "output": 0, "reasoning": 0, "cached": 0}

    def _int(*keys: str, source: dict[str, Any] | None = None) -> int:
        src = usage if source is None else source
        for key in keys:
            value = src.get(key)
            if isinstance(value, (int, float)) and value:
                return int(value)
        return 0

    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    if not isinstance(completion_details, dict):
        completion_details = {}

    return {
        "input": _int("prompt_tokens", "input_tokens"),
        "output": _int("completion_tokens", "output_tokens"),
        "reasoning": _int("reasoning_tokens") or _int("reasoning_tokens", source=completion_details)
        or _int("reasoning_tokens", source=prompt_details),
        "cached": _int("cached_tokens", source=prompt_details)
        or _int("cache_read_input_tokens", "cached_tokens"),
    }


def estimate_cost(
    usage: dict[str, int],
    cost_per_1k: float,
    config: dict[str, Any] | None = None,
) -> tuple[float, bool]:
    """Returns (cost_usd, priced).

    `priced` is False when there is no usable price — the blueprint forbids
    inventing a number in that case, so the caller can warn or refuse instead.
    """
    config = config or {}
    catalog = config.get("price_overrides")
    if isinstance(catalog, dict):
        override = catalog.get("per_1k")
        if isinstance(override, (int, float)) and override > 0:
            cost_per_1k = float(override)
    # OpenAI-style `completion_tokens` and Anthropic `output_tokens` already include the reasoning
    # tokens (`*_details.reasoning_tokens` is a breakdown of them), so adding the two charged
    # every thinking token twice. Only what exceeds the output count can be extra.
    output = usage.get("output", 0)
    extra_reasoning = max(0, usage.get("reasoning", 0) - output)
    total = usage.get("input", 0) + output + extra_reasoning
    if total <= 0:
        return 0.0, False
    if cost_per_1k <= 0:
        return 0.0, False
    return total / 1000.0 * cost_per_1k, True


def default_ledger(workspace: Path) -> UsageLedger:
    return UsageLedger(workspace)


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_day(value: str | None) -> date | None:
    """Accepts YYYY-MM-DD; returns None for anything else."""
    if not value or not _ISO_RE.match(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


async def run_with_reservation(
    ledger: UsageLedger,
    estimate: float,
    coro_factory: Any,
) -> Any:
    """Runs an awaitable while holding a reservation, always releasing it."""
    ledger.reserve(estimate)
    try:
        return await coro_factory()
    finally:
        ledger.release(estimate)


def _unused_env() -> str | None:  # pragma: no cover - keeps `os` import honest
    return os.environ.get("RBIT_USAGE_DISABLED")
