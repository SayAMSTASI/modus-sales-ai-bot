# Telegram-пилот sales AI-агента

Рабочий минимальный контур для проверки удобства и токенной экономики до любых доработок Bitrix24. По умолчанию запускается бесплатный `mock`-режим; боевые Telegram, OpenAI и MCP включаются только через секреты и явную конфигурацию.

Основной режим разработки — локальный Telegram long polling. Его можно запустить напрямую с SQLite либо одной Docker-командой с PostgreSQL. Для контура организации предусмотрен отдельный Compose-стек `web + worker + PostgreSQL` за корпоративным TLS reverse proxy. Секреты не входят в образ и репозиторий.

## Локальный запуск Telegram-бота

1. Создать бота через `@BotFather` и получить token.
2. Запустить из PowerShell:

   ```powershell
   .\scripts\local-up.ps1
   ```

3. Вставить token в скрытый запрос скрипта и написать боту `/start` в личном чате.

По умолчанию используется бесплатный `mock`: он позволяет проверить Telegram, доступ, очередь и контекст без OpenAI. Первый пользователь, отправивший `/start`, становится локальным владельцем. Чтобы заранее закрепить конкретный Telegram ID:

```powershell
.\scripts\local-up.ps1 -OwnerTelegramUserId 123456789
```

Для реального OpenAI после получения project API key:

```powershell
.\scripts\local-up.ps1 -OpenAI
```

Для OpenAI вместе с уже полученным OAuth access token Jira MCP:

```powershell
.\scripts\local-up.ps1 -OpenAI -Mcp
```

Скрипт дополнительно запросит токен MCP скрытым вводом. После запуска повторите
`/start`, чтобы локальный владелец получил актуальный allowlist, затем выполните
`/mcp jira покажи задачу SH-501`.

По умолчанию используется модель `gpt-5.6-luna`.

Скрипт отдельно и скрыто запросит Telegram token и OpenAI key. Секреты не записываются в репозиторий или SQLite. Остановка — `Ctrl+C`.

### Контекст, skills и MCP

- контекст хранится в локальной `data/sales_bot.db`: до 12 сообщений, 24 000 символов и 24 часов; `/new` очищает его сразу;
- `config/project/prompt.md` и каждый `config/project/skills/*/SKILL.md` автоматически собираются в инструкции агента при старте;
- MCP-серверы задаются через `MCP_SERVERS_JSON`; к OpenAI передаются только tools, одновременно разрешённые в конфигурации сервера и для локального владельца;
- конфигурация с `read_only=false` отклоняется до запуска.

Реестр MCP хранится без секретов в `config/mcp_servers.json`. В нём уже зафиксированы Jira, Bitrix и KTalk; `allowed_tools` остаются пустыми до OAuth-входа и live discovery.

Принятый сценарий персональной OAuth-авторизации через Telegram описан в [docs/telegram-mcp-oauth.md](docs/telegram-mcp-oauth.md).

Для явной smoke-проверки MCP используйте команду:

```text
/mcp jira покажи задачу SH-501
/mcp ktalk найди запись встречи
/mcp bitrix найди сделку по клиенту
```

Команда ограничивает запрос выбранным MCP-сервером и требует хотя бы один tool call.
Без параметров `/mcp` показывает серверы, для которых утверждён непустой
`allowed_tools`. OpenAI API key не переносит OAuth-подключения из Developer Mode:
локальное приложение должно передавать OAuth access token в каждом Responses API
запросе через `JIRA_MCP_ACCESS_TOKEN`, `BITRIX_MCP_ACCESS_TOKEN` или
`KTALK_MCP_ACCESS_TOKEN`.

Пример временного переопределения через environment:

```powershell
$env:MCP_SERVERS_JSON='[{"server_label":"bitrix","server_description":"Bitrix CRM read-only","server_url":"https://example/mcp","allowed_tools":["get_deal"],"read_only":true}]'
.\scripts\local-up.ps1 -OpenAI
```

Если MCP требует OAuth bearer token, в объекте задаётся `"authorization_env":"BITRIX_MCP_ACCESS_TOKEN"`, а значение — только в переменной текущего процесса. Сохранённое OAuth-подключение ChatGPT/developer mode не считается автоматически доступным локальному API-приложению.

## Что реализовано

- HTTPS webhook с проверкой `X-Telegram-Bot-Api-Secret-Token`, private chat и владельца чата;
- дедупликация `update_id`, очередь в PostgreSQL и ограниченные повторы worker;
- повторная проверка доступа перед OpenAI/MCP и немедленный отзыв без перезапуска;
- состояния `pending`, `active`, `revoked`, защищённая web-админка и аудит;
- `/start`, `/help`, `/new`, только текстовые сообщения;
- краткоживущий контекст, `store=false`, отсутствие текста диалогов в технических метриках;
- Responses API, versioned prompt/skills bundle и read-only allowlist для Bitrix/KTalk MCP;
- токены, latency, модель, результат, обезличенный пользователь, сценарий и расчётная стоимость;
- метрики в PostgreSQL и обезличенная CSV-выгрузка.

Полное решение, оценки и go/no-go критерии: [docs/technical-solution.md](docs/technical-solution.md).

## Локальный запуск в Docker

Требуются Docker Desktop и PowerShell:

```powershell
.\scripts\docker-up.ps1
```

Скрипт создаёт игнорируемый `.env.docker.local` со случайными локальными секретами, скрыто запрашивает Telegram token, поднимает PostgreSQL и контейнер бота в long polling. Без флага используется бесплатный `mock`.

Реальный OpenAI:

```powershell
.\scripts\docker-up.ps1 -OpenAI
```

Запуск с логами в текущем окне:

```powershell
.\scripts\docker-up.ps1 -OpenAI -Foreground
```

Остановка:

```powershell
docker compose --env-file .env.docker.local -f docker-compose.local.yml down
```

## Проверка

```powershell
.\scripts\verify.ps1
```

Команда создаёт `.venv`, устанавливает зависимости, запускает Ruff, тесты критических сценариев и локальный HTTP readiness smoke-test.

## Подключение реальных сервисов

1. На сервере создать закрытый env-файл на основе `.env.example`, заполнить PostgreSQL и секреты и установить `APP_ENV=production`. Сам файл не коммитить.
2. Установить `AGENT_BACKEND=openai`, API key, точную модель и актуальные цены модели.
3. Либо указать опубликованные `OPENAI_PROMPT_ID`/`OPENAI_PROMPT_VERSION`, либо импортировать versioned bundle:

   ```powershell
   .\.venv\Scripts\python.exe .\scripts\import_project_bundle.py --prompt C:\secure\sales-agent\prompt.md --skills-dir C:\secure\sales-agent\skills
   ```

4. После реализации персонального Device Flow выполнить discovery Jira/Bitrix/KTalk MCP под тестовым пользователем, вручную утвердить read-only tool names и заполнить `config/mcp_servers.json`. Write tools кодом запрещены через `read_only=true` и двойной allowlist.
5. Развернуть сервис за корпоративным TLS reverse proxy. Зарегистрировать webhook:

   ```powershell
   $env:TELEGRAM_BOT_TOKEN='...'
   $env:TELEGRAM_WEBHOOK_SECRET='...'
   .\.venv\Scripts\python.exe .\scripts\register_webhook.py --url https://sales-bot.example.ru
   ```

6. Проверить метрики в PostgreSQL и обезличенную CSV-выгрузку; текст запросов в метрики не попадает.

Секреты не должны храниться в Git, Jira, логах или передаваемом файле с вводными.

Полный runbook, сетевые требования и критерии приёмки: [docs/deployment.md](docs/deployment.md).
