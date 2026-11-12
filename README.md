<div align="center">

<img src="docs/assets/banner.svg" alt="rbit — Next-Gen Terminal AI Coding Agent" width="100%"/>

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Textual](https://img.shields.io/badge/TUI-Textual%208.x-fab283?style=flat-square)](https://textual.textualize.io/)
[![License](https://img.shields.io/badge/License-MIT-7fd88f?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-5c9cf5?style=flat-square)](#)
[![Tests](https://img.shields.io/badge/tests-pytest%2012%20passing-7fd88f?style=flat-square)](tests/test_app.py)

**Автономный полноэкранный AI-агент для терминала** — редактирует код, ищет по проекту, выполняет команды, хранит память проекта и откатывает время без Git.

</div>

---

## Снимки интерфейса

<div align="center">

<table>
<tr>
<td align="center"><sub><b>Home View</b> — центрированный старт</sub></td>
<td align="center"><sub><b>Agent Session</b> — живой diff карточек</sub></td>
<td align="center"><sub><b>Ctrl+M</b> — выбор модели</sub></td>
</tr>
<tr>
<td><img src="docs/screenshots/home.svg" alt="Home View" width="320px"/></td>
<td><img src="docs/screenshots/session.svg" alt="Agent Session" width="320px"/></td>
<td><img src="docs/screenshots/models.svg" alt="Model Selector" width="320px"/></td>
</tr>
</table>

</div>

## Возможности

| | |
|---|---|
| **Полноэкранный TUI** | Тёмный интерфейс на [Textual](https://textual.textualize.io/): Home View, сессии, inline-карточки инструментов с **цветными diff'ами** (зелёные/красные подложки, номера строк) и плоский сайдбар (Context · MCP · LSP · Todo · Files) |
| **Два агента (Tab)** | **Build** — свободно редактирует файлы и запускает команды, **Plan** — только читает, анализирует и составляет план, ничего не меняя |
| **Bang Shell (`!`)** | `!git status`, `!pytest` — команда выполняется мгновенно и **без траты LLM-токенов** |
| **Чекпоинты без Git** | `/checkpoint [имя]` — снимок всего проекта в `.rbit/checkpoints/`, `/rewind` — машина времени в любой момент, даже вне репозитория |
| **Память проекта** | `RBIT.md` + `/remember <правило>` — знания автоматически попадают в системный промпт каждой сессии |
| **Real-time Streaming** | Настоящий SSE-стриминг ответов OpenAI-совместимого API в реальном времени |
| **Модели** | OpenRouter / OpenAI / DeepSeek / любой OpenAI-compatible endpoint + автообнаружение локальной **Ollama** (`Ctrl+M`) |
| **Инструменты** | `read`, `write`, `edit`, `multi_edit` (атомарно), `glob`, `grep`, `diagnostics`, `webfetch`, `bash`, `todowrite`, `subagent(explore)` + undo/redo стек |
| **Виртуальные аттачи** | `@diff`, `@staged`, `@git`, `@tree` — git-diff, staged-изменения, состояние ветки и дерево проекта попадают в контекст без чтения файлов |
| **Команды проекта** | Свои слэш-команды из `.rbit/commands/*.md` с подстановкой `{args}` и автодополнением в палитре |
| **Всё локально** | Сессии, конфиг, память и чекпоинты — в скрытой папке `.rbit/` вашего проекта |

## Установка

```bash
git clone https://github.com/DreyzeDev/rbit.git
cd rbit

python -m venv .venv
.venv\Scripts\pip install -e .        # Windows
pip install -e .                      # Linux / macOS
```

Запуск:

```bash
rbit                                  # TUI по умолчанию
.\rbit.bat                            # Windows-лаунчер
```

## Как пользоваться

Единственный ввод — строка приглашения в нижней части экрана:

```
создай файл calc.py с калькулятором     ← обычный промпт агенту
/review src/app.py                      ← анализ файла
!pytest -q                              ← мгновенный шелл без токенов
@src/main.py найди утечку памяти        ← аттач файла к промпту
@diff почему тест падает                ← git-diff прямо в контекст
@tree + @git где точка входа            ← дерево проекта и состояние ветки
```

Свои команды: положите Markdown-файл в `.rbit/commands/`, например
`.rbit/commands/review.md` — и вызывайте его как `/review <аргументы>`
(`{args}` в шаблоне подставится автоматически).

Поддержка провайдеров через `/connect` (Base URL + API key):
`https://openrouter.ai/api/v1` · `https://api.openai.com/v1` · `http://localhost:11434/v1` · любой OpenAI-подобный.
Без ключа работает встроенный локальный Smart-агент `rbit/smart-local`.

## Горячие клавиши

<div align="center">

| Клавиша | Действие |
|---|---|
| `Tab` | Build ↔ Plan |
| `Ctrl+P` / `Ctrl+K` | Палитра команд |
| `Ctrl+M` | Выбор модели |
| `Ctrl+T` | Темы (live preview) |
| `Ctrl+O` | Менеджер Todo |
| `Ctrl+B` | Показать/скрыть сайдбар |
| `Ctrl+N` | Новая сессия |
| `!` + строка | Мгновенный шелл (без токенов) |
| `Esc` | Прервать агента |
| `Ctrl+Q` | Выход |

</div>

## Слэш-команды

```
/checkpoint [имя]     Снимок проекта (без Git)      /rewind    Откат к снимку
/remember <правило>   Память проекта               /export    Экспорт сессии в Markdown
/todo [задача]        Чеклист                       /undo      Отменить правку файлов
/doctor               Диагностика (API, Ollama...)  /redo      Вернуть отменённое
/model · /theme       Модель и тема                 /diff      Сводка изменений
/init                 Анализ проекта + план         /compact   Сжать контекст
```

## CLI (headless / CI)

```bash
rbit run "исправь flake8-предупреждения"      # выполнить задачу без TUI
rbit run --json "..."                         # структурированный JSON-вывод для CI
rbit doctor                                   # диагностика окружения
rbit stats                                    # аналитика токенов и расходов
rbit sessions                                 # список сохранённых сессий
rbit export -o report.md                      # выгрузить переписку в Markdown
```

## Тесты

```bash
.venv\Scripts\pytest -v
```

Headless-тесты прогоняют настоящий TUI через Textual Pilot: Home→Session переходы,
Todo-лист, diff-карточки, undo/redo, чекпоинты, память проекта, модалки и CLI-подкоманды.

## Структура проекта

```
rbit/
├── app.py         # Полноэкранное TUI-приложение (Home / Session / Bang Shell)
├── cli.py         # CLI: run · doctor · stats · sessions · export
├── agent.py       # Движок: SSE-стриминг, Ollama discovery, Smart-агент
├── tools.py       # Инструменты агента + чекпоинты + undo/redo
├── storage.py     # Сессии, конфиг, память, чекпоинты, экспорт
├── widgets.py     # Карточки, логотип, сайдбар, автодополнение
├── modals.py      # Командная палитра, модели, темы, чекпоинты, Todo
├── models.py      # Структуры данных (Session, ChatMessage, ToolCall...)
├── themes.py      # Темы оформления
└── __main__.py    # python -m rbit
```

## Дорожная карта

Актуальный приоритизированный бэклог — в [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md).

- [x] Real-time SSE-стриминг
- [x] Чекпоинты без Git + `/rewind`
- [x] Проектная память (`RBIT.md`, `/remember`)
- [x] Параллельные read-only инструменты и авто-диагностика
- [x] Секрет-гард и авто-чекпоинты перед правками
- [x] Виртуальные аттачи `@diff`/`@git`/`@staged`/`@tree` и команды проекта
- [x] Суб-агент `subagent(explore)`
- [ ] AST-индекс и symbol-index
- [ ] `rbit map` и `rbit lint` в CLI

## Лицензия

[MIT](LICENSE) © [DreyzeDev](https://github.com/DreyzeDev)

---

<div align="center">
<sub>Сделано на <a href="https://textual.textualize.io/">Textual</a> · <a href="https://github.com/DreyzeDev/rbit">поставь звезду, если пригодилось</a></sub>
</div>
