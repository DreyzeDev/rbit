# RBit Tools Readiness Audit

Аудит всех 29 инструментов RBit (`rbit/tools.py`, `rbit/browser.py`, `rbit/remote.py`, `rbit/lsp.py`, `rbit/mcp.py`, `rbit/scheduler.py`).

Статусы:
- **Production-Ready** — полная реализация, валидация входа, таймауты, контроль доступа и тесты.
- **Verified (Optional Deps)** — готово при наличии опционального пакета/CLI (`playwright`, `pypdf`, `gh`, `docker`, `lsp`-сервер).

| Инструмент | Назначение | Безопасность и лимиты | Тесты | Статус |
|---|---|---|---|---|
| `read` | Чтение файлов (кодировки, PDF, бинарники) | `resolve_path`, `is_secret_file`, лимит 512 KB | `test_fix_tools.py`, `test_fix_encoding.py` | **Production-Ready** |
| `read_many_files` | Пакетное чтение до нескольких файлов | Ограничение по путям, секрет-фильтр | `test_fix_tools.py` | **Production-Ready** |
| `write` | Атомарная запись файлов + авто-чекпоинт | `plan`/`ask`/`review` блок, авто-диагностика | `test_app.py`, `test_fix_tools.py` | **Production-Ready** |
| `edit` | Точечная правка + fuzzy fallback | Сохранение кодировки и line endings, undo-стек | `test_fix_encoding.py`, `test_fix_tools.py` | **Production-Ready** |
| `multi_edit` | Атомарные множественные правки | Проверка всех замен до записи | `test_fix_tools.py` | **Production-Ready** |
| `apply_patch` | Применение unified diff | Валидация hunk'ов, сохранение кодировки | `test_fix_tools.py` | **Production-Ready** |
| `glob` | Поиск файлов по маске | Уважает `.gitignore`, кэш дерева | `test_fix_tools.py` | **Production-Ready** |
| `grep` | Поиск по содержимому (`rg` + Python fallback) | Encoding-aware для non-ASCII, `.gitignore` | `test_fix_encoding.py`, `test_fix_tools.py` | **Production-Ready** |
| `symbols` | Извлечение символов (Python AST, JS/TS, MD) | Read-only, `resolve_path` | `test_fix_tools.py` | **Production-Ready** |
| `list_changes` | Сводка правок текущей сессии | Read-only | `test_app.py` | **Production-Ready** |
| `notebook` | Чтение/правка `.ipynb` и экспорт | Валидация JSON-структуры | `test_fix_tools.py` | **Production-Ready** |
| `git` | `status`/`diff`/`log`/`show`/`blame`/`bisect` + `issue_*`/`pr_*` | Read-only, `bisect run` запрещён, `gh` в `<untrusted_web_content>` | `test_fix_core.py`, `test_fix_tools.py` | **Production-Ready** (`gh` опционально) |
| `diagnostics` | Проверка синтаксиса (Python/JSON/TOML) | Read-only, авто-запуск после правок | `test_app.py` | **Production-Ready** |
| `lint` | Запуск `ruff` или встроенных проверок | Read-only, sandboxed subprocess | `test_fix_core.py` | **Production-Ready** |
| `lsp` | Диагностика, definition, references, hover | Sandboxed LSP-серверы | `test_lsp.py` | **Verified (Optional Deps)** |
| `bash` | Выполнение команд (foreground + background) | Sandbox, deny-паттерны, approval gate, `_kill_process_tree` | `test_sandbox_isolation.py`, `test_long_horizon.py` | **Production-Ready** |
| `terminal` | Персистентный shell (`PersistentShell`) | Sandbox policy, `envscrub`, `_gate_shell` | `test_terminal.py`, `test_sandbox_isolation.py` | **Production-Ready** |
| `remote` | `docker_ps`, `docker_exec`, `ssh_exec` | Защита от option-injection, `BatchMode=yes`, approval gate | `test_fix_core.py` | **Verified (Optional Deps)** |
| `webfetch` | Загрузка и очистка веб-страниц | SSRF + DNS-rebinding защита, `_fence_untrusted` | `test_fix_tools.py` | **Production-Ready** |
| `websearch` | Поиск в сети | Безопасный HTTP-клиент, очистка результата | `test_websearch.py` | **Production-Ready** |
| `browser` | Playwright-автоматизация браузера | `check_url_safe`, path confinement скриншотов, prompt-injection санитайзер | `test_fix_core.py` | **Verified (Optional Deps)** |
| `todowrite` | Управление планом и задачами сессии | Единый источник правды с сайдбаром | `test_app.py` | **Production-Ready** |
| `subagent` | Делегирование (`explore`, `researcher`, `coder`, `tester`, `reviewer`, `debugger`) | Атомарный бюджет, лимит параллельности, конфликт-гард файлов | `test_long_horizon.py`, `test_sandbox_isolation.py` | **Production-Ready** |
| `question` | Запрос уточнения у пользователя | Интерактивный канал или отказ в headless | `test_app.py` | **Production-Ready** |
| `artifact` | Сохранение структурированных артефактов | Путь строго внутри workspace | `test_blueprint_features.py` | **Production-Ready** |
| `schedule` | Отложенные и периодические задачи | Блокировка мутаций в read-only режиме | `test_schedule.py` | **Production-Ready** |
| `image_generate` | Генерация изображений через API | Валидация пути вывода в workspace | `test_image_generate.py` | **Verified (Optional Deps)** |
| `risk_review` | Оценка риска изменений и команд | Детерминированная классификация | `test_blueprint_features.py` | **Production-Ready** |
| `mcp_resource` | Чтение ресурсов подключённых MCP-серверов | Sandbox для MCP-серверов, `rbit trust` | `test_mcp.py`, `test_sandbox_isolation.py` | **Production-Ready** |
