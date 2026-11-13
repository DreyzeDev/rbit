# rbit desktop

Electron-обёртка над `rbit serve` — GUI-клиент терминального агента rbit
(аналог десктоп-клиентов opencode).

## Как это работает

Electron запускает локальный бэкенд `python -m rbit.cli serve` (HTTP + NDJSON
стриминг) на `127.0.0.1:8337` и открывает чат-окно: сессии, стриминг ответа,
thinking-индикатор, карточки tool-вызовов. Сессии сохраняются штатным
хранилищем rbit (`.rbit/sessions`).

Требования: Python 3.10+ и установленный rbit (`pip install -e ..` в корне
репозитория).

## Запуск в dev-режиме

```bash
cd desktop
npm install
npm start
```

Переменные окружения:
- `RBIT_DESKTOP_PORT` — порт бэкенда (по умолчанию 8337)
- `RBIT_DESKTOP_WORKSPACE` — воркспейс (по умолчанию домашняя папка)

## Сборка инсталляторов

```bash
cd desktop
npm install
npm run dist
```

Артефакты появятся в `rbit-dist/` (NSIS/portable для Windows, dmg для macOS,
AppImage для Linux). На CI сборка идёт через workflow
`.github/workflows/desktop.yml` при пуше тега `v*`.

> Примечание: если `npm start` не находит бинарник Electron (пустой
> `node_modules/electron/dist`) — корпоративная политика `allow-scripts`
> заблокировала postinstall. Выполните `npm approve-scripts electron` и
> `node node_modules/electron/install.js`, либо собирайте через CI
> (`.github/workflows/desktop.yml` соберёт инсталляторы для Windows/macOS/Linux
> при пуше тега `v*`).
