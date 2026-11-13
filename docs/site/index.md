# rbit

**rbit** — автономный полноэкранный AI-агент, который живёт в терминале. Он читает и
правит файлы, ищет по проекту, запускает команды и умеет откатывать собственные
изменения — даже если проект не под Git.

## Что внутри

- **Полноэкранный TUI** на [Textual](https://textual.textualize.io/): сессии,
  inline-карточки инструментов с цветными diff'ами и плоский сайдбар
  (Context · MCP · LSP · Todo · Files).
- **Два режима агента** (`Tab`): *Build* свободно редактирует файлы, *Plan* — только
  читает и составляет план.
- **Инструменты**: `read`, `write`, `edit`, `multi_edit`, `glob`, `grep`, `symbols`,
  `git`, `diagnostics`, `lint`, `webfetch`, `bash`, `todowrite`, суб-агент `explore`.
- **Чекпоинты без Git**: `/checkpoint` и `/rewind` — снимок всего проекта в `.rbit/`.
- **Память проекта**: `RBIT.md` и `/remember` автоматически попадают в системный промпт.
- **Виртуальные аттачи**: `@diff`, `@staged`, `@git`, `@tree` — контекст без чтения файлов.
- **Headless-режим**: `rbit run` для скриптов и CI, `--output-format jsonl` —
  событийный поток для автоматизаций.

## Установка

```bash
git clone https://github.com/DreyzeDev/rbit.git
cd rbit
pip install -e .
```

Затем просто наберите `rbit` из любого каталога. Для изолированной установки —
`pipx install .`.

## Быстрый старт

Единственный ввод — строка приглашения внизу экрана:

```text
создай файл calc.py с калькулятором   ← обычный промпт агенту
!pytest -q                            ← мгновенный шелл без траты токенов
@diff почему тест падает              ← git-diff прямо в контекст
/review src/app.py                    ← своя слэш-команда из .rbit/commands/
```

Провайдер настраивается через `/connect` (OpenRouter, OpenAI, DeepSeek, локальная
Ollama или любой OpenAI-совместимый endpoint). Без ключа работает встроенный
локальный Smart-агент.

## Что дальше

- [Рецепты](recipes.md) — пять практических сценариев: от починки падающего теста
  до автоматизации в CI.

## Ссылки

- [README](https://github.com/DreyzeDev/rbit#readme) — полная таблица возможностей
  и горячие клавиши.
- [CONTRIBUTING.md](https://github.com/DreyzeDev/rbit/blob/main/CONTRIBUTING.md) —
  как собрать окружение и прислать PR.
- Лицензия [MIT](https://github.com/DreyzeDev/rbit/blob/main/LICENSE).
