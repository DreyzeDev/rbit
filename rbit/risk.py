"""Deterministic change-risk analysis for diffs before commit/review."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RISK_PATTERNS: list[tuple[str, str, int, str]] = [
    ("critical", r"PRIVATE KEY-----|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}", 40, "possible secret in diff"),
    ("high", r"\b(eval|exec)\s*\(|subprocess\.(?:Popen|run)\([^)]*shell\s*=\s*True", 25, "code execution"),
    ("high", r"(?i)\b(drop\s+(table|database)|truncate\s+table|delete\s+from\s+\w+\s*;)", 25, "destructive SQL"),
    ("high", r"rm\s+-[a-z]*r[a-z]*f|Remove-Item\s+.*-Recurse.*-Force|git\s+push\s+.*--force", 25, "destructive command"),
    ("medium", r"(?i)migrations?/|schema\.(?:sql|prisma)|alembic|drizzle", 12, "migration/schema change"),
    ("medium", r"(?i)(auth|permission|security|token|password|oauth)", 10, "security-sensitive area"),
    ("medium", r"(?i)(Dockerfile|\.github/workflows/|\.gitlab-ci|deploy|terraform)", 12, "deployment/CI change"),
    ("medium", r"(?i)(package-lock\.json|poetry\.lock|uv\.lock|requirements\.txt|pyproject\.toml)", 8, "dependency change"),
    ("low", r"TODO|FIXME|HACK|XXX", 3, "unresolved marker"),
]
SEVERITY_WEIGHT = {"critical": 100, "high": 60, "medium": 30, "low": 10}


@dataclass
class Finding:
    severity: str
    file: str
    line: int
    message: str
    excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "excerpt": self.excerpt,
        }


@dataclass
class RiskReport:
    score: int = 0
    level: str = "low"
    files: int = 0
    additions: int = 0
    deletions: int = 0
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "level": self.level,
            "files": self.files,
            "additions": self.additions,
            "deletions": self.deletions,
            "findings": [item.to_dict() for item in self.findings],
        }


def _git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(workspace), capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def analyze_risk(workspace: Path) -> RiskReport:
    workspace = workspace.resolve()
    diff = _git(workspace, "diff", "HEAD", "--unified=2")
    if not diff.strip():
        diff = _git(workspace, "diff", "--unified=2")
    report = RiskReport()
    current_file = ""
    current_line = 0
    seen: set[tuple[str, int, str]] = set()
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            current_file = raw[4:].strip().removeprefix("b/")
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            current_line = int(match.group(1)) if match else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            current_line += 1
            text = raw[1:]
            for severity, pattern, weight, message in RISK_PATTERNS:
                if re.search(pattern, text):
                    key = (current_file, current_line, message)
                    if key in seen:
                        continue
                    seen.add(key)
                    excerpt = text.strip()[:160]
                    report.findings.append(Finding(severity, current_file, current_line, message, excerpt))
                    report.score += weight
        elif raw.startswith("-") and not raw.startswith("---"):
            report.deletions += 1
        elif not raw.startswith("\\"):
            report.additions += 1
    for line in diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
    report.files = len({item.file for item in report.findings}) or len(
        [line for line in diff.splitlines() if line.startswith("+++ ")]
    )
    report.score = min(100, report.score)
    report.level = (
        "critical" if report.score >= 70 else "high" if report.score >= 40 else "medium" if report.score >= 15 else "low"
    )
    return report


def render_markdown(report: RiskReport) -> str:
    lines = [
        f"# Change risk: {report.level.upper()} ({report.score}/100)",
        "",
        f"- files touched: {report.files}",
        f"- changed lines: +{report.additions} / -{report.deletions}",
        "",
    ]
    if not report.findings:
        lines.append("No risky patterns detected.")
        return "\n".join(lines)
    for finding in sorted(report.findings, key=lambda item: -SEVERITY_WEIGHT.get(item.severity, 0)):
        lines.append(
            f"- **{finding.severity}** `{finding.file}:{finding.line}` — {finding.message}"
            + (f"\n  `{finding.excerpt}`" if finding.excerpt else "")
        )
    return "\n".join(lines)
