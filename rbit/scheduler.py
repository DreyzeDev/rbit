"""Persistent scheduled prompts (`.rbit/schedules.json`)."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import short_id


def _now() -> float:
    return time.time()


class Scheduler:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.path = self.workspace / ".rbit" / "schedules.json"

    def load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def save(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def next_run(spec: str, *, after: float | None = None) -> float | None:
        """Parses `every 30m`, `daily HH:MM`, or an ISO datetime into epoch seconds."""
        base = after if after is not None else _now()
        value = spec.strip().lower()
        match = re.fullmatch(r"every\s+(\d+)\s*([smhd])", value)
        if match:
            amount = int(match.group(1))
            unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
            return base + amount * unit
        match = re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})", value)
        if match:
            now = datetime.fromtimestamp(base, tz=timezone.utc).astimezone()
            target = now.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
            if target.timestamp() <= base:
                target += timedelta(days=1)
            return target.timestamp()
        try:
            return datetime.fromisoformat(spec.strip()).timestamp()
        except ValueError:
            pass
        cron = spec.strip().split()
        if len(cron) == 5:
            return Scheduler._next_cron(cron, base)
        return None

    @staticmethod
    def _cron_field(field: str, value: int, *, minimum: int, maximum: int) -> bool:
        for part in field.split(","):
            part = part.strip()
            step = 1
            if "/" in part:
                part, step_text = part.split("/", 1)
                try:
                    step = max(1, int(step_text))
                except ValueError:
                    continue
            if part == "*":
                start, end = minimum, maximum
            elif "-" in part:
                try:
                    start_text, end_text = part.split("-", 1)
                    start, end = int(start_text), int(end_text)
                except ValueError:
                    continue
            else:
                try:
                    start = end = int(part)
                except ValueError:
                    continue
            if start <= value <= end and (value - start) % step == 0:
                return True
        return False

    @staticmethod
    def _next_cron(fields: list[str], base: float) -> float | None:
        minute, hour, day, month, weekday = fields
        current = datetime.fromtimestamp(base, tz=timezone.utc).astimezone().replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = current + timedelta(days=366)
        while current < limit:
            if (
                Scheduler._cron_field(minute, current.minute, minimum=0, maximum=59)
                and Scheduler._cron_field(hour, current.hour, minimum=0, maximum=23)
                and Scheduler._cron_field(day, current.day, minimum=1, maximum=31)
                and Scheduler._cron_field(month, current.month, minimum=1, maximum=12)
                and Scheduler._cron_field(weekday, (current.weekday() + 1) % 7, minimum=0, maximum=6)
            ):
                return current.timestamp()
            current += timedelta(minutes=1)
        return None

    def add(self, prompt: str, spec: str, *, name: str = "", model: str = "", agent: str = "build") -> tuple[dict[str, Any] | None, str]:
        prompt = prompt.strip()
        if not prompt:
            return None, "prompt is required"
        next_run = self.next_run(spec)
        if next_run is None:
            return None, f"invalid schedule: {spec!r} (use 'every 30m', 'daily 09:30', or ISO datetime)"
        entries = self.load()
        entry = {
            "id": short_id(),
            "name": (name or prompt[:40]).strip(),
            "prompt": prompt,
            "schedule": spec.strip(),
            "model": model,
            "agent": agent,
            "enabled": True,
            "created_at": _now(),
            "last_run": None,
            "next_run": next_run,
        }
        entries.append(entry)
        self.save(entries)
        return entry, ""

    def remove(self, target: str) -> bool:
        entries = self.load()
        remaining = [entry for entry in entries if entry.get("id") != target and entry.get("name") != target]
        if len(remaining) == len(entries):
            return False
        self.save(remaining)
        return True

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        current = now if now is not None else _now()
        return [
            entry
            for entry in self.load()
            if entry.get("enabled", True) and float(entry.get("next_run") or 0) <= current
        ]

    def mark_run(self, entry_id: str, *, next_run: float | None) -> None:
        entries = self.load()
        for entry in entries:
            if entry.get("id") == entry_id:
                entry["last_run"] = _now()
                if next_run is None:
                    entry["enabled"] = False
                else:
                    entry["next_run"] = next_run
                break
        self.save(entries)

    def describe(self) -> list[str]:
        rows = []
        for entry in self.load():
            state = "enabled" if entry.get("enabled", True) else "done"
            next_run = entry.get("next_run")
            when = datetime.fromtimestamp(float(next_run), tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M") if next_run else "-"
            rows.append(f"{entry.get('id')}  [{state}]  {entry.get('name')}  ({entry.get('schedule')})  next={when}")
        return rows
