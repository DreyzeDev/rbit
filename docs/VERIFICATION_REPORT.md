# RBit Verification & Readiness Report

Дата отчёта: **2026-11-26** · Ветка: `main`

---

## 1. Подтверждённые проверки (с доказательствами в тестах и CI)

| Область | Что проверено | Где зафиксировано | Результат |
|---|---|---|---|
| **Live-LLM Autopilot Soak (33.41 мин)** | Непрерывный прогон `rbit soak` на отдельном тестовом репозитории (`taskflow`) с реальной моделью `deepseek-v4.1-flash`: 14 задач, 135 LLM-запросов/ответов, 336 вызовов инструментов, 113 изменений файлов, 73 запуска тестов (рост от `3 failed` до `382 passed`); принудительное исправление ошибок и адаптация плана без вмешательства пользователя | `rbit/soak.py`, `rbit/cli.py` (`rbit soak`, `--trace-log`), `trace.jsonl`, `report.md` | **14/14 verify PASS** *(прим.: в `task-6` зафиксирован 1 LLM-сбой с 0 tool calls, далее циклы 7–14 продолжили работу)* |
| **Real Process Kill + Crash Recovery** | Реальное принудительное завершение (`TerminateProcess` / `proc.kill()`) запущенного `rbit run -a autopilot` (PID 3112, `status="running"`) и последующий `rbit run -c -a autopilot`: обнаружен и исправлен реальный баг (сессия не сохранялась до конца хода), после фикса восстановление завершило все задачи и `pytest` (`3 passed`) | `rbit/cli.py`, `rbit/soak.py`, `tests/test_autopilot.py::test_autopilot_recovers_after_abrupt_process_termination` | **PASS** |
| **Autopilot (24-шаговый цикл + Test-Fix Loop)** | Анализ многомодульного проекта, запуск `pytest`, правка кода, повторный `pytest`, сохранение `status="completed"`, отсутствие бесконечного зацикливания | `tests/test_autopilot.py::test_autopilot_24_step_multi_file_test_fix_verify` | **PASS** |
| **Параллельные субагенты и сохранность файлов** | Атомарное резервирование бюджета (`asyncio.Lock`), блокировка конфликтующих правок одного файла (`Conflict:`) и чистая параллельная правка непересекающихся файлов | `tests/test_autopilot.py::test_concurrent_parallel_subagents_atomic_file_conflict_lock`, `test_parallel_subagents_disjoint_files_succeed_without_corruption`, `tests/test_long_horizon.py` | **PASS** |
| **Завершение фоновых процессов** | Реальное завершение дерева процессов через `_kill_process_tree` при `terminate_background_pid` | `tests/test_long_horizon.py::test_terminate_background_kills_process_tree` | **PASS** |
| **OS-level Sandbox + Adversarial (Windows 11 + Server 2022)** | AppContainer + Job Object: чтение/запись вне workspace, `~/.rbit`, `.rbit/autopilot`, `.git/hooks`, shell chaining (`OPENAI_API_KEY`/`AWS_SECRET_ACCESS_KEY`), path traversal (`../`), hardlink, сеть/loopback, субагенты под `strict` | `rbit sandbox check` (**15/15 PASS**), `tests/test_sandbox_isolation.py` (**36 passed, 1 skipped** локально; `smoke (windows-latest)` в CI) | **PASS** |
| **OS-level Sandbox (macOS arm64)** | Seatbelt (`sandbox-exec`): изоляция `HOME`, `/private/tmp`, `/private/var/folders`, `.rbit`, `.git/hooks`, symlink escape, сеть | `smoke (macos-latest)` в CI (`rbit sandbox check` + `test_sandbox_isolation.py`) | **PASS** |
| **OS-level Sandbox (Linux)** | Built-in Landlock + `pivot_root` + `seccomp` (WSL2 kernel 6.18: **22/22 PASS**); `bwrap` fallback (Ubuntu 24.04 CI: `.rbit` tmpfs, read-only `.git/hooks`) | `test (3.10, 3.11, 3.12)` в CI, `docs/security/sandbox-audit.md` | **PASS** |
| **Non-editable установка и обновление** | `pip install --no-deps .` + `rbit --version` на Linux, Windows, macOS в CI и обновление в изолированном venv на Windows 11 | `.github/workflows/ci.yml` (`45810691347`) | **PASS** |

---

## 2. Честные ограничения и наблюдаемые особенности

1. **Единичный сбой LLM-шага в soak-прогоне (`task-6-error-recovery-and-hardening`):**
   - В ходе 33.41-минутного прогона на задаче `task-6` произошёл 1 сбой ответа LLM (0 вызовов инструментов); проверка `verify_cmd` (`pytest`) при этом прошла, так как тесты от предыдущих задач уже были зелёными, а последующие автономные циклы 7–14 продолжили работу штатно.
2. **Linux `ubuntu-latest` CI:**
   - Из-за AppArmor-ограничения непривилегированных `userns` в Ubuntu 24.04 встроенный Landlock-лаунчер уступает место `bwrap`, где недоступны `seccomp` (`AF_VSOCK`) и `pivot_root`.
3. **Windows без Developer Mode:**
   - Создание symlink непривилегированным пользователем ограничено ОС (1 тест в `tests/test_sandbox_isolation.py` помечен `skipped`).
4. **Внешние аудиты и сьюты:**
   - Независимый внешний penetration test, публичные бенчмарки (`SWE-bench Verified`, `Terminal-Bench`, `Aider Polyglot`), Electron Desktop GUI и Cloud/Team остаются открытыми в [docs/ROADMAP.md](ROADMAP.md).
