# RBit Release Readiness Report

Дата: **2026-11-26** · Ветка: `main` · Версия: `1.6.0` · Итоговая оценка: **Conditional Go (готов к ежедневному локальному и CLI/Autopilot использованию с оговорёнными ограничениями ниже)**

---

## 1. Что готово и подтверждено тестами

| Подсистема | Подтверждённое состояние | Доказательства (команды и результаты) |
|---|---|---|
| **Общая тестовая база и линтер** | Полный прогон ~862 тестов выявил 1 гонку таймера в `_tick_spinner` (`PromptSubHints` `NoMatches`) — исправлена; `ruff` без фатальных ошибок (`E9,F63,F7,F82`) | `python -m pytest -q`: `851 passed, 10 skipped` (+ исправлен и перепроверен `test_rewind_and_continue_are_refused_while_busy`); `ruff check rbit tests --select E9,F63,F7,F82` → `All checks passed!` |
| **CI на Python 3.10–3.12 и 3 ОС** | Стабильный 5/5 зелёный прогон на Linux (`3.10`, `3.11`, `3.12`), macOS arm64 и Windows Server 2022 | GitHub Actions [45875565871](https://github.com/DreyzeDev/rbit/actions/runs/45875565871): **5/5 PASS** |
| **Autopilot & Длительная автономность** | Подтверждена 126.05-минутная непрерывная работа на незнакомом внешнем репозитории `pallets/click` (`08b803c`, `deepseek-v4.1-flash`): `26/28` PASS (`92.9%`), `26/27` инженерных (`96.3%`), тесты выросли с `2,543` до `3,369 passed` | [docs/SOAK_2H_REPORT.md](SOAK_2H_REPORT.md), `python -m rbit soak` |
| **Восстановление после прерывания (Crash / Kill / `--resume`)** | Сохранение сессии до старта хода, `.rbit/autopilot/<id>.json`, `checkpoint.json` и `--resume` проверены при реальном `proc.kill()` посреди задачи | `run_crash_recovery_live.py`, `run_soak_interrupt_resume.py` (`2/2`, `\$0.01353`), `tests/test_soak.py` (**5 passed**), `tests/test_autopilot.py` (**6 passed**) |
| **Учёт токенов и стоимости** | По `759/759` ответам `usage` провайдера токены совпадают 1:1 (`75,730,593` in / `707,717` out). В `rbit/soak.py` добавлен приоритет `opencode.list_cost` / `usage.cost` (`\$0.954143` вместо `\$229.31` по каталогу) | `rbit/soak.py`, `rbit/usage.py`, `tests/test_soak.py` |
| **Честность завершения (`false_success_claims`)** | Детектор `_claims_success` + `status="incomplete"` при открытых `todos` / `Loop guard` / `Max steps`; `0` ложных `SUCCESS` в 126-мин прогоне + FP/FN юнит-тесты | `tests/test_soak.py::test_truthfulness_detector_avoids_false_positives_and_false_negatives` |
| **OS-level Sandbox + Adversarial** | Windows AppContainer + Job Object (`15/15`), macOS Seatbelt, Linux Landlock/bwrap: блокировка чтения вне workspace, утечки `OPENAI_API_KEY`/`GITHUB_TOKEN`/`AWS_SECRET_ACCESS_KEY`, перезаписи `.git/hooks` и `.rbit/autopilot`, `../` traversal, Windows junction (`mklink /J`), вложенных `subprocess`, исходящей сети (`1.1.1.1:80`) | `rbit sandbox check .` (**15/15**), `independent_sandbox_audit.py` (**6/6**), `tests/test_sandbox_isolation.py` |
| **CLI, установка, обновление, TUI, MCP/LSP** | `rbit --version`, `rbit sandbox policy --json`, `rbit mcp list`, `rbit lint --json`, non-editable `pip install --no-deps .` в CI на 3 ОС; исправлены перенос ответа в конец `#chat-scroll`, `_discover_models` shutdown race и `_tick_spinner` | `rbit/app.py`, `rbit/cli.py`, `rbit/storage.py` |

---

## 2. Что не идеально / сломано / ограничено

1. **Windows Application Control (`rbit.exe`):**
   - Стандартный `pip`-генератор создаёт неподписанный `Scripts\rbit.exe`, который на системах со строгим Application Control блокируется ОС (`An Application Control policy has blocked this file`). Локальный обход без отключения защиты — запуск через `python -m rbit` или `.cmd`-обёртку (`rbit.cmd`).
2. **Расчёт стоимости в `rbit stats` (`UsageLedger` vs `rbit soak`):**
   - `rbit/soak.py` уже учитывает реальный `opencode.list_cost` / `usage.cost` провайдера, но основной `AgentEngine._record_usage` в `rbit/agent.py` для некаталожных моделей по-прежнему записывает `cost=0.0, priced=False` (соблюдая правило «не выдумывать цену»), если модель не задана в `price_overrides` или статическом каталоге.
3. **Некаталожный `mypy` локально на Windows и `ruff` non-fatal lint:**
   - Локально на Windows `mypy` не установлен (проверяется в Linux CI); `rbit lint --json .` находит `384` некритичных замечания стиля/рефакторинга (при `0` фатальных ошибок `E9,F63,F7,F82`).
4. **Linux `ubuntu-latest` CI:**
   - Из-за ограничений AppArmor на непривилегированные `userns` в Ubuntu 24.04 встроенный Landlock-лаунчер уступает место `bwrap`, где недоступны `seccomp` (`AF_VSOCK`) и `pivot_root`.
5. **Windows без Developer Mode:**
   - 1 тест на создание `os.symlink` пропускается ОС (`skipped`), при этом Windows junction (`mklink /J`) проверен и заблокирован.

---

## 3. Оставшиеся риски и блокеры стабильного релиза (v1.6.0 GA)

- **Блокер 1 (Windows UX при `pip install`):** пользователям Windows с Application Control / Smart App Control `pip install rbit` создаст блокируемый `rbit.exe`; для публичного релиза нужен либо подписанный бинарник / winget-пакет, либо явная документация `python -m rbit`.
- **Блокер 2 (внешний pentest и `swe-bench` / `terminal-bench`):** независимый аудит безопасности и публичные бенчмарки ещё не проведены (зафиксировано в [docs/ROADMAP.md](ROADMAP.md)).
- **Риск (лимит `max_steps=50`):** на очень крупных рефакторингах в одном ходе Autopilot может упереться в `50` шагов (как в `soak-cycle-9`), после чего требуется следующий цикл или `--continue`.

---

## 4. Воспроизводимые команды проверки

```powershell
# Полный локальный прогон
ruff check rbit tests --select E9,F63,F7,F82
python -m pytest -q

# Независимая проверка Sandbox
rbit sandbox check .
rbit sandbox policy --json .
python independent_sandbox_audit.py

# Длительный soak и восстановление
python -m rbit soak <repo> --tasks <tasks.json> --duration-minutes 125 --log-dir <log_dir> --json
python -m rbit soak <repo> --tasks <tasks.json> --log-dir <log_dir> --resume --json
```
