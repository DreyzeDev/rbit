# RBit v1.6.0-rc1 — Release Candidate Guide

Дата: **2026-11-26** · Ветка: `main`

---

## 1. Установка и обновление

### Linux / macOS
```bash
git clone https://github.com/DreyzeDev/rbit.git
cd rbit
python -m pip install --no-deps .
rbit --version
```

### Windows (включая среды с Application Control / Smart App Control)
Стандартный `pip install` создаёт неподписанный `Scripts\rbit.exe`, который может блокироваться политикой Application Control (`An Application Control policy has blocked this file`). Рекомендуемый порядок:
```powershell
git clone https://github.com/DreyzeDev/rbit.git
cd rbit
python -m pip install --no-deps .

# Вариант A: безопасный запуск через подписанный python.exe
python -m rbit --version

# Вариант B: создать rbit.cmd и убрать блокируемый rbit.exe
python -m rbit doctor --fix-shim
rbit --version
```

---

## 2. Воспроизводимая проверка после установки

```powershell
# 1. Диагностика окружения, провайдера и Windows-лаунчера
python -m rbit doctor .

# 2. Независимая проверка OS-level Sandbox
python -m rbit sandbox check .
python -m rbit sandbox policy --json .

# 3. Автотесты Sandbox, Autopilot, Soak и TUI
ruff check rbit tests --select E9,F63,F7,F82
python -m pytest tests/test_soak.py tests/test_autopilot.py tests/test_sandbox_isolation.py tests/test_ui_responsiveness.py -q
```

---

## 3. Известные ограничения RC1

1. **Подпись Windows `.exe`:** неподписанный `rbit.exe` от `pip` по-прежнему блокируется строгим Application Control, поэтому для команды `rbit` в PATH используется `rbit doctor --fix-shim` (`rbit.cmd`) или `python -m rbit`.
2. **Linux `ubuntu-latest` CI (Ubuntu 24.04 AppArmor):** непривилегированные `userns` ограничены, поэтому в CI используется `bwrap` fallback (без `seccomp` `AF_VSOCK` и `pivot_root`); нативный `Landlock + pivot_root + seccomp` подтверждён на WSL2 Ubuntu-24.04 (`23/23 PASS`).
3. **Windows без Developer Mode:** создание `os.symlink` недоступно непривилегированному пользователю (`1` тест в `tests/test_sandbox_isolation.py` помечен `skipped`); Windows junction (`mklink /J`) проверен и заблокирован.
4. **Непроверенные внешние сьюты:** независимый внешний pentest, `SWE-bench Verified`, `Terminal-Bench` и Electron Desktop GUI остаются в [docs/ROADMAP.md](ROADMAP.md).
