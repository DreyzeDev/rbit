"""Persistent scheduled prompts (`.rbit/schedules.json`)."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import short_id


def _now() -> float:
    return time.time()


LOCK_MAX_AGE = 6 * 3600.0


def acquire_lock(lock_path: Path, *, max_age: float = LOCK_MAX_AGE) -> bool:
    """Create an exclusive lock file holding our PID.

    A lock whose PID is dead, or that is older than `max_age`, is stale and is replaced.
    """
    from .service import pid_alive

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lock_path.read_text(encoding="utf-8").strip() or 0)
                age = time.time() - lock_path.stat().st_mtime
            except (OSError, ValueError):
                pid, age = 0, max_age + 1
            if pid > 0 and pid != os.getpid() and pid_alive(pid) and age < max_age:
                return False
            try:
                lock_path.unlink()
            except OSError:
                return False
            continue
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True
    return False


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
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: readers never see a half-written file

    @staticmethod
    def next_run(spec: str, *, after: float | None = None) -> float | None:
        """Parses `every 30m`, `daily HH:MM`, or an ISO datetime into epoch seconds."""
        base = after if after is not None else _now()
        value = spec.strip().lower()
        match = re.fullmatch(r"every\s+(\d+)\s*([smhd])", value)
        if match:
            amount = int(match.group(1))
            if amount < 1:
                return None
            unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
            return base + amount * unit
        match = re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})", value)
        if match:
            if int(match.group(1)) > 23 or int(match.group(2)) > 59:
                return None
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
        day_restricted = day.strip() != "*"
        weekday_restricted = weekday.strip() != "*"
        while current < limit:
            cron_wd = (current.weekday() + 1) % 7  # 0 = Sunday
            day_ok = Scheduler._cron_field(day, current.day, minimum=1, maximum=31)
            weekday_ok = Scheduler._cron_field(weekday, cron_wd, minimum=0, maximum=7) or (
                cron_wd == 0 and Scheduler._cron_field(weekday, 7, minimum=0, maximum=7)
            )
            if day_restricted and weekday_restricted:
                date_ok = day_ok or weekday_ok  # classic cron: either field may match
            else:
                date_ok = day_ok and weekday_ok
            if (
                date_ok
                and Scheduler._cron_field(minute, current.minute, minimum=0, maximum=59)
                and Scheduler._cron_field(hour, current.hour, minimum=0, maximum=23)
                and Scheduler._cron_field(month, current.month, minimum=1, maximum=12)
            ):
                return current.timestamp()
            current += timedelta(minutes=1)
        return None

    def add(self, prompt: str, spec: str, *, name: str = "", model: str = "", agent: str = "build", unattended: bool = True) -> tuple[dict[str, Any] | None, str]:
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
            "unattended": unattended,
            "created_at": _now(),
            "last_run": None,
            "next_run": next_run,
        }
        entries.append(entry)
        self.save(entries)
        return entry, ""

    def remove(self, target: str) -> bool:
        entries = self.load()
        index = next((i for i, entry in enumerate(entries) if entry.get("id") == target), None)
        if index is None:
            index = next((i for i, entry in enumerate(entries) if entry.get("name") == target), None)
        if index is None:
            return False
        self.save(entries[:index] + entries[index + 1 :])  # exactly one entry
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
