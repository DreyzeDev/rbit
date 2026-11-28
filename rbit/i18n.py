"""Minimal ru/en localization for user-facing TUI strings (config `locale`: "auto", "ru" or "en")."""

from __future__ import annotations

import os

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "agent_busy": "Agent is busy — press Esc first.",
        "auto_compact": "Context {pct:.0f}% — auto-compacting…",
        "build_by_phrase": "Build mode (from a phrase in the prompt).",
        "copied": "Copied the last answer to the clipboard.",
        "delete_busy": "Cannot delete the current session while the agent is busy — press Esc first.",
        "interrupted": "Interrupted by user (esc).",
        "is_current": "This is the current session.",
        "mcp_added": "MCP server added: {name} (connecting…)",
        "mcp_reconnect": "Reconnecting MCP servers…",
        "memory_added": "🧠 Memory updated (+{added}).",
        "no_pending_commit": "No pending commit — run /commit first.",
        "no_skills": "No skills. Create .rbit/skills/<name>.md",
        "no_tests": "No test command detected; use /test <command>.",
        "nothing_commit": "Nothing to commit — working tree is clean.",
        "nothing_copy": "Nothing to copy yet — no assistant answer in this session.",
        "nothing_fork": "Nothing to fork — session is empty.",
        "nothing_retry": "Nothing to retry — no user prompt in this session.",
        "nothing_undo": "Nothing to undo.",
        "plan_by_phrase": "Plan mode (from a phrase in the prompt).",
        "prefix_allowed": "“{prefix}” will no longer be asked about.",
        "prompt_filled": "Prompt filled in — press Enter to send.",
        "queued": "Queued — will run after the current turn: {preview}",
        "running": "Running: {cmd}",
        "summarising": "Summarising…",
        "tests_failed": "✗ Tests failed — feeding failures to the agent",
        "tests_passed": "✓ Tests passed",
        "todo_added": "Todo added: {text}",
        "turn_done": "Turn finished",
        "usage_bug": "Usage: /bug <what is broken>",
        "usage_fork": "Usage: /fork [N] — N is the message index to branch at.",
        "usage_mcp": "Usage: /mcp add <name> <command…> | /mcp add <name> --url <url> | /mcp remove <name>",
        "usage_mcp_cmd": "Usage: /mcp add <name> <command…>",
        "usage_mcp_url": "Usage: /mcp add <name> --url <https://…>",
    },
    "ru": {
        "agent_busy": "Агент занят — сначала нажмите Esc.",
        "auto_compact": "Контекст {pct:.0f}% — авто-компакция…",
        "build_by_phrase": "Режим Build (по фразе в промпте).",
        "copied": "Последний ответ скопирован в буфер обмена.",
        "delete_busy": "Нельзя удалить текущую сессию, пока агент занят — сначала нажмите Esc.",
        "interrupted": "Прервано пользователем (esc).",
        "is_current": "Это и есть текущая сессия.",
        "mcp_added": "MCP-сервер добавлен: {name} (подключаюсь…)",
        "mcp_reconnect": "Переподключаю MCP-серверы…",
        "memory_added": "🧠 Память пополнена (+{added}).",
        "no_pending_commit": "Нет ожидающего коммита — сначала выполните /commit.",
        "no_skills": "Нет скиллов. Создайте .rbit/skills/<name>.md",
        "no_tests": "Команда тестов не найдена; используйте /test <команда>.",
        "nothing_commit": "Коммитить нечего — рабочее дерево чистое.",
        "nothing_copy": "Копировать нечего — в этой сессии ещё нет ответа ассистента.",
        "nothing_fork": "Ветвить нечего — сессия пуста.",
        "nothing_retry": "Повторять нечего — в сессии нет запроса пользователя.",
        "nothing_undo": "Отменять нечего.",
        "plan_by_phrase": "Режим Plan (по фразе в промпте).",
        "prefix_allowed": "Команда «{prefix}» больше не спрашивается.",
        "prompt_filled": "Промпт подставлен — Enter для отправки.",
        "queued": "В очереди — выполнится после текущего хода: {preview}",
        "running": "Запуск: {cmd}",
        "summarising": "Собираю выжимку…",
        "tests_failed": "✗ Тесты упали — отправляю падения агенту",
        "tests_passed": "✓ Тесты прошли",
        "todo_added": "Задача добавлена: {text}",
        "turn_done": "Ход завершён",
        "usage_bug": "Использование: /bug <что сломано>",
        "usage_fork": "Использование: /fork [N] — N это индекс сообщения, с которого ветвить.",
        "usage_mcp": "Использование: /mcp add <имя> <команда…> | /mcp add <имя> --url <url> | /mcp remove <имя>",
        "usage_mcp_cmd": "Использование: /mcp add <имя> <команда…>",
        "usage_mcp_url": "Использование: /mcp add <имя> --url <https://…>",
    },
}

_current = "en"


def detect_locale(env: dict[str, str] | None = None) -> str:
    """`ru` when the user's environment says Russian, otherwise `en`."""
    source = env if env is not None else os.environ
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        value = (source.get(var) or "").lower()
        if value:
            return "ru" if value.startswith("ru") else "en"
    return "en"


def set_locale(name: str | None) -> str:
    """Selects the language ("auto" follows the environment); returns the one in effect."""
    global _current
    wanted = (name or "auto").strip().lower()
    if wanted == "auto":
        wanted = detect_locale()
    _current = wanted if wanted in _STRINGS else "en"
    return _current


def current_locale() -> str:
    return _current


def tr(key: str, **kwargs: object) -> str:
    text = _STRINGS.get(_current, {}).get(key) or _STRINGS["en"].get(key) or key
    return text.format(**kwargs) if kwargs else text
