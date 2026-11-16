# rbit для iPhone

Нативный SwiftUI-клиент для **rbit CLI**. Не PWA: это отдельное iOS-приложение
с иконкой на экране «Домой», локальной сетью, стримингом ответов, историей
сессий и подтверждением bash-команд.

Приложение общается с вашим компьютером по локальной Wi-Fi сети через
`rbit mobile` — HTTP/NDJSON API, который уже есть в rbit CLI.

## Что внутри

- **Новый чат** — большой composer в стиле rbit, выбор модели, effort и агента.
- **История** — все сессии из `.rbit/sessions`, поиск, переименование, удаление,
  продолжение по нажатию, индикатор выполняющейся сессии.
- **Чат** — потоковый ответ, reasoning, tool-карточки с аргументами/выводом/diff,
  стоп, разрешения на опасные команды.
- **Настройки** — профиль и аватар, сервер, токен, тема rbit Dark / rbit Light,
  модель/effort/агент по умолчанию.
- **Liquid Glass** — нижний таб-бар использует нативный `TabView`; на iOS 26
  Apple сама рисует liquid glass.
- **Токен** хранится в Keychain, а сервер по умолчанию подтверждает bash-команды
  через приложение.

## Требования

- iPhone с iOS 17 или новее.
- Компьютер и iPhone в одной Wi-Fi сети.
- Установленный rbit CLI последней версии (ветка `main` или v1.3.1+).

## 1. Запустить сервер на ПК

В папке проекта, которым должен управлять агент:

```bash
rbit mobile -d C:\path\to\project
```

Команда:

1. поднимает HTTP-сервер на `0.0.0.0:8333`;
2. создаёт/берёт постоянный токен из `~/.rbit/service.json`;
3. печатает адрес, токен, ссылку `rbit://connect?...` и QR-код
   (QR — если установлен `pip install "rbit[mobile]"`);
4. включает подтверждение bash-команд через iPhone (отключить:
   `--no-approvals`).

Если нужен другой порт:

```bash
rbit mobile -d C:\path\to\project --port 8334
```

> Windows Firewall может спросить доступ к сети. Разрешите Python/Node в
> частной сети. Порт по умолчанию — `8333`.

## 2. Подключить iPhone

1. Соберите приложение (см. ниже) и установите на iPhone.
2. Откройте rbit. При первом запуске откроется экран «Подключить ПК».
3. Вставьте ссылку `rbit://connect?...` из терминала, отсканируйте QR или
   введите адрес и токен вручную.
4. Разрешите локальную сеть iOS.

Токен можно не вводить, если сервер запущен без `RBIT_SERVER_TOKEN` (но для
`rbit mobile` токен включён всегда).

## Сборка

### Вариант A: Xcode на Mac

```bash
open ios/Rbit.xcodeproj
```

В Xcode:

1. Выберите target **Rbit** → Signing & Capabilities.
2. Выберите свою Team (нужен Apple ID; бесплатный аккаунт подходит для
   sideload-сборки).
3. Подключите iPhone и нажмите Run.

Минимальная версия iOS — 17.0. Внешних зависимостей нет.

### Вариант B: без Mac (Windows)

GitHub Actions собирает **неподписанный IPA**:

1. Запушьте репозиторий на GitHub.
2. Откройте **Actions → iOS → Run workflow**.
3. Скачайте артефакт `rbit-unsigned-ipa`.
4. Подпишите и установите IPA через [Sideloadly](https://sideloadly.io/) или
   AltStore с вашим Apple ID.

Бесплатный Apple ID даёт 7 дней подписи; подпись можно обновлять.

> Важно: на Windows нельзя собрать iOS-приложение локально. Xcode-проект
> создан для macOS/CI; IPA из Actions — это уже готовый бинарник приложения,
> который остаётся только подписать.

## Генерация Xcode-проекта

Если добавляете новые `.swift`-файлы:

```bash
cd ios
python generate_project.py
```

Скрипт пересобирает `Rbit.xcodeproj/project.pbxproj` без внешних зависимостей.

## Структура

```
ios/
  Rbit.xcodeproj/
  Rbit/
    RbitApp.swift
    Info.plist
    Models/       Codable-модели rbit API и стрима
    Networking/   RbitClient (URLSession + NDJSON), Keychain
    State/        AppState, настройки
    Utilities/    палитра rbit, форматирование, haptics
    Views/        Home / History / Chat / Settings / подключение / QR
  generate_project.py
```

## API, который использует приложение

- `GET /health`
- `GET /v1/status`
- `GET /v1/models`
- `GET /v1/sessions`
- `POST /v1/sessions`
- `GET /v1/sessions/{id}`
- `POST /v1/sessions/{id}/rename`
- `POST /v1/sessions/{id}/abort`
- `DELETE /v1/sessions/{id}`
- `POST /v1/turns` — NDJSON: `session`, `status`, `thinking`, `reasoning`,
  `delta`, `tool_call`, `tool_result`, `tool_approval`, `message`, `aborted`,
  `error`, `done`
- `POST /v1/approvals`

## Безопасность

- Приложение рассчитано на доверенную домашнюю сеть. Не публикуйте порт 8333 в
  интернет без туннеля и дополнительной защиты.
- Токен хранится в iOS Keychain. Сервер принимает `Authorization: Bearer ...`
  и `X-RBit-Token`.
- ATS разрешает HTTP к локальному IP; это осознанное решение для LAN-клиента.
- По умолчанию `rbit mobile` включает approvals: опасные bash-команды ждут
  подтверждения в iPhone до 180 секунд.
