# RBit Release Readiness Report (v1.6.0-rc1)

Дата: **2026-11-26** · Ветка: `main` · Версия: `1.6.0` · Оценка: **Release Candidate Ready (Conditional Go для v1.6.0-rc1)**

---

## 1. Доказательства готовности по всем приоритетам

| Приоритет | Что проверено и исправлено | Конкретные доказательства | Результат |
|---|---|---|---|
| **1. Windows Application Control** | Добавлен `install_windows_cmd_shim()` и `rbit doctor --fix-shim`, создающий `rbit.cmd` (`python -m rbit %*`) и удаляющий неподписанный `rbit.exe`; добавлена диагностика в `rbit doctor` и инструкция в [docs/RELEASE_CANDIDATE.md](RELEASE_CANDIDATE.md) | `tests/test_soak.py::test_install_windows_cmd_shim`, `rbit --version` → `rbit v1.6.0` (exit 0) | **PASS** *(shim/workaround без отключения ОС-защиты)* |
| **2. Единый учёт реальной стоимости (`AgentEngine`, `rbit stats`, `rbit soak`)** | Вынесен общий хелпер `extract_provider_cost` в `rbit/usage.py`; `AgentEngine._account_step` / `_record_usage` и `AutopilotSoakRunner` оба используют реальный `opencode.list_cost` / `usage.cost` при наличии, иначе `estimate_cost` | `tests/test_soak.py::test_agent_engine_and_usage_ledger_record_provider_cost`, сверка 759/759 ответов (`\$0.954143`) | **PASS** |
| **3. Независимый аудит Sandbox (Windows, Linux, macOS)** | **Windows 11:** AppContainer (`15/15` + `6/6` в `independent_sandbox_audit.py`). **Linux WSL2 Ubuntu-24.04 (`landlock`):** `23/23` (`symlink`, `AF_VSOCK`, `abstract unix sockets`, `ptrace`, `pid namespace`). **macOS arm64 (CI):** Seatbelt `rbit sandbox check` + `test_sandbox_isolation.py` | `rbit sandbox check .`, `wsl_sandbox_check.py` (**23/23**), `independent_sandbox_audit.py` (**6/6**), CI `smoke (macos-latest)` & `smoke (windows-latest)` | **PASS** |
| **4. Расширенные adversarial-тесты Sandbox** | Проверены: MCP-сервер (чтение секрета вне workspace + `1.1.1.1:80`), недоверенный проектный плагин без `rbit trust`, Windows junction (`mklink /J`), вложенный `subprocess`, shell chaining (`OPENAI_API_KEY`/`AWS_SECRET_ACCESS_KEY`), `.git/hooks`, `.rbit/autopilot`, `../` traversal | `tests/test_sandbox_isolation.py` | **40 passed, 1 skipped** *(symlink без Dev Mode)* |
| **5. Autopilot & Resume на длительных и аварийных сценариях** | 126.05-мин soak на внешнем `pallets/click` (`26/28` PASS, `2,543 → 3,369` тестов, `0` false claims); реальный `proc.kill()` на `rbit run` и `rbit soak --resume` (`2/2` PASS); FP/FN тесты детектора `_claims_success` | [docs/SOAK_2H_REPORT.md](SOAK_2H_REPORT.md), `tests/test_autopilot.py` (**6 passed**), `tests/test_soak.py` (**7 passed**) | **PASS** |
| **6. Стабильный CI (Python 3.10–3.12, Linux/macOS/Windows)** | Устранены тайминг-гонки в TUI (`_discover_models`, `_tick_spinner`, `test_streaming_reply_tail_stays_visible`); CI стабильно 5/5 зелёный | GitHub Actions [45879546397](https://github.com/DreyzeDev/rbit/actions/runs/45879546397) (**5/5 PASS**) + повторная проверка на финальном коммите | **PASS** |
| **7. Воспроизводимый Release Candidate** | Подготовлен [docs/RELEASE_CANDIDATE.md](RELEASE_CANDIDATE.md) с командами установки на Windows/Linux/macOS, верификации и списком ограничений | [docs/RELEASE_CANDIDATE.md](RELEASE_CANDIDATE.md) | **READY** |

---

## 2. Оставшиеся ограничения и риски (честно)

1. **Без подписи `.exe` на Windows:** `pip install` всё равно создаёт `rbit.exe`, поэтому в средах с Application Control требуется `rbit doctor --fix-shim` или `python -m rbit` (решается только коммерческой цифровой подписью бинарника).
2. **Linux `ubuntu-latest` CI:** из-за AppArmor-ограничения непривилегированных `userns` в Ubuntu 24.04 CI использует `bwrap` вместо встроенного Landlock-лаунчера (сам Landlock + `seccomp` + `pivot_root` проверены на WSL2 Ubuntu-24.04: `23/23`).
3. **Внешний аудит и публичные бенчмарки:** независимый pentest, `SWE-bench Verified` и `Terminal-Bench` не запускались и остаются открытыми в [docs/ROADMAP.md](ROADMAP.md).
