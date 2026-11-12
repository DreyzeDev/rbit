"""Dual-mode rbit Agent Engine: Real OpenAI-compatible SSE Streaming API + Built-in Smart Agent + Ollama Discovery."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .models import ChatMessage, ModelInfo, Session, ToolCallRecord, get_model_info, short_id
from .storage import StorageManager
from .tools import OPENAI_TOOLS_SCHEMA, READONLY_TOOLS, ToolExecutor, ToolResult

MAX_PARALLEL_TOOLS = 4

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
Use tools (`read`, `glob`, `grep`, `write`, `edit`, `multi_edit`, `webfetch`, `bash`, `todowrite`) proactively when working with files, URLs, or tasks.
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

    async def refresh_ollama_models(self) -> list[ModelInfo]:
        self.extra_models = await discover_ollama_models()
        return self.extra_models

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
            res = await self.executor.execute(name, args, agent_mode=agent_mode)
            rec.status = "success" if res.ok else "error"
            rec.summary = res.summary
            rec.output = res.output
            rec.diff = res.diff
            rec.duration_ms = res.duration_ms
            await on_tool_end(rec)
            return res

        if use_remote:
            effective_base = (
                "http://localhost:11434/v1"
                if is_ollama and "openrouter.ai" in api_base
                else api_base
            )
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

        elapsed = round(time.perf_counter() - t0, 2)
        tokens_est = max(12, len(content) // 4 + sum(len(tc.output) // 6 for tc in tool_records))
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
                        async with att_sem:
                            res = await self.executor.execute("read", {"filePath": rel, "limit": 160}, agent_mode)
                        return f'<file path="{rel}">\n{res.output}\n</file>'

                    attached_blocks = await asyncio.gather(*(_read_attachment(rel) for rel in m.attachments))
                    text = "\n\n".join(attached_blocks) + "\n\n" + text
                api_messages.append({"role": m.role, "content": text})

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://rbit.cli",
            "X-Title": "rbit",
        }

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

        try:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
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
        except Exception as exc:
            return (
                False,
                f"Не удалось подключиться к `{url}`: **{exc}**.\n\n"
                "Проверьте настройки через `/connect` или переключитесь на `rbit/smart-local` в `/models`.",
                [],
            )

        ordered_tcs = [tc_acc[k] for k in sorted(tc_acc.keys())]
        return True, "".join(full_text), ordered_tcs

    async def _non_stream_chat_step(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        model_id: str,
        messages: list[dict[str, Any]],
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "tools": OPENAI_TOOLS_SCHEMA,
            "tool_choice": "auto",
        }
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except Exception as exc:
            return (
                False,
                f"Не удалось подключиться к `{url}`: **{exc}**.\n\n"
                "Проверьте `/connect` или переключитесь на `rbit/smart-local` в `/models`.",
                [],
            )
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
        if user_msg.attachments:
            await on_status(f"Чтение контекста ({len(user_msg.attachments)} файлов)...")
            att_sem = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

            async def _read_att(rel: str) -> tuple[str, ToolResult]:
                async with att_sem:
                    return rel, await invoke_tool("read", {"filePath": rel, "limit": 120})

            att_results = await asyncio.gather(*(_read_att(rel) for rel in user_msg.attachments))
            for rel, res in att_results:
                if res.ok:
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
            return (
                f"Изучил прикреплённый контекст:\n{ctx_block}\n\n"
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
