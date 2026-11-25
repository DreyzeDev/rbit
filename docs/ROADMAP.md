# RBit Public Roadmap

Цель RBit — стать автономным инженерным агентом, которому можно безопасно доверить реальный репозиторий на десятки и сотни шагов: планирование, правка кода, запуск тестов, исправление ошибок, проверка результата и безопасная остановка.

Статусы: **Completed** (готово и покрыто тестами) · **In Progress** (в активной доработке) · **Planned** (в планах).

---

## Приоритеты

| Приоритет | Фокус | Текущий статус |
|---|---|---|
| **P0** | Надёжность Agent Core, Test Gate, Predictive Compaction, восстановление сессий | **Completed / Hardening** |
| **P1** | Завершение аудита Sandbox, macOS verification, независимые security-тесты | **In Progress** |
| **P2** | Model Router, MCP/Skills, браузерный агент, долгие автономные задачи | **In Progress** |
| **P3** | Desktop Control, Cloud/Team, расширенная экосистема и внешние бенчмарки | **Planned** |

---

## 1. Agent Core (P0)

| Направление | Статус | Детали и результаты проверки |
|---|---|---|
| Длинные автономные задачи (`max_steps`, `max_cost_usd`, budget guard) | **Completed** | Многошаговый цикл с лимитами шагов и стоимости, стриминг tool-вызовов и защита бюджета во всех клиентах |
| Test Gate (TUI + headless `rbit run`) | **Completed** | Автозапуск `pytest`/`npm test`/`cargo test` после правок кода + 1 круг авто-фикса (`tests/test_app.py::test_test_gate_feeds_failures_to_fix_round`) |
| Predictive Compaction (до запроса и внутри длинных циклов) | **Completed** | Подсчёт токенов промпта, tool-схем, памяти, diff и вложений; сжатие до 400 overflow и на длинных сериях tool-вызовов (`tests/test_fix_core.py`) |
| Авто-память проекта (`.rbit/memory.md`) | **Completed** | Автоматическая выжимка фактов после хода с дедупликацией (`tests/test_app.py::test_append_project_memory_dedups`) |
| Восстановление сессий после падений и ошибок API | **Completed** | Атомарная запись, восстановление незавершённых сессий при старте TUI, retry на 429/5xx и fallback при 401/404 |
| Локальные embeddings / BM25 для поиска по прошлым сессиям | **Completed** | `StorageManager.search_sessions` (BM25 + опциональные `sentence-transformers` embeddings) и `rbit sessions --search <query>` (`tests/test_fix_core.py`) |

---

## 2. Agent Tools (P2)

| Направление | Статус | Детали и результаты проверки |
|---|---|---|
| Файлы, diff, AST-символы, Git, LSP, форматтеры | **Completed** | `read`/`write`/`edit`/`multi_edit`/`apply_patch`, сохранение кодировок (cp1251/UTF-16/UTF-8), `symbols`, `lsp`, `diagnostics`, `lint` |
| Субагенты (`subagent`, фоновый режим) | **Completed** | Параллельные и фоновые субагенты с передачей результата карточкой |
| Общий бюджет и изоляция прав для нескольких субагентов | **Completed** | Общий лимит `subagent_turn_budget_usd` / `max_subagents_per_turn`, ролевая модель `model_router.roles.subagent` и запрет рекурсивного/мутирующего доступа в read-only режиме (`tests/test_fix_core.py`) |
| Браузерный агент с защитой от prompt injection | **In Progress** | `rbit/browser.py`: SSRF-защита (`check_url_safe`), workspace-ограничение для скриншотов, санитайзер невидимых символов, детектор prompt-injection маркеров и `<untrusted_web_content>` обёртка |
| Docker, SSH и удалённые среды разработки | **Completed** | `rbit/remote.py` и инструмент `remote` (`docker_ps`, `docker_exec`, `ssh_exec`) с защитой от option-injection, очисткой ключей и permission-гейтами (`tests/test_fix_core.py`) |
| Глубокая интеграция GitHub Issues и Pull Requests | **Completed** | `rbit pr` CLI + `git` tool actions (`issue_list`, `issue_view`, `pr_list`, `pr_view`, `pr_diff`) с защитой от prompt injection (`tests/test_fix_core.py`) |

---

## 3. Security и Sandbox (P1)

| Направление | Статус | Детали и результаты проверки |
|---|---|---|
| Windows Sandbox (AppContainer + Job Object) | **Completed** | Верифицировано на Windows 11 (15/15 проверок `rbit sandbox check`, `tests/test_sandbox_isolation.py`) |
| Linux Sandbox (namespaces + Landlock + seccomp + `pivot_root`) | **Completed** | Верифицировано на Linux / WSL2 kernel 6.18 (22/22 проверки; см. `docs/security/sandbox-audit.md`) |
| Trust-модель проектных конфигов (`rbit trust`) и очистка env | **Completed** | Недоверенные `.rbit/config.json` не запускают код; секреты LLM вычищаются через `rbit/envscrub.py` |
| Нативная проверка macOS (`sandbox-exec`) | **Completed** | Профиль `rbit/sandbox/macos.py` изолирует чтение/запись в `HOME`, `/private/tmp`, `/private/var/folders`, `.rbit` и `.git/hooks`; `rbit sandbox check` и `pytest` пройдены на `macos-latest` (`arm64`) в GitHub Actions (`45714289238`) |
| Независимый penetration test и public security report | **In Progress** | Обновлён `SECURITY.md` (responsible disclosure, SLA, scope); аудит `docs/security/sandbox-audit.md` |
| Проверка происхождения и целостности плагинов (provenance + SHA-256) | **Completed** | `install_plugin` и `verify_sources` в `rbit/install.py` фиксируют и проверяют SHA-256 манифестов и удалённые источники (`tests/test_fix_core.py`, `tests/test_fix_install.py`) |

---

## 4. Model Router (P2)

| Направление | Статус | Детали и результаты проверки |
|---|---|---|
| Smart-роутер по сложности, тирам и `never_switch_mid_task` | **Completed** | Активен в `AgentEngine.run_turn` (`rbit/router.py`) |
| Автопереключение при rate limit / outage (429/502/503/504/404) | **Completed** | `failover_candidates` в `rbit/router.py` и автоматический failover в `AgentEngine.run_turn` до fallback на `rbit/smart-local` |
| Разные модели для Plan / Code / Review / Subagents | **Completed** | Поддержка `model_router.roles` (`plan`, `build`, `review`, `subagent`) в `rbit/router.py` (`tests/test_fix_core.py`) |
| Роутинг по цене, скорости, качеству и размеру контекста | **Completed** | Политики `cheapest` / `best_value` / `balanced`, `check_capabilities` и учёт `required_context` подключены в `apply_live_route` и `AgentEngine.run_turn` (`tests/test_fix_core.py`) |
| Точный учёт стоимости всех запросов (включая фоновые) | **In Progress** | Единый `UsageLedger` и `BudgetGuard` (`tests/test_fix_budget.py`) |

---

## 5. Reliability, Testing и Benchmarks (P0–P3)

| Направление | Статус | Детали и результаты проверки |
|---|---|---|
| CI на Windows, Linux и macOS (`pytest` + `ruff` + `mypy`) | **Completed** | `.github/workflows/ci.yml` |
| Интеграционные тесты длинных автономных задач и параллельных агентов | **Completed** | `tests/test_long_horizon.py` (14-шаговый цикл с mid-turn компакцией, параллельные субагенты с общим бюджетом) + `tests/test_fix_core.py`, `tests/test_app.py` |
| Собственный Long-horizon benchmark | **Completed** | Сценарий `long_horizon_12` в `benchmarks/agent_bench.py` + `tests/test_long_horizon.py` |
| Внешние бенчмарки (SWE-bench Verified, Terminal-Bench, Aider Polyglot) | **Planned** | Интеграция публичных сьютов поверх `benchmarks/agent_bench.py` |
| Отдельный Security benchmark для Sandbox | **In Progress** | База в `rbit/sandbox/selftest.py` и `tests/test_sandbox_isolation.py` |

---

## 6. Product и UX (P3)

| Направление | Статус | Детали и результаты проверки |
|---|---|---|
| Полноэкранный TUI + CLI + iOS-клиент | **Completed** | Textual TUI, headless CLI, HTTP/NDJSON `rbit serve`/`mobile`, нативный SwiftUI-клиент (`ios/`) |
| Electron Desktop-клиент (`desktop/`) | **In Progress** | Базовый клиент поверх `rbit serve`; в планах GUI для моделей/MCP/Sandbox, трей и автообновления |
| Cloud / Team / совместная работа | **Planned** | Синхронизация и командные политики |
