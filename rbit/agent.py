"""Dual-mode rbit Agent Engine: Real OpenAI-compatible SSE Streaming API + Built-in Smart Agent + Ollama Discovery."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .models import (
    ChatMessage,
    ModelInfo,
    Session,
    ToolCallRecord,
    count_tokens,
    get_model_info,
    short_id,
)
from .storage import StorageManager
from .tools import (
    OPENAI_TOOLS_SCHEMA,
    READONLY_TOOLS,
    VIRTUAL_ATTACHMENTS,
    ToolExecutor,
    ToolResult,
)

MAX_PARALLEL_TOOLS = 4
HISTORY_UNITS = 24
TOOL_HISTORY_CHARS = 2000
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
logger = logging.getLogger("rbit.agent")
HTTP_LIMITS = httpx.Limits(max_keepalive_connections=10, max_connections=20)

# Prompt profiles: accent + optionally restricted toolset. Selected via config
# `prompt_profile` (or `--prompt-profile` on the CLI).
TOOLLESS_MODEL_MARKERS = (
    "gemma-2", "gemma2", "gemma3", "gemma-3",
    "phi-2", "phi-3-small", "stablelm", "stable-lm",
    "tinyllama", "smollm", "ministral-1", "ministral-3b",
)


def model_supports_tools(model_id: str, cfg: dict[str, Any] | None = None) -> bool:
    """Heuristic + per-model override from config `model_capabilities`."""
    if cfg:
        caps = cfg.get("model_capabilities")
        if isinstance(caps, dict):
            override = caps.get(model_id)
            if isinstance(override, dict) and "tools" in override:
                return bool(override["tools"])
            if isinstance(override, bool):
                return override
    mid = (model_id or "").lower()
    return not any(marker in mid for marker in TOOLLESS_MODEL_MARKERS)


HEAVY_TASK_MARKERS = (
    "рефактор", "реализ", "мигр", "архитект", "оптимиз", "перепиш",
    "refactor", "implement", "migrate", "architecture", "optimiz", "rewrite",
)


def route_model_for_task(model_id: str, user_msg: Any, routing: dict[str, Any]) -> str:
    """Chooses light/heavy model by task size and keywords (config model_routing)."""
    light = str(routing.get("light") or model_id)
    heavy = str(routing.get("heavy") or model_id)
    content = (getattr(user_msg, "content", "") or "")
    lowered = content.lower()
    is_heavy = (
        len(content) > 600
        or bool(getattr(user_msg, "attachments", None))
        or any(w in lowered for w in HEAVY_TASK_MARKERS)
    )
    return heavy if is_heavy else light


PROMPT_PROFILES: dict[str, dict[str, Any]] = {
    "default": {"prompt": ""},
    "fast": {
        "prompt": (
            "PROFILE: fast — be maximally terse: minimal narration, the fewest tool calls "
            "that solve the task, no summaries unless asked."
        ),
    },
    "careful": {
        "prompt": (
            "PROFILE: careful — be defensive: re-read files before editing, verify changes "
            "with diagnostics/tests, briefly state trade-offs before acting."
        ),
    },
    "reviewer": {
        "prompt": (
            "PROFILE: reviewer — read-only code review. Inspect code and diffs, report findings "
            "with file:line references and priorities; never modify anything."
        ),
        "read_only": True,
        "allowed_tools": set(READONLY_TOOLS) | {"todowrite"},
    },
    "docs": {
        "prompt": (
            "PROFILE: docs — documentation focus: work on Markdown files, comments and docstrings; "
            "keep terminology consistent and examples runnable."
        ),
        "allowed_tools": {
            "read", "glob", "grep", "webfetch", "diagnostics", "lint", "symbols", "git",
            "write", "edit", "multi_edit", "apply_patch", "todowrite",
        },
    },
}

PROVIDER_LABELS = {
    "openrouter.ai": "OpenRouter",
    "api.openai.com": "OpenAI",
    "api.anthropic.com": "Anthropic",
    "api.deepseek.com": "DeepSeek",
    "generativelanguage.googleapis.com": "Google",
    "api.groq.com": "Groq",
    "api.mistral.ai": "Mistral",
    "api.together.xyz": "Together",
    "localhost": "Ollama",
    "127.0.0.1": "Ollama",
}


def provider_label_for(api_base: str) -> str:
    """Maps a provider base URL to a stable display category."""
    try:
        host = httpx.URL(api_base).host.lower()
    except Exception:
        host = (api_base or "").lower()
    for key, label in PROVIDER_LABELS.items():
        if key in host:
            return label
    return host or "Custom"


def _pretty_model_name(model_id: str) -> str:
    tail = model_id.rsplit("/", 1)[-1]
    return tail.replace("-", " ").replace("_", " ").strip() or model_id


EFFORT_LEVELS = ("auto", "low", "medium", "high")
REASONING_MARKERS = (
    "o1",
    "o3",
    "o4",
    "gpt-5",
    "deepseek-r1",
    "deepseek-reasoner",
    "qwq",
    "reasoning",
    "thinking",
)


def is_reasoning_model(model_id: str) -> bool:
    lowered = (model_id or "").lower()
    return any(marker in lowered for marker in REASONING_MARKERS)

ToolStartHook = Callable[[ToolCallRecord], Awaitable[None]]
ToolEndHook = Callable[[ToolCallRecord], Awaitable[None]]
StatusHook = Callable[[str], Awaitable[None]]
StreamHook = Callable[[str], Awaitable[None]]
ThinkingHook = Callable[[bool], Awaitable[None]]


SYSTEM_PROMPT_TEMPLATE = """You are rbit, an autonomous interactive CLI coding agent.
Workspace: {workspace}
Active Agent Mode: {agent_mode}
{shell_note}
{mode_note}
{memory_section}
Respond concisely in the user's language (Russian if the user writes in Russian) using GitHub-flavored Markdown.
Use tools (`read`, `glob`, `grep`, `symbols`, `git`, `diagnostics`, `write`, `edit`, `multi_edit`, `apply_patch`, `webfetch`, `bash`, `todowrite`) proactively when working with files, URLs, or tasks.
If `write`, `edit`, `multi_edit`, or `apply_patch` reports `Diagnostics` syntax errors in its tool output, fix them immediately in the same turn before finishing.
"""


async def discover_ollama_models(timeout: float = 1.2) -> list[ModelInfo]:
    """Auto-discovers locally installed Ollama models on localhost:11434."""
    discovered: list[ModelInfo] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get("http://localhost:11434/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("models", []):
                    name = item.get("name", "")
                    if name:
                        discovered.append(
                            ModelInfo(
                                id=f"ollama/{name}",
                                name=f"{name} (Local)",
                                provider="Ollama Local",
                                context_window=32_768,
                                cost_per_1k=0.0,
                            )
                        )
    except Exception:
        pass
    return discovered


class AgentEngine:
    def __init__(
        self,
        workspace: Path,
        executor: ToolExecutor,
        get_config: Callable[[], dict[str, Any]],
        storage: StorageManager | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.workspace = workspace
        self.executor = executor
        self.get_config = get_config
        self.storage = storage or StorageManager(workspace)
        self.http_client = http_client
        self.extra_models: list[ModelInfo] = []
        self.ollama_online: bool | None = None
        self.retry_attempts = 4
        self.retry_base_delay = 0.6
        self.retry_max_delay = 8.0
        self._provider_models_synced_at = 0.0
        self.last_api_latency: float | None = None
        self._active_profile_tools: set[str] | None = None
        self._active_profile_readonly: bool = False

    def _tools_for_payload(self) -> list[dict[str, Any]]:
        allowed = self._active_profile_tools
        if allowed is None:
            return OPENAI_TOOLS_SCHEMA
        return [t for t in OPENAI_TOOLS_SCHEMA if t["function"]["name"] in allowed]

    def _cached_messages(self, url: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Marks the stable system prefix cacheable for providers with prompt
        caching (OpenRouter/Anthropic `cache_control`), when enabled."""
        mode = str(self.get_config().get("prompt_caching", "auto")).lower()
        if mode == "off" or not messages or messages[0].get("role") != "system":
            return messages
        if mode != "on" and not any(host in url for host in ("openrouter.ai", "anthropic")):
            return messages
        if not isinstance(messages[0].get("content"), str):
            return messages
        cached = list(messages)
        cached[0] = {
            **messages[0],
            "content": [
                {
                    "type": "text",
                    "text": messages[0]["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        return cached

    def _retry_delay(self, attempt: int, resp: httpx.Response | None = None) -> float:
        if resp is not None:
            raw = resp.headers.get("retry-after")
            if raw:
                try:
                    return min(max(float(raw), 0.0), self.retry_max_delay)
                except ValueError:
                    pass
        return min(self.retry_base_delay * (2 ** (attempt - 1)), self.retry_max_delay)

    async def refresh_ollama_models(self) -> list[ModelInfo]:
        self.extra_models = await discover_ollama_models()
        self.ollama_online = bool(self.extra_models)
        return self.extra_models

    def add_provider_models(self, models: list[ModelInfo]) -> int:
        """Merges provider models into the picker list, keeping one category per provider."""
        existing = {m.id for m in self.extra_models}
        added = 0
        for model in models:
            if not model.id or model.id in existing:
                continue
            self.extra_models.append(model)
            existing.add(model.id)
            added += 1
        return added

    async def sync_provider_models(
        self,
        api_base: str,
        api_key: str,
        force: bool = False,
        min_interval: float = 60.0,
        timeout: float = 10.0,
    ) -> tuple[bool, str, int]:
        """Fetches provider models and merges them, throttled unless `force` is set."""
        if not api_base:
            return False, "Base URL is not set.", 0
        if (
            not force
            and self._provider_models_synced_at
            and time.monotonic() - self._provider_models_synced_at < min_interval
        ):
            return True, "Model list is up to date.", 0
        ok, message, models = await self.fetch_provider_models(api_base, api_key, timeout=timeout)
        if ok:
            self._provider_models_synced_at = time.monotonic()
        added = self.add_provider_models(models) if ok else 0
        return ok, message, added

    def load_cached_provider_models(self, cfg: dict[str, Any]) -> list[ModelInfo]:
        cached: list[ModelInfo] = []
        for item in cfg.get("provider_models") or []:
            if isinstance(item, dict) and item.get("id"):
                cached.append(ModelInfo.from_dict(item))
        return cached

    async def auto_discover_models(
        self, cfg: dict[str, Any], force: bool = False
    ) -> tuple[int, int]:
        """Loads cached models, refreshes the configured provider and local Ollama.

        Returns `(provider_count, ollama_count)` describing how many models are known.
        """
        self.add_provider_models(self.load_cached_provider_models(cfg))
        api_base = str(cfg.get("api_base") or "").strip()
        api_key = self.storage.resolve_api_key(cfg).strip()
        if api_base and api_key:
            await self.sync_provider_models(api_base, api_key, force=force, timeout=6.0)
        ollama = await discover_ollama_models()
        self.ollama_online = bool(ollama)
        self.add_provider_models(ollama)
        provider_count = sum(1 for m in self.extra_models if m.provider != "Ollama Local")
        return provider_count, len(ollama)

    def persistable_provider_models(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.extra_models if m.provider != "Ollama Local"]

    async def validate_api_key(self, api_base: str, api_key: str) -> tuple[bool, str]:
        """Checks a provider key with GET /models and returns a human-readable result."""
        ok, message, _ = await self.fetch_provider_models(api_base, api_key)
        return ok, message

    async def fetch_provider_models(
        self, api_base: str, api_key: str, timeout: float = 10.0
    ) -> tuple[bool, str, list[ModelInfo]]:
        """Validates a key with GET /models and returns every model the provider exposes."""
        base = (api_base or "").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            if self.http_client is not None:
                resp = await self.http_client.get(url, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=timeout, limits=HTTP_LIMITS) as client:
                    resp = await client.get(url, headers=headers)
        except Exception as exc:
            return False, f"Cannot reach {base}: {exc}", []
        if resp.status_code in (401, 403):
            return False, f"Invalid API key (HTTP {resp.status_code}).", []
        if resp.status_code >= 400:
            return False, f"Provider returned HTTP {resp.status_code} for /models.", []
        listed: list[Any] = []
        try:
            data = resp.json()
            raw = data.get("data") or data.get("models") or []
            if isinstance(raw, list):
                listed = raw
        except Exception:
            pass
        provider = provider_label_for(base)
        models: list[ModelInfo] = []
        seen: set[str] = set()
        for entry in listed:
            if isinstance(entry, str):
                model_id = entry
            elif isinstance(entry, dict):
                model_id = str(entry.get("id") or entry.get("name") or "")
            else:
                continue
            model_id = model_id.strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            context_window = 200_000
            cost_per_1k = 0.0
            if isinstance(entry, dict):
                try:
                    context_window = int(entry.get("context_length") or 200_000)
                except (TypeError, ValueError):
                    context_window = 200_000
                pricing = entry.get("pricing") or {}
                try:
                    prompt_p = float(pricing.get("prompt") or 0)
                    completion_p = float(pricing.get("completion") or 0)
                    if prompt_p or completion_p:
                        # Per-token prices from OpenRouter-style listings → per-1k blended.
                        cost_per_1k = round((prompt_p + completion_p) / 2 * 1000, 6)
                except (TypeError, ValueError):
                    cost_per_1k = 0.0
            models.append(
                ModelInfo(
                    id=model_id,
                    name=_pretty_model_name(model_id),
                    provider=provider,
                    context_window=context_window,
                    cost_per_1k=cost_per_1k,
                )
            )
        count = len(models)
        return True, f"Connected: {count} models available" if count else "Connected.", models

    def _reasoning_effort(self, model_id: str) -> str | None:
        """Resolves the configured effort for this model, or None to omit the parameter."""
        effort = str((self.get_config() or {}).get("effort") or "auto").lower()
        if effort == "auto":
            return "medium" if is_reasoning_model(model_id) else None
        if effort not in ("low", "medium", "high"):
            return None
        if not is_reasoning_model(model_id):
            return None
        return effort

    def _build_headers(self, api_key: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://rbit.cli",
            "X-Title": "rbit",
        }
        extra = (self.get_config() or {}).get("extra_headers")
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    async def run_turn(
        self,
        session: Session,
        user_msg: ChatMessage,
        agent_mode: str,
        model_id: str,
        on_tool_start: ToolStartHook,
        on_tool_end: ToolEndHook,
        on_status: StatusHook,
        on_stream_chunk: StreamHook | None = None,
        on_thinking: ThinkingHook | None = None,
        on_reasoning_chunk: StreamHook | None = None,
    ) -> ChatMessage:
        t0 = time.perf_counter()
        cfg = self.get_config()
        routing = cfg.get("model_routing")
        if isinstance(routing, dict) and routing.get("enabled"):
            routed = route_model_for_task(model_id, user_msg, routing)
            if routed and routed != model_id:
                if on_status is not None:
                    await on_status(f"роутинг: {routed}")
                model_id = routed
        self.executor.begin_turn()
        model_info = get_model_info(model_id, self.extra_models)
        api_key = self.storage.resolve_api_key(cfg).strip()
        api_base = (cfg.get("api_base") or "https://openrouter.ai/api/v1").rstrip("/")
        if api_base.endswith("/chat/completions"):
            api_base = api_base[: -len("/chat/completions")]

        is_ollama = (
            model_id.startswith("ollama/")
            or "ollama" in model_info.provider.lower()
            or "localhost:11434" in api_base
        )
        remote_model_id = model_id.split("/", 1)[1] if model_id.startswith("ollama/") else model_id
        use_remote = (not model_info.is_local_smart) and (bool(api_key) or is_ollama)

        tool_records: list[ToolCallRecord] = []
        logger.debug(
            "run_turn model=%s remote=%s ollama=%s mode=%s", model_id, use_remote, is_ollama, agent_mode
        )
        effective_base = (
            "http://localhost:11434/v1"
            if is_ollama and "openrouter.ai" in api_base
            else api_base
        )

        async def invoke_tool(name: str, args: dict[str, Any]) -> ToolResult:
            rec = ToolCallRecord(
                id=short_id(),
                name=name,
                arguments=args,
                status="running",
                summary=f"{name} ...",
            )
            tool_records.append(rec)
            await on_tool_start(rec)
            await asyncio.sleep(0.05)
            if name == "subagent":
                res = await self._run_subagent(
                    args,
                    model_id=remote_model_id if use_remote else "",
                    api_base=effective_base if use_remote else "",
                    api_key=api_key or "ollama",
                )
            else:
                res = await self.executor.execute(name, args, agent_mode=agent_mode)
            rec.status = "success" if res.ok else "error"
            rec.summary = res.summary
            rec.output = res.output
            rec.diff = res.diff
            rec.duration_ms = res.duration_ms
            rec.pid = res.pid
            await on_tool_end(rec)
            return res

        if use_remote:
            content = await self._run_openai_compatible(
                session=session,
                user_msg=user_msg,
                agent_mode=agent_mode,
                model_id=remote_model_id,
                api_base=effective_base,
                api_key=api_key or "ollama",
                invoke_tool=invoke_tool,
                on_status=on_status,
                on_stream_chunk=on_stream_chunk,
                on_thinking=on_thinking,
                on_reasoning_chunk=on_reasoning_chunk,
            )
        else:
            if not model_info.is_local_smart and not api_key:
                await on_status("API-ключ не задан — используется встроенный Smart-агент (/connect для API)")
            content = await self._run_smart_local_agent(
                session=session,
                user_msg=user_msg,
                agent_mode=agent_mode,
                invoke_tool=invoke_tool,
                on_status=on_status,
            )

        if use_remote and self._is_model_unavailable(content):
            mutated = {
                "write",
                "edit",
                "multi_edit",
                "apply_patch",
            }
            already_wrote = any(rec.name in mutated for rec in tool_records)
            await on_status("Модель недоступна (401/404) — fallback на rbit/smart-local...")
            if already_wrote:
                # The turn already modified files; letting the built-in agent run
                # would overwrite the model's real work with synthesized stubs.
                content = (
                    f"**[Fallback]** `{model_id}` стал недоступен (401/404) после того, как часть "
                    "инструментов уже изменила файлы. Встроенный Smart-агент не запускался, чтобы не "
                    "перезаписать эти правки. Нажмите `Ctrl+R` для повтора хода или продолжите другим промптом."
                )
            else:
                fallback_text = await self._run_smart_local_agent(
                    session=session,
                    user_msg=user_msg,
                    agent_mode=agent_mode,
                    invoke_tool=invoke_tool,
                    on_status=on_status,
                )
                content = (
                    f"**[Fallback]** `{model_id}` недоступна — выполнен встроенный Smart-агент "
                    "без обращения к API.\n\n" + fallback_text
                )

        elapsed = round(time.perf_counter() - t0, 2)
        tokens_est = max(
            12, count_tokens(content) + sum(count_tokens(tc.output) for tc in tool_records)
        )
        return ChatMessage(
            id=short_id(),
            role="assistant",
            content=content,
            agent=agent_mode,
            model=model_id,
            tokens=tokens_est,
            duration_s=elapsed,
            tool_calls=tool_records,
        )

    async def _run_openai_compatible(
        self,
        session: Session,
        user_msg: ChatMessage,
        agent_mode: str,
        model_id: str,
        api_base: str,
        api_key: str,
        invoke_tool: Callable[[str, dict[str, Any]], Awaitable[ToolResult]],
        on_status: StatusHook,
        on_stream_chunk: StreamHook | None = None,
        on_thinking: ThinkingHook | None = None,
        on_reasoning_chunk: StreamHook | None = None,
    ) -> str:
        mode_note = (
            "PLAN MODE ACTIVE: Do NOT modify files (`write`/`edit`/`multi_edit`/`apply_patch` disabled). Read code, analyze, and create a plan + `todowrite`."
            if agent_mode == "plan"
            else "BUILD MODE ACTIVE: Full access to read, search, edit/write files, fetch URLs, run commands, and manage todos."
        )
        project_mem = self.storage.load_project_memory()
        memory_section = f"\nProject Rules & Memory:\n{project_mem}\n" if project_mem else ""
        shell_note = (
            "Shell: Windows PowerShell 5.1 — write PowerShell commands "
            "(`Get-ChildItem`, `New-Item -ItemType Directory`, `$env:VAR`); "
            "Unix flags like `ls -la` fail."
            if os.name == "nt"
            else "Shell: POSIX sh/bash."
        )

        api_messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_TEMPLATE.format(
                    workspace=self.workspace,
                    agent_mode=agent_mode.upper(),
                    shell_note=shell_note,
                    mode_note=mode_note,
                    memory_section=memory_section,
                ),
            }
        ]

        cfg_prompt = self.get_config()
        profile = PROMPT_PROFILES.get(
            str(cfg_prompt.get("prompt_profile", "default")).lower(), PROMPT_PROFILES["default"]
        )
        self._active_profile_tools = profile.get("allowed_tools")
        self._active_profile_readonly = bool(profile.get("read_only"))
        system_text = api_messages[0]["content"]
        if agent_mode == "build" and cfg_prompt.get("plan_first", True):
            system_text += (
                "\n\nWORK STYLE: For multi-step tasks, begin your reply with a 1-3 line plan "
                "(short bullets) before the first tool call."
            )
        if profile["prompt"]:
            system_text += f"\n\n{profile['prompt']}"
        api_messages[0]["content"] = system_text

        units: list[list[dict[str, Any]]] = []
        for m in session.messages:
            if m.role == "user":
                text = m.content
                if m is user_msg and m.attachments:
                    att_sem = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

                    async def _read_attachment(rel: str) -> str:
                        if rel in VIRTUAL_ATTACHMENTS:
                            return await self._virtual_attachment(rel)
                        async with att_sem:
                            res = await self.executor.execute("read", {"filePath": rel, "limit": 160}, agent_mode)
                        return f'<file path="{rel}">\n{res.output}\n</file>'

                    attached_blocks = await asyncio.gather(*(_read_attachment(rel) for rel in m.attachments))
                    text = "\n\n".join(attached_blocks) + "\n\n" + text
                units.append([{"role": "user", "content": text}])
            elif m.role == "assistant":
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": m.content or "",
                }
                tool_messages: list[dict[str, Any]] = []
                if m.tool_calls:
                    calls: list[dict[str, Any]] = []
                    for tc in m.tool_calls:
                        try:
                            args_repr = json.dumps(tc.arguments, ensure_ascii=False)
                        except (TypeError, ValueError):
                            args_repr = "{}"
                        call_id = f"call_{tc.id}"
                        calls.append(
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": tc.name, "arguments": args_repr},
                            }
                        )
                        payload = tc.output or ""
                        if tc.diff:
                            payload = f"{payload}\n\nDiff:\n{tc.diff}" if payload else f"Diff:\n{tc.diff}"
                        tool_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "name": tc.name,
                                "content": payload[:TOOL_HISTORY_CHARS],
                            }
                        )
                    assistant_msg["tool_calls"] = calls
                units.append([assistant_msg] + tool_messages)
        for unit in units[-HISTORY_UNITS:]:
            api_messages.extend(unit)

        headers = self._build_headers(api_key)

        cfg_limits = self.get_config()
        max_steps = max(1, int(cfg_limits.get("max_steps", 50) or 50))
        raw_max_cost = cfg_limits.get("max_cost_usd")
        max_cost = float(raw_max_cost) if raw_max_cost not in (None, "", 0, "0") else None
        cost_per_1k = get_model_info(model_id, self.extra_models).cost_per_1k
        spent_usd = 0.0

        async def _drive_steps(client: httpx.AsyncClient) -> str:
            nonlocal spent_usd
            await on_status(f"Запрос к {model_id}...")
            step_texts: list[str] = []

            async def cumulative_chunk(partial: str) -> None:
                # The app renders ONE live card per turn: feed it the text of
                # every step so far, not just the current one.
                parts = [*step_texts, partial] if partial else list(step_texts)
                await on_stream_chunk("\n\n".join(p for p in parts if p))

            for _step in range(max_steps):
                if on_thinking is not None:
                    await on_thinking(True)
                try:
                    if on_stream_chunk is not None:
                        ok, content, tool_calls = await self._stream_chat_step(
                            client=client,
                            url=f"{api_base}/chat/completions",
                            headers=headers,
                            model_id=model_id,
                            messages=api_messages,
                            on_stream_chunk=cumulative_chunk,
                            on_status=on_status,
                            on_reasoning_chunk=on_reasoning_chunk,
                        )
                        if not ok:
                            return content
                    else:
                        ok, content, tool_calls = await self._non_stream_chat_step(
                            client=client,
                            url=f"{api_base}/chat/completions",
                            headers=headers,
                            model_id=model_id,
                            messages=api_messages,
                            on_status=on_status,
                            on_reasoning_chunk=on_reasoning_chunk,
                        )
                        if not ok:
                            return content
                finally:
                    if on_thinking is not None:
                        await on_thinking(False)

                if _step == 0 and content and tool_calls and on_status is not None:
                    first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
                    if first_line:
                        try:
                            await on_status(f"план: {first_line[:64]}")
                        except Exception:
                            logger.debug("status hook failed", exc_info=True)

                if not tool_calls:
                    # The reply must carry the text of every step of the turn,
                    # not just the last one (search/export/next-context depend on it).
                    parts = [*step_texts, content] if content and content.strip() else list(step_texts)
                    full = "\n\n".join(p for p in parts if p and p.strip())
                    return full or content or "(пустой ответ модели)"

                if content and content.strip():
                    step_texts.append(content)

                api_messages.append(
                    {
                        "role": "assistant",
                        "content": content or "",
                        "tool_calls": tool_calls,
                    }
                )
                await self._dispatch_tool_calls(
                    tool_calls=tool_calls,
                    api_messages=api_messages,
                    invoke_tool=invoke_tool,
                    on_status=on_status,
                )
                spent_usd += (
                    sum(count_tokens(str(m.get("content") or "")) for m in api_messages) / 1000.0
                ) * cost_per_1k
                if max_cost is not None and spent_usd >= max_cost:
                    return (
                        f"Достигнут лимит стоимости (${max_cost:.2f}, оценка ${spent_usd:.2f}). "
                        "Увеличьте `max_cost_usd` в конфиге, чтобы продолжить."
                    )

            return f"Достигнут лимит шагов вызова инструментов ({max_steps})."

        try:
            if self.http_client is not None:
                return await _drive_steps(self.http_client)
            async with httpx.AsyncClient(timeout=75.0, limits=HTTP_LIMITS) as client:
                return await _drive_steps(client)
        finally:
            self._active_profile_tools = None
            self._active_profile_readonly = False

    @staticmethod
    def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return {}

    def _format_tool_message(self, tc_id: str, fn_name: str, res: ToolResult) -> dict[str, Any]:
        out_payload = res.output
        if res.diff:
            out_payload += f"\n\nDiff:\n{res.diff}"
        limit = 6000
        try:
            limit = max(500, int((self.get_config() or {}).get("tool_output_chars", 6000)))
        except (TypeError, ValueError):
            limit = 6000
        if len(out_payload) > limit:
            out_payload = (
                out_payload[:limit]
                + f"\n\n[... truncated: {len(out_payload)} → {limit} chars; use grep or read with offset/limit]"
            )
        return {
            "role": "tool",
            "tool_call_id": tc_id,
            "name": fn_name,
            "content": out_payload,
        }

    async def _dispatch_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        api_messages: list[dict[str, Any]],
        invoke_tool: Callable[[str, dict[str, Any]], Awaitable[ToolResult]],
        on_status: StatusHook,
    ) -> None:
        """Runs contiguous read-only tool calls in parallel (Semaphore=4), keeping mutating tools as barriers."""

        async def _safe_invoke(name: str, args: dict[str, Any]) -> ToolResult:
            # One crashed tool must not kill the whole turn: report it to the
            # model as a failed tool result and keep going.
            if self._active_profile_readonly and name in {"write", "edit", "multi_edit", "apply_patch"}:
                return ToolResult(
                    False,
                    "rejected by profile",
                    "The active prompt profile is read-only; the tool was not run.",
                )
            if self._active_profile_tools is not None and name not in self._active_profile_tools:
                return ToolResult(
                    False,
                    "rejected by profile",
                    f"Tool `{name}` is not allowed by the active prompt profile.",
                )
            try:
                return await invoke_tool(name, args)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("tool %s crashed", name, exc_info=True)
                return ToolResult(
                    False,
                    f"{name} crashed: {type(exc).__name__}",
                    f"Tool `{name}` failed with an internal error: {type(exc).__name__}: {exc}",
                )

        batches: list[list[tuple[dict[str, Any], str, dict[str, Any]]]] = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            fn_name = fn.get("name", "")
            parsed_args = self._parse_tool_args(fn.get("arguments"))
            if tc.get("length_truncated") and not parsed_args and fn.get("arguments"):
                api_messages.append(
                    self._format_tool_message(
                        tc.get("id") or f"call_{short_id()}",
                        fn_name,
                        ToolResult(
                            False,
                            "response truncated",
                            "The model's tool-call arguments were cut off by max_tokens; the tool was not run.",
                        ),
                    )
                )
                continue
            item = (tc, fn_name, parsed_args)
            if fn_name in READONLY_TOOLS and batches and batches[-1][0][1] in READONLY_TOOLS:
                batches[-1].append(item)
            else:
                batches.append([item])

        for batch in batches:
            if len(batch) == 1:
                tc, fn_name, parsed_args = batch[0]
                res = await _safe_invoke(fn_name, parsed_args)
                api_messages.append(
                    self._format_tool_message(tc.get("id") or f"call_{short_id()}", fn_name, res)
                )
            else:
                await on_status(f"Параллельный вызов ({len(batch)} read-only инструментов)...")
                sem = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

                async def _bounded_call(name: str, args: dict[str, Any]) -> ToolResult:
                    async with sem:
                        return await _safe_invoke(name, args)

                results = await asyncio.gather(
                    *(_bounded_call(fn_name, parsed_args) for _, fn_name, parsed_args in batch)
                )
                for (tc, fn_name, _), res in zip(batch, results):
                    api_messages.append(
                        self._format_tool_message(
                            tc.get("id") or f"call_{short_id()}", fn_name, res
                        )
                    )

    async def _stream_chat_step(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        model_id: str,
        messages: list[dict[str, Any]],
        on_stream_chunk: StreamHook,
        on_status: StatusHook | None = None,
        on_reasoning_chunk: StreamHook | None = None,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        if self._is_ollama_base(url):
            return await self._ollama_chat_step(
                client, url, model_id, messages,
                on_stream_chunk=on_stream_chunk, on_status=on_status,
                on_reasoning_chunk=on_reasoning_chunk, stream=True,
            )
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": self._cached_messages(url, messages),
        }
        if model_supports_tools(model_id, self.get_config()):
            payload["tools"] = self._tools_for_payload()
            payload["tool_choice"] = "auto"
        payload["stream"] = True
        effort = self._reasoning_effort(model_id)
        if effort is not None:
            payload["reasoning_effort"] = effort
        full_text: list[str] = []
        reasoning_text: list[str] = []
        tc_acc: dict[int, dict[str, Any]] = {}
        last_tc_key = 0
        finish_reason: str | None = None

        for attempt in range(1, self.retry_attempts + 1):
            # Reset accumulators on every attempt: retrying with state from a
            # broken stream glues stale tool-call deltas onto the new response.
            full_text = []
            reasoning_text = []
            tc_acc = {}
            last_tc_key = 0
            finish_reason = None
            args_progress: dict[int, tuple[str, int]] = {}
            try:
                _t_req = time.perf_counter()
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if (
                        resp.status_code in RETRYABLE_STATUS
                        and attempt < self.retry_attempts
                        and not full_text
                        and not tc_acc
                    ):
                        await resp.aread()
                        delay = self._retry_delay(attempt, resp)
                        if on_status is not None:
                            await on_status(
                                f"HTTP {resp.status_code} — повтор {attempt}/{self.retry_attempts - 1} через {delay:.1f}s..."
                            )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code >= 400:
                        err_body = (await resp.aread()).decode("utf-8", errors="replace")
                        return (
                            False,
                            f"Ошибка API ({resp.status_code}): `{err_body[:300]}`\n\n"
                            "Откройте `/connect` для настройки ключа или выберите `rbit/smart-local` в `/models`.",
                            [],
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw_data = line[5:].strip()
                        if raw_data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw_data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
                        delta = choices[0].get("delta") or {}
                        reasoning_piece = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning_piece, str) and reasoning_piece:
                            reasoning_text.append(reasoning_piece)
                            if on_reasoning_chunk is not None:
                                try:
                                    await on_reasoning_chunk(reasoning_piece)
                                except Exception:
                                    logger.debug("reasoning hook failed", exc_info=True)
                        txt_piece = delta.get("content")
                        if txt_piece:
                            full_text.append(txt_piece)
                            try:
                                await on_stream_chunk("".join(full_text))
                            except Exception:
                                logger.debug("stream hook failed", exc_info=True)

                        for tc_delta in delta.get("tool_calls") or []:
                            raw_idx = tc_delta.get("index")
                            if raw_idx is not None:
                                try:
                                    last_tc_key = int(raw_idx)
                                except (TypeError, ValueError):
                                    last_tc_key = 0
                            elif tc_delta.get("id"):
                                # Providers that omit `index`: segment by tool-call id
                                # instead of merging parallel calls into one.
                                tc_id = tc_delta["id"]
                                last_tc_key = next(
                                    (k for k, v in tc_acc.items() if v["id"] == tc_id),
                                    max(tc_acc.keys(), default=-1) + 1,
                                )
                            # No index and no id: keep appending to the call we are
                            # already reading rather than collapsing into key 0.
                            entry = tc_acc.setdefault(
                                last_tc_key,
                                {
                                    "id": tc_delta.get("id") or f"call_{short_id()}",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            if tc_delta.get("id"):
                                entry["id"] = tc_delta["id"]
                            fn = tc_delta.get("function") or {}
                            if fn.get("name"):
                                entry["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                entry["function"]["arguments"] += fn["arguments"]
                            # Live feedback while the model streams tool arguments
                            # (name change or every ~512 chars).
                            if on_status is not None:
                                name_now = entry["function"]["name"]
                                args_len = len(entry["function"]["arguments"])
                                last_name, last_len = args_progress.get(last_tc_key, ("", -1))
                                if name_now != last_name or args_len - last_len >= 512:
                                    args_progress[last_tc_key] = (name_now, args_len)
                                    label = f"{name_now} · {args_len} B" if name_now else "tool call…"
                                    try:
                                        await on_status(f"стрим аргументов: {label}")
                                    except Exception:
                                        logger.debug("status hook failed", exc_info=True)
                if args_progress and on_status is not None:
                    # Clear the "streaming arguments" hint once the response ends.
                    try:
                        await on_status("")
                    except Exception:
                        logger.debug("status hook failed", exc_info=True)
                self.last_api_latency = time.perf_counter() - _t_req
                ordered_tcs = [tc_acc[k] for k in sorted(tc_acc.keys())]
                if finish_reason == "length":
                    for entry in ordered_tcs:
                        entry["length_truncated"] = True
                text = "".join(full_text)
                if not text and not ordered_tcs:
                    text = "".join(reasoning_text)
                return True, text, ordered_tcs
            except httpx.HTTPError as exc:
                if attempt < self.retry_attempts and not full_text and not tc_acc:
                    delay = self._retry_delay(attempt)
                    if on_status is not None:
                        await on_status(
                            f"Сеть недоступна ({exc}) — повтор {attempt}/{self.retry_attempts - 1} через {delay:.1f}s..."
                        )
                    await asyncio.sleep(delay)
                    continue
                return (
                    False,
                    f"Не удалось подключиться к `{url}`: **{exc}**.\n\n"
                    "Проверьте настройки через `/connect` или переключитесь на `rbit/smart-local` в `/models`.",
                    [],
                )
            except Exception as exc:
                return (
                    False,
                    f"Ошибка обработки ответа модели: **{type(exc).__name__}: {exc}**",
                    [],
                )

        return (False, f"Не удалось получить ответ от `{url}` после повторных попыток.", [])

    async def _non_stream_chat_step(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        model_id: str,
        messages: list[dict[str, Any]],
        on_status: StatusHook | None = None,
        on_reasoning_chunk: StreamHook | None = None,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        if self._is_ollama_base(url):
            return await self._ollama_chat_step(
                client, url, model_id, messages,
                on_stream_chunk=None, on_status=on_status,
                on_reasoning_chunk=on_reasoning_chunk, stream=False,
            )
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": self._cached_messages(url, messages),
        }
        if model_supports_tools(model_id, self.get_config()):
            payload["tools"] = self._tools_for_payload()
            payload["tool_choice"] = "auto"
        effort = self._reasoning_effort(model_id)
        if effort is not None:
            payload["reasoning_effort"] = effort
        for attempt in range(1, self.retry_attempts + 1):
            try:
                _t_req = time.perf_counter()
                resp = await client.post(url, headers=headers, json=payload)
                self.last_api_latency = time.perf_counter() - _t_req
            except httpx.HTTPError as exc:
                if attempt < self.retry_attempts:
                    delay = self._retry_delay(attempt)
                    if on_status is not None:
                        await on_status(
                            f"Сеть недоступна ({exc}) — повтор {attempt}/{self.retry_attempts - 1} через {delay:.1f}s..."
                        )
                    await asyncio.sleep(delay)
                    continue
                return (
                    False,
                    f"Не удалось подключиться к `{url}`: **{exc}**.\n\n"
                    "Проверьте `/connect` или переключитесь на `rbit/smart-local` в `/models`.",
                    [],
                )
            if resp.status_code in RETRYABLE_STATUS and attempt < self.retry_attempts:
                delay = self._retry_delay(attempt, resp)
                if on_status is not None:
                    await on_status(
                        f"HTTP {resp.status_code} — повтор {attempt}/{self.retry_attempts - 1} через {delay:.1f}s..."
                    )
                await asyncio.sleep(delay)
                continue
            if resp.status_code >= 400:
                return (
                    False,
                    f"Ошибка API ({resp.status_code}): `{resp.text[:300]}`\n\n"
                    "Откройте `/connect` для настройки ключа или выберите `rbit/smart-local` в `/models`.",
                    [],
                )
            try:
                data = resp.json()
            except ValueError:
                return (
                    False,
                    f"Ошибка обработки ответа модели: провайдер вернул не-JSON (`{resp.text[:200]}`).",
                    [],
                )
            choices = data.get("choices") or []
            if not choices:
                return (
                    False,
                    "Ошибка обработки ответа модели: провайдер вернул пустой `choices`.",
                    [],
                )
            choice = choices[0].get("message") or {}
            content = choice.get("content") or ""
            tool_calls = choice.get("tool_calls") or []
            for tc in tool_calls:
                if isinstance(tc, dict) and not tc.get("id"):
                    tc["id"] = f"call_{short_id()}"
            reasoning = choice.get("reasoning")
            if isinstance(reasoning, str) and reasoning and on_reasoning_chunk is not None:
                try:
                    await on_reasoning_chunk(reasoning)
                except Exception:
                    logger.debug("reasoning hook failed", exc_info=True)
            if not content and not tool_calls and isinstance(reasoning, str):
                content = reasoning
            return True, content, tool_calls
        return False, f"Не удалось получить ответ от `{url}` после повторных попыток.", []

    @staticmethod
    def _is_ollama_base(url: str) -> bool:
        return ":11434" in (url or "")

    @staticmethod
    def _ollama_url(url: str) -> str:
        return url.split("/chat/completions")[0].rstrip("/") + "/api/chat"

    def _ollama_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                out.append({"role": "tool", "content": str(m.get("content") or "")})
            elif role == "assistant" and m.get("tool_calls"):
                calls = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function") or {}
                    raw_args = fn.get("arguments") or "{}"
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except ValueError:
                            raw_args = {}
                    calls.append({"function": {"name": fn.get("name", ""), "arguments": raw_args}})
                out.append({
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": calls,
                })
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    async def _ollama_chat_step(
        self,
        client: httpx.AsyncClient,
        url: str,
        model_id: str,
        messages: list[dict[str, Any]],
        on_stream_chunk: StreamHook | None = None,
        on_status: StatusHook | None = None,
        on_reasoning_chunk: StreamHook | None = None,
        stream: bool = False,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """Native Ollama /api/chat path (NDJSON streaming, dict tool arguments)."""
        endpoint = self._ollama_url(url)
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": self._ollama_messages(messages),
            "stream": stream,
        }
        if model_supports_tools(model_id, self.get_config()) and self._tools_for_payload():
            payload["tools"] = self._tools_for_payload()
        full_text: list[str] = []
        reasoning_text: list[str] = []
        tcs: list[dict[str, Any]] = []
        try:
            if stream and on_stream_chunk is not None:
                async with client.stream("POST", endpoint, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        return False, f"Ollama error ({resp.status_code}): `{body[:200]}`", []
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except ValueError:
                            continue
                        msg = chunk.get("message") or {}
                        piece = msg.get("content")
                        if piece:
                            full_text.append(piece)
                            try:
                                await on_stream_chunk("".join(full_text))
                            except Exception:
                                logger.debug("stream hook failed", exc_info=True)
                        think = msg.get("thinking")
                        if isinstance(think, str) and think:
                            reasoning_text.append(think)
                            if on_reasoning_chunk is not None:
                                try:
                                    await on_reasoning_chunk(think)
                                except Exception:
                                    logger.debug("reasoning hook failed", exc_info=True)
                        for tc in msg.get("tool_calls") or []:
                            fn = tc.get("function") or {}
                            raw_args = fn.get("arguments") or {}
                            args_str = (
                                raw_args if isinstance(raw_args, str)
                                else json.dumps(raw_args, ensure_ascii=False)
                            )
                            tcs.append({
                                "id": f"call_{short_id()}",
                                "type": "function",
                                "function": {"name": fn.get("name", ""), "arguments": args_str},
                            })
                        if chunk.get("done"):
                            break
            else:
                resp = await client.post(endpoint, json=payload)
                if resp.status_code >= 400:
                    return False, f"Ollama error ({resp.status_code}): `{resp.text[:200]}`", []
                data = resp.json()
                msg = data.get("message") or {}
                text = msg.get("content") or ""
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    raw_args = fn.get("arguments") or {}
                    args_str = (
                        raw_args if isinstance(raw_args, str)
                        else json.dumps(raw_args, ensure_ascii=False)
                    )
                    tcs.append({
                        "id": f"call_{short_id()}",
                        "type": "function",
                        "function": {"name": fn.get("name", ""), "arguments": args_str},
                    })
                if not text and not tcs:
                    text = msg.get("thinking") or ""
                return True, text, tcs
        except httpx.HTTPError as exc:
            return False, f"Ollama недоступна: **{exc}**. Проверьте, что сервер запущен.", []
        text = "".join(full_text)
        if not text and not tcs:
            text = "".join(reasoning_text)
        return True, text, tcs

    async def _run_git_full(self, *args: str, timeout: float = 15.0) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, out.decode("utf-8", errors="replace").strip()
        except Exception as exc:
            return 1, f"git unavailable: {exc}"

    async def _run_git(self, *args: str, timeout: float = 15.0) -> str:
        code, out = await self._run_git_full(*args, timeout=timeout)
        return out if code == 0 else ""

    async def _virtual_attachment(self, name: str) -> str:
        """Expands @diff / @git / @staged / @tree into ready-to-use prompt context."""
        if name == "diff":
            content = await self._run_git("diff")
            return f'<git-diff path=".">\n{content[:8000] or "(no unstaged changes)"}\n</git-diff>'
        if name == "staged":
            content = await self._run_git("diff", "--staged")
            return f'<git-staged-diff>\n{content[:8000] or "(nothing staged)"}\n</git-staged-diff>'
        if name == "git":
            status = await self._run_git("status", "--short")
            log = await self._run_git("log", "--oneline", "-8")
            branch = await self._run_git("branch", "--show-current")
            return (
                "<git-state>\n"
                f"branch: {branch or '(not a git repo / detached)'}\n\n"
                f"status:\n{status or '(clean)'}\n\n"
                f"recent commits:\n{log or '(none)'}\n"
                "</git-state>"
            )
        if name == "tree":
            files = await asyncio.to_thread(self.executor.list_workspace_files, 200)
            listing = "\n".join(files) or "(workspace is empty)"
            return f"<workspace-tree count=\"{len(files)}\">\n{listing}\n</workspace-tree>"
        return ""

    async def _explore_deterministic(self, goal: str) -> str:
        keywords = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", goal) if len(w) >= 4][:3]
        files = await asyncio.to_thread(self.executor.list_workspace_files, 200)
        lines = [f"Workspace has {len(files)} file(s)."]
        if files:
            lines.append("Files: " + ", ".join(files[:12]) + (" ..." if len(files) > 12 else ""))
        for kw in keywords:
            res = await self.executor.execute("grep", {"pattern": re.escape(kw)}, agent_mode="plan")
            head = "\n".join(res.output.splitlines()[:8]) or "(no matches)"
            lines.append(f"\n`{kw}`:\n{head}")
        return "\n".join(lines)

    async def _explore_with_llm(
        self,
        goal: str,
        model_id: str,
        api_base: str,
        api_key: str,
    ) -> str:
        headers = self._build_headers(api_key)
        writable = {"write", "edit", "multi_edit", "apply_patch", "bash", "todowrite", "subagent"}
        tools = [t for t in OPENAI_TOOLS_SCHEMA if t["function"]["name"] not in writable]
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are an explore subagent inside rbit. You have ONLY read-only tools. "
                    f"Workspace: {self.workspace}\n"
                    "Investigate the user's goal and return a compact summary (max ~200 words) "
                    "with concrete `file:line` references. Do not ask questions."
                ),
            },
            {"role": "user", "content": goal},
        ]
        steps = 0
        last_content = ""
        used: list[str] = []
        client_ctx = self.http_client or httpx.AsyncClient(timeout=75.0, limits=HTTP_LIMITS)
        try:
            for _ in range(5):
                steps += 1
                try:
                    resp = await client_ctx.post(
                        f"{api_base}/chat/completions",
                        headers=headers,
                        json={
                            "model": model_id,
                            "messages": messages,
                            "tools": tools,
                            "tool_choice": "auto",
                        },
                    )
                except Exception as exc:
                    return f"Subagent network error: {exc}"
                if resp.status_code >= 400:
                    return f"Subagent API error ({resp.status_code}): {resp.text[:200]}"
                try:
                    choice = resp.json()["choices"][0]["message"]
                except (ValueError, KeyError, IndexError, TypeError):
                    return f"Subagent error: provider returned a malformed response (`{resp.text[:150]}`)"
                last_content = choice.get("content") or ""
                tool_calls = choice.get("tool_calls") or []
                if not tool_calls:
                    break
                messages.append(
                    {"role": "assistant", "content": last_content, "tool_calls": tool_calls}
                )
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name", "")
                    tc_id = tc.get("id") or f"call_{short_id()}"
                    if fn_name not in READONLY_TOOLS:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "name": fn_name,
                                "content": "Rejected: subagent is read-only.",
                            }
                        )
                        continue
                    used.append(fn_name)
                    res = await self.executor.execute(
                        fn_name, self._parse_tool_args(fn.get("arguments")), agent_mode="plan"
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": fn_name,
                            "content": res.output[:4000],
                        }
                    )
        finally:
            if self.http_client is None:
                await client_ctx.aclose()
        suffix = f"\n\n(subagent steps: {steps}, tools: {', '.join(sorted(set(used))) or 'none'})"
        return (last_content or "Subagent finished without findings.") + suffix

    async def _run_subagent(
        self,
        args: dict[str, Any],
        *,
        model_id: str,
        api_base: str,
        api_key: str,
    ) -> ToolResult:
        t0 = time.perf_counter()
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return ToolResult(False, "missing goal", "`goal` is required for subagent.")
        kind = str(args.get("agent") or "explore").strip().lower()
        if kind != "explore":
            return ToolResult(
                False, f"unsupported agent: {kind}", 'Only agent="explore" is supported for now.'
            )

        if model_id and api_base:
            text = await self._explore_with_llm(goal, model_id, api_base, api_key)
        else:
            text = await self._explore_deterministic(goal)

        duration = int((time.perf_counter() - t0) * 1000)
        return ToolResult(
            True,
            f"explore · {len(text)} chars",
            f"Subagent explore result for: {goal}\n\n{text}",
            duration_ms=duration,
        )

    @staticmethod
    def _is_model_unavailable(content: str) -> bool:
        return content.startswith(("Ошибка API (401", "Ошибка API (404"))

    @staticmethod
    def _transcript_for_summary(messages: list[ChatMessage]) -> str:
        lines: list[str] = []
        for m in messages:
            if m.role == "user":
                lines.append(f"USER: {m.content[:1200]}")
                continue
            for tc in m.tool_calls:
                lines.append(f"TOOL {tc.name}: {tc.summary}")
            if m.content:
                lines.append(f"ASSISTANT: {m.content[:1200]}")
        return "\n".join(lines)[:14000]

    @staticmethod
    def _heuristic_summary(messages: list[ChatMessage]) -> str:
        prompts = [m.content.strip().replace("\n", " ")[:140] for m in messages if m.role == "user"]
        touched: list[str] = []
        for m in messages:
            for tc in m.tool_calls:
                if tc.name in ("write", "edit", "multi_edit", "apply_patch"):
                    path = tc.arguments.get("filePath") or tc.arguments.get("path")
                    if path and path not in touched:
                        touched.append(str(path))
        goal_lines = "\n".join(f"- {p}" for p in prompts[-8:]) or "- (no user prompts)"
        files_line = ", ".join(f"`{f}`" for f in touched[:20]) or "none"
        return f"**Goals**\n{goal_lines}\n\n**Files touched**\n{files_line}"

    async def summarize_session(self, messages: list[ChatMessage], model_id: str) -> str:
        """Summarizes older messages via the active LLM, with a deterministic fallback."""
        cfg = self.get_config()
        api_key = self.storage.resolve_api_key(cfg).strip()
        api_base = (cfg.get("api_base") or "https://openrouter.ai/api/v1").rstrip("/")
        if api_base.endswith("/chat/completions"):
            api_base = api_base[: -len("/chat/completions")]
        info = get_model_info(model_id, self.extra_models)
        is_ollama = (
            model_id.startswith("ollama/")
            or "ollama" in info.provider.lower()
            or "localhost:11434" in api_base
        )
        remote_id = model_id.split("/", 1)[1] if model_id.startswith("ollama/") else model_id
        remote = (not info.is_local_smart) and (bool(api_key) or is_ollama)
        transcript = self._transcript_for_summary(messages)

        if remote:
            base = "http://localhost:11434/v1" if is_ollama and "openrouter.ai" in api_base else api_base
            headers = self._build_headers(api_key or "ollama")
            payload = {
                "model": remote_id,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You compact coding-agent sessions. Write a dense handoff summary of the "
                            "transcript: goals, decisions, files touched, verified facts, and open TODOs. "
                            "Max 220 words, Markdown bullets. No greetings."
                        ),
                    },
                    {"role": "user", "content": transcript or "(empty transcript)"},
                ],
                "temperature": 0.2,
            }
            try:
                if self.http_client is not None:
                    resp = await self.http_client.post(f"{base}/chat/completions", headers=headers, json=payload)
                else:
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        resp = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
                if resp.status_code < 400:
                    text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
                    if text:
                        return text
            except Exception:
                pass

        return self._heuristic_summary(messages)

    @staticmethod
    def _heuristic_commit_message(stat: str, diff: str) -> str:
        files: list[str] = []
        added = removed = 0
        for line in stat.splitlines():
            if "|" not in line:
                continue
            name = line.split("|", 1)[0].strip()
            if not name:
                continue
            files.append(name)
            plus = line.count("+")
            minus = line.count("-")
            added += plus
            removed += minus
        if not files:
            files = [
                ln[3:].strip()
                for ln in diff.splitlines()
                if ln.startswith(("+++ b/", "--- a/"))
            ][:5]
        scope = files[0].split("/")[0] if files else "workspace"
        kind = "docs" if files and all(f.endswith((".md", ".rst", ".txt")) for f in files) else "chore"
        subject = f"{kind}: update {len(files) or 'workspace'} file(s)" if files else "chore: update workspace"
        if len(files) == 1 and files[0]:
            subject = f"{kind}: update {files[0]}"
        body = "\n".join(f"- {name}" for name in files[:12])
        if len(files) > 12:
            body += f"\n- ...and {len(files) - 12} more"
        if not body:
            body = f"- {scope}"
        return f"{subject}\n\n{body}"

    async def draft_commit_message(self, stat: str, diff: str, model_id: str) -> str:
        """Drafts a Conventional Commits message from git diff, with a deterministic fallback."""
        cfg = self.get_config()
        api_key = self.storage.resolve_api_key(cfg).strip()
        api_base = (cfg.get("api_base") or "https://openrouter.ai/api/v1").rstrip("/")
        if api_base.endswith("/chat/completions"):
            api_base = api_base[: -len("/chat/completions")]
        info = get_model_info(model_id, self.extra_models)
        is_ollama = (
            model_id.startswith("ollama/")
            or "ollama" in info.provider.lower()
            or "localhost:11434" in api_base
        )
        remote_id = model_id.split("/", 1)[1] if model_id.startswith("ollama/") else model_id
        remote = (not info.is_local_smart) and (bool(api_key) or is_ollama)
        if remote:
            base = "http://localhost:11434/v1" if is_ollama and "openrouter.ai" in api_base else api_base
            headers = self._build_headers(api_key or "ollama")
            payload = {
                "model": remote_id,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You write Git commit messages from a diff. Follow Conventional Commits: "
                            "a concise imperative subject (max 72 chars, no trailing period), a blank line, "
                            "then 1-5 bullet points. Return ONLY the commit message, no code fences."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"git diff --stat:\n{stat[:3000]}\n\ndiff:\n{diff[:9000]}",
                    },
                ],
                "temperature": 0.2,
            }
            try:
                if self.http_client is not None:
                    resp = await self.http_client.post(f"{base}/chat/completions", headers=headers, json=payload)
                else:
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        resp = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
                if resp.status_code < 400:
                    text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
                    text = text.removeprefix("```").removeprefix("text").removesuffix("```").strip()
                    if text:
                        return text
            except Exception:
                pass
        return self._heuristic_commit_message(stat, diff)

    async def _run_smart_local_agent(
        self,
        session: Session,
        user_msg: ChatMessage,
        agent_mode: str,
        invoke_tool: Callable[[str, dict[str, Any]], Awaitable[ToolResult]],
        on_status: StatusHook,
    ) -> str:
        text = user_msg.content.strip()
        lower = text.lower()

        attached_summaries: list[str] = []
        attached_blocks: list[str] = []
        if user_msg.attachments:
            await on_status(f"Чтение контекста ({len(user_msg.attachments)} элементов)...")
            att_sem = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

            async def _read_att(rel: str) -> tuple[str, ToolResult | None, str]:
                if rel in VIRTUAL_ATTACHMENTS:
                    block = await self._virtual_attachment(rel)
                    return rel, None, block
                async with att_sem:
                    return rel, await invoke_tool("read", {"filePath": rel, "limit": 120}), ""

            att_results = await asyncio.gather(*(_read_att(rel) for rel in user_msg.attachments))
            for rel, res, block in att_results:
                if block:
                    attached_blocks.append(block)
                    attached_summaries.append(f"- `@{rel}` прикреплён ({len(block)} символов)")
                elif res is not None and res.ok:
                    attached_summaries.append(f"- `{rel}` прочитан ({res.summary})")

        # 1. Greetings / capabilities
        if re.fullmatch(r"(привет|ку|здравствуй|хай|йо|hello|hi|hey|кто ты\??|что ты умеешь\??)[!?.]*", lower):
            return (
                "Привет! Я **rbit** — автономный терминальный AI-агент и CLI для разработки.\n\n"
                "### Суперспособности rbit:\n"
                "- **Работа с кодом:** `@имя_файла`, `glob`, `grep`, `write`, `edit`, `multi_edit` с цветным **diff** и `webfetch` для чтения URL.\n"
                "- **Мгновенный `!команда` шелл:** начните ввод с `!` (например, `!git status` или `!pytest`), чтобы выполнить команду без траты токенов.\n"
                "- **Машина времени без Git:** `/checkpoint [имя]` сохраняет снимок всего проекта, `/rewind` мгновенно возвращает любое состояние.\n"
                "- **Память проекта и экспорт:** `/remember <правило>` записывает знания в `.rbit/memory.md`, `/export` выгружает сессию в Markdown.\n"
                "- **Два режима (`Tab`):** **Build** (полное редактирование) и **Plan** (безопасное проектирование + живой Todo-лист).\n"
                "- **CLI-комбайн:** `rbit run \"...\"`, `rbit -c`, `rbit doctor`, `rbit stats`, `rbit sessions`."
            )

        # 2. Webfetch URL request ("открой/скачай https://...")
        url_match = re.search(r"(https?://[^\s`\"'>]+)", text)
        if url_match and any(w in lower for w in ("открой", "скачай", "прочитай", "fetch", "сайт", "страниц", "url")):
            url = url_match.group(1)
            await on_status(f"Загрузка страницы {url}...")
            res = await invoke_tool("webfetch", {"url": url})
            return f"Содержимое `{url}` ({res.summary}):\n\n```text\n{res.output[:1800]}\n```"

        # 3. Explicit bash run request
        cmd_match = re.search(r"(?:выполни|запусти команду|run)\s+`?([^`]+)`?$", text, re.IGNORECASE)
        if cmd_match:
            cmd = cmd_match.group(1).strip()
            await on_status(f"Выполнение команды: {cmd}...")
            res = await invoke_tool("bash", {"command": cmd})
            status_word = "успешно выполнена" if res.ok else "завершилась с ошибкой или отклонена"
            return f"Команда `{cmd}` {status_word}.\n\n```text\n{res.output[:1500]}\n```"

        # 4. Search / grep request
        grep_match = re.search(r"(?:найди|ищи|поиск|grep)\s+['\"`]?([\w.\-/]+)['\"`]?", text, re.IGNORECASE)
        if grep_match and "файл" not in lower:
            term = grep_match.group(1)
            await on_status(f"Поиск `{term}` по кодовой базе...")
            res = await invoke_tool("grep", {"pattern": re.escape(term)})
            return f"Результаты поиска по паттерну `{term}`:\n\n```text\n{res.output[:1800]}\n```"

        # 5. Read specific file request
        read_match = re.search(r"(?:прочитай|покажи файл|открой)\s+([^\s]+)", text, re.IGNORECASE)
        if read_match:
            fpath = read_match.group(1).lstrip("@").strip("'\"`")
            await on_status(f"Чтение файла {fpath}...")
            res = await invoke_tool("read", {"filePath": fpath, "limit": 120})
            return f"Содержимое `{fpath}`:\n\n```text\n{res.output[:2200]}\n```"

        # 6. File creation / code generation in BUILD or PLAN mode
        create_match = re.search(
            r"(?:создай|сделай|напиши)\s+(?:файл|скрипт|модуль)?\s*([a-zA-Z0-9_\-/\\]+\.[a-zA-Z0-9]+)",
            text,
            re.IGNORECASE,
        )
        if create_match:
            filename = create_match.group(1)
            if agent_mode == "plan":
                await on_status("Составление плана в режиме PLAN...")
                await invoke_tool(
                    "todowrite",
                    {
                        "todos": [
                            {"content": f"Спроектировать структуру `{filename}`", "status": "completed", "priority": "high"},
                            {"content": f"Создать `{filename}` (переключите Tab -> BUILD)", "status": "pending", "priority": "high"},
                            {"content": "Проверить работу скрипта", "status": "pending", "priority": "medium"},
                        ]
                    },
                )
                return (
                    f"### План создания `{filename}` (режим PLAN)\n\n"
                    "В режиме **PLAN** модификация файлов отключена для безопасности.\n"
                    "1. Я подготовил список задач в панели **Todo** справа.\n"
                    "2. Нажмите **`Tab`**, чтобы переключиться в режим **BUILD**, и повторите запрос — файл будет создан с полным diff'ом!"
                )

            await on_status(f"Планирование создания {filename}...")
            todos = [
                {"content": f"Подготовить код для {filename}", "status": "in_progress", "priority": "high"},
                {"content": f"Записать файл {filename} и сформировать diff", "status": "pending", "priority": "high"},
                {"content": "Проверить результат", "status": "pending", "priority": "medium"},
            ]
            await invoke_tool("todowrite", {"todos": todos})

            code = self._synthesize_file_content(filename, text)
            todos[0]["status"] = "completed"
            todos[1]["status"] = "in_progress"
            await invoke_tool("todowrite", {"todos": todos})

            await on_status(f"Запись файла {filename}...")
            write_res = await invoke_tool("write", {"filePath": filename, "content": code})

            todos[1]["status"] = "completed"
            todos[2]["status"] = "completed"
            await invoke_tool("todowrite", {"todos": todos})

            return (
                f"Готово! Файл **`{filename}`** создан ({write_res.summary}).\n\n"
                "- Карточка `write` выше содержит полный цветной **diff** изменений.\n"
                "- Вы можете отменить правку через `/undo` или сохранить снимок проекта через `/checkpoint`."
            )

        # 7. Project analysis / init / todo planning
        if any(
            kw in lower
            for kw in ("анализ", "проект", "структур", "план", "туду", "тудо", "todo", "задач", "init", "архитектур", "файлы")
        ) or agent_mode == "plan":
            await on_status("Сканирование рабочей директории...")
            glob_res = await invoke_tool("glob", {"pattern": "*"})
            files = [ln for ln in glob_res.output.splitlines() if ln and not ln.startswith("No files")]

            await on_status("Формирование списка задач (Todo)...")
            sample_file = files[0] if files else None
            sample_info = ""
            if sample_file:
                read_res = await invoke_tool("read", {"filePath": sample_file, "limit": 40})
                if read_res.ok:
                    sample_info = f"\nПроверен ключевой файл `{sample_file}`."

            plan_todos = [
                {"content": "Изучить структуру файлов рабочей директории", "status": "completed", "priority": "high"},
                {"content": f"Проанализировать запрос: {text[:52]}", "status": "completed", "priority": "high"},
                {"content": "Согласовать следующие шаги реализации в режиме BUILD", "status": "in_progress", "priority": "medium"},
                {"content": "Запустить тесты и финальную верификацию", "status": "pending", "priority": "medium"},
            ]
            await invoke_tool("todowrite", {"todos": plan_todos})

            preview_list = "\n".join(f"- `{f}`" for f in files[:12]) or "- *(рабочая папка пуста или содержит скрытые файлы)*"
            extra = f"\n- *...и ещё {len(files) - 12} файлов*" if len(files) > 12 else ""
            return (
                f"### Обзор проекта и план действий\n\n"
                f"Найдено файлов в рабочей области (`{self.workspace.name}`): **{len(files)}**.{sample_info}\n\n"
                f"#### Ключевые файлы:\n{preview_list}{extra}\n\n"
                "#### Что сделано:\n"
                "- Обновлён интерактивный **Todo-лист** в правой боковой панели (`Ctrl+B`).\n"
                "- Используйте `/todo`, `@файл`, `!команда` или `/checkpoint` для удобной работы с проектом."
            )

        if attached_summaries:
            ctx_block = "\n".join(attached_summaries)
            preview = "\n\n".join(block[:1200] for block in attached_blocks)
            preview_section = f"\n\n```text\n{preview}\n```" if preview else ""
            return (
                f"Изучил прикреплённый контекст:\n{ctx_block}{preview_section}\n\n"
                f"Запрос: *{text}*\n\n"
                "Укажите целевой файл (например: `создай файл helper.py ...`) "
                "или подключите внешний LLM API / локальную Ollama через `/connect`."
            )

        await on_status("Проверка контекста рабочей папки...")
        dir_res = await invoke_tool("read", {"filePath": ".", "limit": 30})
        return (
            f"Принял запрос в режиме **{agent_mode.upper()}**: *{text}*\n\n"
            f"Текущее окружение `{self.workspace}` проверено (`{dir_res.summary}`).\n\n"
            "**Подсказки rbit:**\n"
            "- `создай файл example.py ...` — генерация кода с цветным diff и Todo-листом.\n"
            "- `!<команда>` — мгновенное выполнение команды в терминале.\n"
            "- `/checkpoint` и `/rewind` — снимки состояния проекта без Git.\n"
            "- `/connect` — подключение OpenRouter / OpenAI / DeepSeek / локальной Ollama."
        )

    def _synthesize_file_content(self, filename: str, prompt: str) -> str:
        ext = Path(filename).suffix.lower()
        lower = prompt.lower()
        if ext == ".py":
            if "калькулятор" in lower or "calc" in lower:
                return (
                    '"""Интерактивный консольный калькулятор, созданный rbit."""\n\n'
                    "def calculate(expr: str) -> float:\n"
                    "    allowed = set('0123456789+-*/(). %')\n"
                    "    if not set(expr).issubset(allowed):\n"
                    "        raise ValueError('Недопустимые символы в выражении')\n"
                    "    return float(eval(expr, {'__builtins__': {}}, {}))\n\n\n"
                    "def main() -> None:\n"
                    "    print('rbit Calculator (введите q для выхода)')\n"
                    "    while True:\n"
                    "        raw = input('> ').strip()\n"
                    "        if raw.lower() in ('q', 'exit'):\n"
                    "            break\n"
                    "        if not raw:\n"
                    "            continue\n"
                    "        try:\n"
                    "            print('=', calculate(raw))\n"
                    "        except Exception as err:\n"
                    "            print('Ошибка:', err)\n\n\n"
                    "if __name__ == '__main__':\n"
                    "    main()\n"
                )
            return (
                f'"""Модуль {filename} — сгенерирован агентом rbit.\n'
                f'Запрос: {prompt}\n"""\n\n'
                "from __future__ import annotations\n\n\n"
                "def main() -> None:\n"
                f"    print('Запущен модуль {filename}!')\n\n\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            )
        if ext in (".html", ".htm"):
            return (
                '<!doctype html>\n<html lang="ru">\n<head>\n'
                '  <meta charset="utf-8" />\n'
                f"  <title>{filename}</title>\n"
                "  <style>body{background:#0a0a0a;color:#eeeeee;font-family:monospace;padding:2rem;}"
                "h1{color:#fab283;}</style>\n"
                "</head>\n<body>\n"
                f"  <h1>rbit — {filename}</h1>\n"
                f"  <p>{prompt}</p>\n"
                "</body>\n</html>\n"
            )
        if ext == ".md":
            return f"# {Path(filename).stem}\n\nСгенерировано в **rbit**.\n\n- Запрос: {prompt}\n"
        if ext == ".json":
            return json.dumps({"name": Path(filename).stem, "generated_by": "rbit", "prompt": prompt}, ensure_ascii=False, indent=2) + "\n"
        return f"// {filename} — generated by rbit\n// {prompt}\n"
