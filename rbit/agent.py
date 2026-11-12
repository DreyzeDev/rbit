"""Dual-mode rbit Agent Engine: Real OpenAI-compatible SSE Streaming API + Built-in Smart Agent + Ollama Discovery."""

from __future__ import annotations

import asyncio
import json
import logging
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
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
logger = logging.getLogger("rbit.agent")

ToolStartHook = Callable[[ToolCallRecord], Awaitable[None]]
ToolEndHook = Callable[[ToolCallRecord], Awaitable[None]]
StatusHook = Callable[[str], Awaitable[None]]
StreamHook = Callable[[str], Awaitable[None]]


SYSTEM_PROMPT_TEMPLATE = """You are rbit, an autonomous interactive CLI coding agent.
Workspace: {workspace}
Active Agent Mode: {agent_mode}
{mode_note}
{memory_section}
Respond concisely in the user's language (Russian if the user writes in Russian) using GitHub-flavored Markdown.
Use tools (`read`, `glob`, `grep`, `symbols`, `git`, `diagnostics`, `write`, `edit`, `multi_edit`, `webfetch`, `bash`, `todowrite`) proactively when working with files, URLs, or tasks.
If `write`, `edit`, or `multi_edit` reports `Diagnostics` syntax errors in its tool output, fix them immediately in the same turn before finishing.
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
    ) -> ChatMessage:
        t0 = time.perf_counter()
        cfg = self.get_config()
        self.executor.begin_turn()
        model_info = get_model_info(model_id, self.extra_models)
        api_key = (cfg.get("api_key") or "").strip()
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
            await on_status("Модель недоступна (401/404) — fallback на rbit/smart-local...")
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
    ) -> str:
        mode_note = (
            "PLAN MODE ACTIVE: Do NOT modify files (`write`/`edit`/`multi_edit` disabled). Read code, analyze, and create a plan + `todowrite`."
            if agent_mode == "plan"
            else "BUILD MODE ACTIVE: Full access to read, search, edit/write files, fetch URLs, run commands, and manage todos."
        )
        project_mem = self.storage.load_project_memory()
        memory_section = f"\nProject Rules & Memory:\n{project_mem}\n" if project_mem else ""

        api_messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_TEMPLATE.format(
                    workspace=self.workspace,
                    agent_mode=agent_mode.upper(),
                    mode_note=mode_note,
                    memory_section=memory_section,
                ),
            }
        ]

        for m in session.messages[-18:]:
            if m.role in ("user", "assistant"):
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
                api_messages.append({"role": m.role, "content": text})

        headers = self._build_headers(api_key)

        async def _drive_steps(client: httpx.AsyncClient) -> str:
            for step in range(10):
                await on_status(f"Запрос к {model_id} (шаг {step + 1})...")
                if on_stream_chunk is not None:
                    ok, content, tool_calls = await self._stream_chat_step(
                        client=client,
                        url=f"{api_base}/chat/completions",
                        headers=headers,
                        model_id=model_id,
                        messages=api_messages,
                        on_stream_chunk=on_stream_chunk,
                        on_status=on_status,
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
                    )
                    if not ok:
                        return content

                if not tool_calls:
                    return content or "(пустой ответ модели)"

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

            return "Достигнут лимит шагов вызова инструментов (10)."

        if self.http_client is not None:
            return await _drive_steps(self.http_client)
        async with httpx.AsyncClient(timeout=75.0) as client:
            return await _drive_steps(client)

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

    @staticmethod
    def _format_tool_message(tc_id: str, fn_name: str, res: ToolResult) -> dict[str, Any]:
        out_payload = res.output
        if res.diff:
            out_payload += f"\n\nDiff:\n{res.diff}"
        return {
            "role": "tool",
            "tool_call_id": tc_id,
            "name": fn_name,
            "content": out_payload[:6000],
        }

    async def _dispatch_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        api_messages: list[dict[str, Any]],
        invoke_tool: Callable[[str, dict[str, Any]], Awaitable[ToolResult]],
        on_status: StatusHook,
    ) -> None:
        """Runs contiguous read-only tool calls in parallel (Semaphore=4), keeping mutating tools as barriers."""
        batches: list[list[tuple[dict[str, Any], str, dict[str, Any]]]] = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            fn_name = fn.get("name", "")
            parsed_args = self._parse_tool_args(fn.get("arguments"))
            item = (tc, fn_name, parsed_args)
            if fn_name in READONLY_TOOLS and batches and batches[-1][0][1] in READONLY_TOOLS:
                batches[-1].append(item)
            else:
                batches.append([item])

        for batch in batches:
            if len(batch) == 1:
                tc, fn_name, parsed_args = batch[0]
                res = await invoke_tool(fn_name, parsed_args)
                api_messages.append(self._format_tool_message(tc["id"], fn_name, res))
            else:
                await on_status(f"Параллельный вызов ({len(batch)} read-only инструментов)...")
                sem = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

                async def _bounded_call(name: str, args: dict[str, Any]) -> ToolResult:
                    async with sem:
                        return await invoke_tool(name, args)

                results = await asyncio.gather(
                    *(_bounded_call(fn_name, parsed_args) for _, fn_name, parsed_args in batch)
                )
                for (tc, fn_name, _), res in zip(batch, results):
                    api_messages.append(self._format_tool_message(tc["id"], fn_name, res))

    async def _stream_chat_step(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        model_id: str,
        messages: list[dict[str, Any]],
        on_stream_chunk: StreamHook,
        on_status: StatusHook | None = None,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "tools": OPENAI_TOOLS_SCHEMA,
            "tool_choice": "auto",
            "stream": True,
        }
        full_text: list[str] = []
        tc_acc: dict[int, dict[str, Any]] = {}

        for attempt in range(1, self.retry_attempts + 1):
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code in RETRYABLE_STATUS and attempt < self.retry_attempts and not full_text:
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
                        delta = choices[0].get("delta") or {}
                        txt_piece = delta.get("content")
                        if txt_piece:
                            full_text.append(txt_piece)
                            await on_stream_chunk("".join(full_text))

                        for tc_delta in delta.get("tool_calls") or []:
                            idx = int(tc_delta.get("index", 0))
                            entry = tc_acc.setdefault(
                                idx,
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
                ordered_tcs = [tc_acc[k] for k in sorted(tc_acc.keys())]
                return True, "".join(full_text), ordered_tcs
            except Exception as exc:
                if attempt < self.retry_attempts and not full_text:
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

        return (False, f"Не удалось получить ответ от `{url}` после повторных попыток.", [])

    async def _non_stream_chat_step(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        model_id: str,
        messages: list[dict[str, Any]],
        on_status: StatusHook | None = None,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "tools": OPENAI_TOOLS_SCHEMA,
            "tool_choice": "auto",
        }
        for attempt in range(1, self.retry_attempts + 1):
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except Exception as exc:
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
            data = resp.json()
            choice = data["choices"][0]["message"]
            return True, choice.get("content") or "", choice.get("tool_calls") or []
        return False, f"Не удалось получить ответ от `{url}` после повторных попыток.", []

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
        writable = {"write", "edit", "multi_edit", "bash", "todowrite", "subagent"}
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
        client_ctx = self.http_client or httpx.AsyncClient(timeout=75.0)
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
                choice = resp.json()["choices"][0]["message"]
                last_content = choice.get("content") or ""
                tool_calls = choice.get("tool_calls") or []
                if not tool_calls:
                    break
                messages.append(
                    {"role": "assistant", "content": last_content, "tool_calls": tool_calls}
                )
                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    if fn_name not in READONLY_TOOLS:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": fn_name,
                                "content": "Rejected: subagent is read-only.",
                            }
                        )
                        continue
                    used.append(fn_name)
                    res = await self.executor.execute(
                        fn_name, self._parse_tool_args(tc["function"].get("arguments")), agent_mode="plan"
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
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
                if tc.name in ("write", "edit", "multi_edit"):
                    path = tc.arguments.get("filePath") or tc.arguments.get("path")
                    if path and path not in touched:
                        touched.append(str(path))
        goal_lines = "\n".join(f"- {p}" for p in prompts[-8:]) or "- (no user prompts)"
        files_line = ", ".join(f"`{f}`" for f in touched[:20]) or "none"
        return f"**Goals**\n{goal_lines}\n\n**Files touched**\n{files_line}"

    async def summarize_session(self, messages: list[ChatMessage], model_id: str) -> str:
        """Summarizes older messages via the active LLM, with a deterministic fallback."""
        cfg = self.get_config()
        api_key = (cfg.get("api_key") or "").strip()
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
        api_key = (cfg.get("api_key") or "").strip()
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
