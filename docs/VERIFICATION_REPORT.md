# RBit Verification & Readiness Report

Дата отчёта: **2026-11-26** · Ветка: `main`

---

## 1. Подтверждённые проверки (с доказательствами в тестах и CI)

| Область | Что проверено | Где зафиксировано | Результат |
|---|---|---|---|
| **Autopilot (24-шаговый цикл + Test-Fix Loop)** | Анализ многомодульного проекта (`pkg/calc.py`, `pkg/fmt.py`), чтение, запуск `pytest` (воспроизведение падения), правка кода, повторный `pytest` (прохождение), сохранение `status="completed"`, отсутствие бесконечного зацикливания | `tests/test_autopilot.py::test_autopilot_24_step_multi_file_test_fix_verify` | **PASS** |
| **Autopilot Crash & Restart Recovery** | Имитация аварийного завершения (`finished=False`, `status="running"` на диске), рестарт `StorageManager` + `AgentEngine`, инъекция `[Autopilot Resume Context]` с выполненными и оставшимися `todos` без повторения завершённых шагов | `tests/test_autopilot.py::test_autopilot_recovers_after_abrupt_process_termination`, `test_autopilot_persists_paused_state_and_resumes_after_restart` | **PASS** |
| **Параллельные субагенты и сохранность файлов** | Атомарное резервирование бюджета (`asyncio.Lock`), блокировка конфликтующих правок одного файла (`Conflict:`) и чистая параллельная правка непересекающихся файлов (`f1.py`..`f3.py`, `shared.py`) | `tests/test_autopilot.py::test_concurrent_parallel_subagents_atomic_file_conflict_lock`, `test_parallel_subagents_disjoint_files_succeed_without_corruption`, `tests/test_long_horizon.py` | **PASS** |
| **Завершение фоновых процессов** | Реальное завершение дерева процессов через `_kill_process_tree` при `terminate_background_pid` | `tests/test_long_horizon.py::test_terminate_background_kills_process_tree` | **PASS** |
| **OS-level Sandbox (Windows 11 + Server 2022)** | AppContainer + Job Object: чтение/запись вне workspace, `~/.rbit`, `.rbit`, `.git/hooks`, сеть/loopback, env allowlist, hardlink, субагенты под `strict` | `rbit sandbox check` (**15/15 PASS**), `tests/test_sandbox_isolation.py` (**33 passed, 1 skipped** локально; зелёный `smoke (windows-latest)` в CI) | **PASS** |
| **OS-level Sandbox (macOS arm64)** | Seatbelt (`sandbox-exec`): изоляция `HOME`, `/private/tmp`, `/private/var/folders`, `.rbit`, `.git/hooks`, symlink escape, сеть | `smoke (macos-latest)` в CI (`rbit sandbox check` + `test_sandbox_isolation.py`) | **PASS** |
| **OS-level Sandbox (Linux)** | Built-in Landlock + `pivot_root` + `seccomp` (WSL2 kernel 6.18: **22/22 PASS**); `bwrap` fallback (Ubuntu 24.04 CI: `.rbit` tmpfs, read-only `.git/hooks`) | `test (3.10, 3.11, 3.12)` в CI (`835+ passed`), `docs/security/sandbox-audit.md` | **PASS** |
| **Non-editable установка и обновление** | `pip install --no-deps .` + `rbit --version` на Linux, Windows, macOS в CI и обновление в изолированном venv на Windows 11 | `.github/workflows/ci.yml` (`45808421378`) | **PASS** |

---

## 2. Честные ограничения и что НЕ запускалось

1. **Длительные (30–60 минут) живые LLM-сессии на реальных внешних репозиториях:**
   - В текущем контуре без внешнего API-ключа/квоты провайдера **не проводился** 30–60-минутный прогон против живого облачного LLM; многошаговые сценарии (14–24 шага, сбой/рестарт, Test-Fix Loop, параллельные субагенты) проверены с реальными инструментами/файлами/`pytest` и скриптованными ответами модели.
2. **Linux `ubuntu-latest` CI:**
   - Из-за AppArmor-ограничения непривилегированных `userns` в Ubuntu 24.04 встроенный Landlock-лаунчер уступает место `bwrap`, где недоступны `seccomp` (`AF_VSOCK`) и `pivot_root`.
3. **Windows без Developer Mode:**
   - Создание symlink непривилегированным пользователем ограничено ОС (1 тест помечен `skipped`).
4. **Внешние аудиты и сьюты:**
   - Независимый внешний penetration test, публичные бенчмарки (`SWE-bench Verified`, `Terminal-Bench`, `Aider Polyglot`), Electron Desktop GUI и Cloud/Team остаются открытыми в [docs/ROADMAP.md](ROADMAP.md).
