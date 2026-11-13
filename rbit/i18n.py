"""Minimal ru/en localization for user-facing TUI strings (config `locale`)."""

from __future__ import annotations

_STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        "agent_busy": "Agent is busy — press Esc first.",
        "queued": "В очереди — выполнится после текущего хода: {preview}",
        "interrupted": "Interrupted by user (esc).",
        "no_tests": "No test command detected; use /test <command>.",
        "running": "Running: {cmd}",
        "tests_passed": "✓ Tests passed",
        "tests_failed": "✗ Tests failed — отправляю падения агенту",
        "nothing_undo": "Nothing to undo.",
        "copied": "Copied the last answer to the clipboard.",
        "todo_added": "Todo added: {text}",
    },
    "en": {
        "agent_busy": "Agent is busy — press Esc first.",
        "queued": "Queued — will run after the current turn: {preview}",
        "interrupted": "Interrupted by user (esc).",
        "no_tests": "No test command detected; use /test <command>.",
        "running": "Running: {cmd}",
        "tests_passed": "✓ Tests passed",
        "tests_failed": "✗ Tests failed — feeding failures to the agent",
        "nothing_undo": "Nothing to undo.",
        "copied": "Copied the last answer to the clipboard.",
        "todo_added": "Todo added: {text}",
    },
}

_current = "ru"


def set_locale(name: str) -> None:
    global _current
    _current = name if name in _STRINGS else "ru"


def tr(key: str, **kwargs: object) -> str:
    text = _STRINGS.get(_current, {}).get(key) or _STRINGS["ru"].get(key) or key
    return text.format(**kwargs) if kwargs else text
