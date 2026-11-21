"""Skill discovery for `/skill` and the model-callable `skill` tool.

Project skills live in `.rbit/skills/<name>.md`, `.rbit/skills/<name>/SKILL.md`
(opencode style) and global skills in `~/.config/rbit/skills`.  Markdown may start
with a simple `key: value` frontmatter block (description, mode, attach).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MAX_SKILL_CHARS = 60_000


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta, text
    lines = text.splitlines()
    if len(lines) < 2:
        return meta, text
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return meta, text
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip().strip("\"'")
    body = "\n".join(lines[end + 1 :]).strip("\n")
    return meta, body


def _read_skill(path: Path) -> dict[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > MAX_SKILL_CHARS:
        text = text[:MAX_SKILL_CHARS] + "\n... (truncated)"
    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or (path.parent.name if path.name.lower() == "skill.md" else path.stem)
    return {
        "description": meta.get("description", ""),
        "mode": meta.get("mode", ""),
        "attach": meta.get("attach", ""),
        "body": body if body else text,
        "path": str(path),
        "name": name,
    }


def _collect(directory: Path, skills: dict[str, dict[str, str]], *, override: bool = False) -> None:
    """Reads skills from `directory`. Later layers pass `override=True` so that
    project skills replace global ones; within one directory the first wins."""
    if not directory.is_dir():
        return
    seen: set[str] = set()
    for entry in sorted(directory.iterdir()):
        skill: dict[str, str] | None = None
        if entry.is_file() and entry.suffix.lower() == ".md":
            skill = _read_skill(entry)
        elif entry.is_dir():
            nested = next(
                (c for c in sorted(entry.iterdir()) if c.is_file() and c.name.lower() == "skill.md"),
                None,
            )
            if nested is not None:
                skill = _read_skill(nested)
        if skill is None:
            continue
        name = skill["name"]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        _store(skills, name, skill, override=override)


def _store(skills: dict[str, dict[str, str]], name: str, skill: dict[str, str], *, override: bool) -> None:
    """Skill names match case-insensitively: `Deploy` and `deploy` are one skill."""
    existing = next((key for key in skills if key.lower() == name.lower()), None)
    if existing is not None:
        if not override:
            return
        del skills[existing]
    skills[name] = skill


def global_skill_dirs() -> list[Path]:
    home = Path.home()
    dirs = [home / ".config" / "rbit" / "skills", home / ".rbit" / "skills"]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        dirs.insert(0, Path(xdg) / "rbit" / "skills")
    return dirs


def load_skills(workspace: Path, *, include_global: bool = True) -> dict[str, dict[str, str]]:
    """Returns `{name: {description, mode, attach, body, path, name}}`; project wins."""
    skills: dict[str, dict[str, str]] = {}
    if include_global:
        for directory in global_skill_dirs():
            _collect(directory, skills)
    _collect(workspace / ".rbit" / "skills", skills, override=True)
    builtin_dir = Path(__file__).resolve().parent / "builtin_skills"
    if builtin_dir.is_dir():
        builtin: dict[str, dict[str, str]] = {}
        _collect(builtin_dir, builtin)
        for name, skill in builtin.items():
            _store(skills, name, skill, override=False)
    return skills


def skill_names(skills: dict[str, dict[str, str]]) -> list[str]:
    return sorted(skills)


def skills_prompt(skills: dict[str, dict[str, str]]) -> str:
    if not skills:
        return ""
    lines = ["Available skills (call the `skill` tool with a name to load full instructions):"]
    for name, meta in sorted(skills.items()):
        description = meta.get("description") or "(no description)"
        lines.append(f"- {name}: {description}")
    return "\n".join(lines)


def skill_tool_schema(skills: dict[str, dict[str, str]]) -> dict[str, Any] | None:
    if not skills:
        return None
    return {
        "type": "function",
        "function": {
            "name": "skill",
            "description": (
                "Load a reusable skill's full instructions by name. "
                f"Available: {', '.join(sorted(skills))}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name"},
                    "args": {"type": "string", "description": "Optional arguments substituted for {args}"},
                },
                "required": ["name"],
            },
        },
    }
