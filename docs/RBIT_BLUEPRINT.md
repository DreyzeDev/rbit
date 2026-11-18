# rbit — CLI Feature Blueprint

**Назначение:** единый backlog функций для развития `rbit` в полноценный CLI
coding agent уровня Claude Code / Codex / OpenCode.

> Спецификация для реализации, а не утверждение, что всё уже есть.
> Приоритеты: **P0** — фундамент и безопасность; **P1** — ежедневная
> продуктивность; **P2** — продвинутые возможности; **P3** — преимущества.

Статусы: `[x]` сделано · `[~]` частично · `[ ]` не сделано.

---

## 1. Источники идей

| CLI / проект | Что перенято в rbit |
|---|---|
| **Claude Code** | Агентный цикл, `RBIT.md`-инструкции, skills, hooks, subagents, MCP, разрешения, checkpoints/rewind, headless |
| **OpenAI Codex CLI** | sandbox/approval policies, read-only режим, `exec` для CI, resume, image context, subagents, MCP |
| **Gemini CLI** | расширения, Agent Skills, планирование и todo, web search/fetch, checkpointing, hooks, sandbox, model routing, telemetry |
| **OpenCode** | много провайдеров, TUI, сессии, LSP, MCP, плагины |
| **Aider** | Git-first workflow, автоматические коммиты, repo map, lint/test после изменений |
| **GitHub Copilot CLI** | работа с issue/PR, терминальные команды, объяснение shell-команд |
| **Goose / Cline / OpenHands** | расширяемые инструменты, MCP, профили, длительные задачи, браузер |
| **Amazon Q Developer CLI** | облачные сценарии, помощь с инфраструктурой |

---

## 2. P0 — Фундамент и безопасность

### 2.1 Агентный цикл
- [x] Tool calling на OpenAI-совместимых провайдерах, Anthropic, Gemini, Ollama.
- [x] Цикл выполнения с проверкой результата инструмента.
- [x] Параллельные read-only инструменты с ограничением параллелизма.
- [x] Отмена по `Esc`, прерывание с сохранением частичного ответа.
- [x] Retry с экспоненциальной задержкой на 408/425/429/5xx.
- [x] Авто-компакция контекста при переполнении.
- [x] Лимит шагов и лимит стоимости на один ход.

### 2.2 Инструменты
- [x] Файлы: `read`, `write`, `edit`, `multi_edit`, `apply_patch`, `notebook`.
- [x] Поиск: `glob`, `grep`, `symbols`, `read_many_files`, `list_changes`.
- [x] Shell: `bash`, `terminal`, `!команда` без токенов.
- [x] Диагностика: `diagnostics`, `lint`, `lsp` (definition/references/hover/rename).
- [x] Git: `status`, `diff`, `log`, `show`, `blame`, `branch` + **guarded `bisect`**.
- [x] Web: `webfetch`, `websearch` (несколько провайдеров, DDG fallback).
- [x] Прочее: `todowrite`, `question`, `artifact`, `schedule`, `skill`, `subagent`,
      `image_generate`, `browser`, `risk_review`, `mcp_resource`.

### 2.3 Разрешения и sandbox
- [x] Упорядоченные правила allow/ask/deny с wildcard-паттернами.
- [x] Режимы прав: `ask` / `commands` / `edits` / `auto` (`/permissions`).
- [x] Явный `deny` сильнее любого режима.
- [x] Защита путей вне workspace (`allow_outside_paths` только осознанно).
- [x] Bash sandbox и блокировка опасных команд.
- [x] Запоминание префиксов команд и «разрешить всегда».
- [x] Plan mode физически отключает изменяющие инструменты.

### 2.4 Защита секретов
- [x] Редактирование секретов в выводе инструментов.
- [x] Редактирование секретов в audit-логе (на любой глубине вложенности).
- [x] `.env` и приватные ключи не попадают в контекст автоматически.
- [x] **Защита от prompt injection** во внешнем контенте: фехтование
      `<<<UNTRUSTED_CONTENT>>>`, предупреждение модели, режимы `off|warn|strict`
      (`prompt_injection_guard`).

### 2.5 Сессии и контекст
- [x] Сохранение сессий на диск.
- [x] `resume`, `sessions list`, `sessions search`, `sessions delete`.
- [x] Автоматическое восстановление после аварийного завершения.
- [x] Контекст проекта: дерево файлов, стек, package manager, тесты, Git status.
- [x] **`RBIT.md`** как проектные инструкции; `AGENTS.md` и `CLAUDE.md`
      поддерживаются как совместимые источники (`RBIT.md` приоритетнее).
- [x] Иерархия инструкций: global → project → subdirectory → user prompt.
- [x] **Явно показывать, какие инструкции загружены** (`/status`, `/doctor`).
- [x] Сжатие контекста с сохранением решений, ограничений и задач.
- [x] Не включать секреты и огромные файлы в контекст автоматически.

---

## 3. P1 — Ежедневная работа

### 3.1 Команды и TUI
- [x] Интерактивный TUI с потоковым выводом.
- [x] Подсветка кода, Markdown, diff и ошибок.
- [x] Ввод длинных запросов через внешний редактор (`$EDITOR`).
- [x] История ввода и автодополнение (`/`, `@`).
- [x] Горячие клавиши: остановить, продолжить, свернуть вывод, открыть diff.
- [x] Индикатор модели, режима, папки, контекста, токенов и стоимости.
- [x] Копирование ответа и кода.
- [x] PowerShell, CMD, Bash, Zsh и Fish.
- [x] Темы оформления (включая минималистичную тёмную `rbit-design`).
- [x] Доступный plain mode и `--no-color`.
- [x] Латч-детектор UI: `/doctor` показывает худший стопор event loop.

### 3.2 Slash-команды
- [x] `/help`, `/status`, `/model`, `/provider`, `/permissions`.
- [x] **`/plan`** — план без изменения файлов (принудительный Plan mode).
- [x] **`/execute`** — выполнить согласованный план в Build mode.
- [x] **`/bug`** — воспроизвести, изолировать (bisect), доказать фикс.
- [x] `/init`, `/doctor`, `/cost`, `/usage`, `/diff`, `/review`, `/test`, `/lint`.
- [x] `/checkpoint`, `/rewind`, `/undo`, `/redo`, `/compact`, `/fork`, `/retry`.
- [x] `/commit`, `/search`, `/copy`, `/remember`, `/skill`, `/mcp`, `/sessions`.
- [x] Команда выполняется с первого Enter (не требует второго нажатия).

### 3.3 Plan mode и todo
- [x] Plan mode, todo-менеджер, индикация прогресса в сайдбаре.
- [x] `/plan` → `/execute` как явный двухшаговый workflow.
- [x] Todo сохраняются в сессии и восстанавливаются.

### 3.4 Git review, tests, lint
- [x] Diff-viewer, черновик commit-message, `/commit`.
- [x] Автоопределение тестовой команды и запуск после изменений.
- [x] Self-check: диагностика изменённых файлов + один раунд авто-фикса.
- [x] Lint-интеграция (ruff и другие форматтеры).
- [x] **`git bisect`** как безопасный инструмент (без `bisect run`).

### 3.5 Headless / CI
- [x] `rbit run -p "..."` и `rbit -p "..."`.
- [x] `--output-format text|json|jsonl`.
- [x] Exit codes для CI, работа с пайпами.
- [x] `--auto/--yolo`, бюджетные лимиты.

### 3.6 Web search и документация
- [x] Web search и fetch с несколькими провайдерами.
- [x] Поиск официальной документации, а не блогов.
- [x] Указание источников в выводе.
- [x] Работа офлайн, если web tools недоступны.
- [x] Внешний контент помечается как данные, а не инструкции.

---

## 4. P2 — Экосистема

### 4.1 Skills
- [x] Формат `SKILL.md` с описанием и инструкциями.
- [x] Project skills `.rbit/skills/<name>/SKILL.md`.
- [x] User skills `~/.rbit/skills/<name>/SKILL.md`.
- [x] Автоподбор skill по задаче (например `rbit-design`).
- [x] Явный вызов `/skill`.
- [x] Skills могут содержать templates и reference docs.
- [ ] Публикация/установка skill из Git URL.
- [~] Проверка источника и разрешений skill.

### 4.2 Hooks
- [x] События: `SessionStart`, `BeforeTool`, `AfterTool`, `BeforeEdit`,
      `AfterEdit`, `BeforeCommand`, `AfterCommand`, `OnError`, `OnStop`.
- [x] Hooks в конфиге проекта и пользователя.
- [x] Возможность hook заблокировать действие с объяснением.
- [x] Логирование результатов (audit).
- [ ] Timeout и ограничение ресурсов hooks.
- [ ] Preview/подтверждение hooks перед первым запуском.

### 4.3 MCP и плагины
- [x] MCP stdio и HTTP transports.
- [x] `mcp add/list/remove/test`, показ схем tools.
- [x] Плагины с manifest и разрешениями.
- [ ] MCP OAuth и безопасное хранение credentials.
- [ ] Права на каждый MCP tool: read / write / destructive.
- [ ] Установка/обновление/удаление плагинов из реестра.

### 4.4 Subagents и параллельная работа
- [x] Специализированные агенты (`.rbit/agents/*.md`).
- [x] Read-only `explore` по умолчанию.
- [x] Параллельный запуск независимых subagents.
- [ ] Ограничение бюджета на subagent.

### 4.5 LSP и repo map
- [x] LSP: definition, references, hover, document symbols, rename.
- [x] Автозапуск LSP по типам файлов.
- [x] Repo map: `map`, `symbols`, дерево проекта.
- [~] Семантический поиск по проекту.

---

## 5. P3 — Преимущества rbit

### 5.1 Budget Guard
- [x] Учёт input / output / reasoning / cached токенов по каждому вызову.
- [x] Дневные лимиты (`daily_budget_usd`, `daily_token_limit`), лимиты по модели
      (`model_budgets`).
- [x] Жёсткая остановка **до** платного вызова (`budget_mode: strict`).
- [x] Предупреждения на 50 / 75 / 90 / 100%.
- [x] **Прогноз расхода**: burn-rate и «сколько ещё продержится лимит».
- [x] Цена из каталога тарифов, с возможностью overrides.
- [x] **Не угадывать стоимость**, если usage/тариф неизвестен (`priced: false`).
- [x] **Экспорт usage в JSON/CSV** (`rbit usage export`).
- [x] Защита от параллельных запросов, обходящих общий лимит (reservations).
- [x] `budget strict`: неизвестная цена → предупреждение или запрет.

### 5.2 Smart Model Router
- [x] Маршрутизация по сложности: `fast` / `balanced` / `deep`.
- [x] Отдельная модель на тир (`model_router.tiers`).
- [x] Учёт доступности модели перед переключением.
- [x] **Не менять модель посреди задачи незаметно** (`never_switch_mid_task`).
- [x] **Показывать причину выбора модели** (`RoutingDecision.describe()`).
- [x] Пользователь может закрепить модель или провайдера (`pin`).
- [~] Учёт цены/скорости/контекста при выборе внутри тира.
- [~] Автоматическое переключение при rate limit / outage.
- [ ] Режим «самая дешёвая подходящая модель».
- [ ] Режим «максимальное качество в заданном бюджете».
- [~] Проверка совместимости модели с обязательными функциями агента.

### 5.3 Надёжность и самодиагностика
- [x] `/doctor`: Python, сеть, API, права, Git, shell, конфиги, UI lag.
- [x] Автоматическое восстановление повреждённой сессии.
- [x] Crash recovery с последним checkpoint.
- [x] **Audit log**: кто / что / когда / результат по каждому действию.
- [x] Статистика tool success rate, latency и стоимости (`rbit audit`, `rbit usage`).
- [~] Диагностический пакет с редактированием секретов.
- [ ] Тестовый режим инструментов без реального изменения файлов.
- [ ] Self-test после обновления CLI.
- [~] Миграции конфигурации с резервной копией.

### 5.4 Автоматизация
- [x] Headless mode, JSON output, exit codes, пайпы.
- [x] Batch и watch mode.
- [x] Scheduled tasks с явным расписанием.
- [x] Уведомления, когда агент ждёт подтверждения.
- [ ] Desktop control с безопасностью (отложено осознанно).
- [ ] Cloud / team / automation.

---

## 6. Приоритетный план

### Milestone 1 — Надёжный агент (P0) — **готово**
1. [x] Tool calling на всех провайдерах.
2. [x] Файловые инструменты и shell.
3. [x] Цикл с проверкой результата.
4. [x] Разрешения, sandbox, защита секретов, prompt-injection guard.
5. [x] Сессии, восстановление, отмена.
6. [x] Diff, checkpoint, undo.
7. [x] Тесты на Windows и Linux.

**Критерий готовности:** агент реально читает, меняет и проверяет проект;
не заявляет об успехе, если tool call не выполнен.

### Milestone 2 — Удобство Claude Code уровня (P1) — **готово**
1. [x] TUI, slash-команды, история.
2. [x] `RBIT.md` + совместимость `AGENTS.md`.
3. [x] Plan mode + todo, `/plan` и `/execute`.
4. [x] Git review, tests, lint, `/bug` + bisect.
5. [x] Headless / JSON / CI.
6. [x] Web search и документация.

### Milestone 3 — Экосистема (P2) — **частично**
1. [x] Skills.
2. [~] Hooks (нет timeout/preview).
3. [~] MCP (нет OAuth, прав на tool).
4. [x] Subagents.
5. [x] LSP и repo map.
6. [x] Browser tools и image context.

### Milestone 4 — Преимущества rbit (P3) — **готово по основе**
1. [x] Budget Guard.
2. [x] Smart Model Router.
3. [x] Прогноз расхода и жёсткие квоты.
4. [ ] Desktop control с безопасностью.
5. [ ] Cloud / team / automation.

---

## 7. Архитектурные принципы

- Модульность: `agent`, `tools`, `providers`, `permissions`, `storage`,
  `rules`, `skills`, `plugins`, `mcp`, `lsp`, `usage`, `audit`, `guard`,
  `router`, `ui`.
- Каждый инструмент: schema, permission level, timeout, cancellation, тесты.
- UI не выполняет shell напрямую — только через единый Tool Runtime.
- Все изменения файлов проходят через один Patch Engine.
- Budget Guard стоит перед каждым платным запросом и учитывает конкурентные
  запросы.
- Внешний контент — это данные, а не инструкции.
- Никакой «автономности» ценой безопасности: `deny` всегда сильнее.

---

## 8. CLI-справочник новых команд

```powershell
rbit audit [summary|tail|path|clear] [--day YYYY-MM-DD] [-n 40] [--json]
rbit usage [today|show|forecast|export] [--days 7] [--format json|csv] [--out FILE] [--json]
rbit permissions mode [ask|commands|edits|auto]
```

Конфигурация (`.rbit/config.json`):

```json
{
  "permission_mode": "edits",
  "audit_log": true,
  "prompt_injection_guard": "warn",
  "budget_mode": "warn",
  "daily_budget_usd": 2.0,
  "daily_token_limit": 500000,
  "model_budgets": {"best-model": 0.5},
  "model_router": {
    "enabled": true,
    "never_switch_mid_task": true,
    "tiers": {"fast": "cheap-model", "balanced": "mid-model", "deep": "best-model"}
  }
}
```
