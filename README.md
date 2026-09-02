# Modus Sales Telegram Bot — MVP

Локальный Telegram-бот с OpenAI Responses API, общим набором skills и персональным доступом к Jira, Bitrix и KTalk MCP через Keycloak. Web-админки, ролей, индивидуальных tool-наборов и пользовательских лимитов в MVP нет.

## Что реализовано

- private Telegram chat и команды `/start`, `/help`, `/new`;
- администраторы из `ADMIN_TELEGRAM_IDS`;
- запрос доступа с Telegram-кнопками «Разрешить» и «Отклонить»;
- отзыв доступа через `/revoke <telegram_id>` без перезапуска;
- `/login` и `/logout` через Keycloak Device Flow либо Authorization Code + PKCE;
- `/mcp_status` с фактическим состоянием OAuth и allowlist;
- `/mcp_discover <сервер>` для безопасного чтения каталога tools администратором;
- зашифрованное хранение персональных access/refresh token в БД и автоматический refresh;
- передача токена текущего пользователя в `authorization` remote MCP tool;
- единый read-only allowlist tools для Jira, Bitrix и KTalk;
- отдельный контекст каждого пользователя, `/new` удаляет его;
- общий Git-набор skills и версии из БД без перезапуска;
- `/skills`, `/skill_show`, `/skill_edit`, `/skill_cancel`, `/skill_rollback` для администраторов;
- агрегированные tokens/cost/latency/error metrics без полного текста диалога;
- SQLite для прямого локального запуска и PostgreSQL в Docker.

## MCP в текущей версии

Live discovery через персональный Keycloak OAuth выполнен для всех трёх серверов. В [config/mcp_servers.json](config/mcp_servers.json) включены только опубликованные read-only tools: Jira — 11, Bitrix — 13, KTalk — 5. Каталог можно перепроверить без вызова бизнес-операций командой `/mcp_discover jira|bitrix|ktalk` или скриптом `scripts/discover_mcp_tools.py`.

## Прямой локальный запуск одной командой

Требуются Python 3.11+ и PowerShell. При первом запуске команда один раз запросит Telegram token, OpenAI key и Telegram ID администратора:

```powershell
.\scripts\local-start.ps1
```

На Windows ту же команду можно запустить двойным кликом по `START_BOT.cmd` в корне проекта.

Секреты сохраняются в игнорируемом `data/local-secrets.clixml` через Windows DPAPI и доступны только текущей учётной записи Windows. Все следующие запуски выполняются той же командой без повторного ввода. Для замены ключей:

```powershell
.\scripts\local-start.ps1 -Configure
```

Числовой Telegram ID и Keycloak client можно задать сразу параметрами:

```powershell
.\scripts\local-start.ps1 -AdminTelegramId 123456789 -KeycloakClientId 'modus-sales-telegram-local'
```

Постоянный локальный `TOKEN_ENCRYPTION_KEY` автоматически создаётся в игнорируемом `data/token-encryption.key`, поэтому OAuth-сессии переживают перезапуск.

## Локальный запуск в Docker

```powershell
.\scripts\docker-up.ps1 -OpenAI -OwnerTelegramUserId 123456789
```

С Keycloak:

```powershell
.\scripts\docker-up.ps1 -OpenAI -OwnerTelegramUserId 123456789 -KeycloakClientId 'modus-sales-telegram-local'
```

Первый запуск создаёт игнорируемый `.env.docker.local`, запрашивает ключи и генерирует PostgreSQL password, webhook/safety secrets и постоянный Fernet key. Следующий `docker-up.ps1` использует сохранённые значения без вопросов. Для ротации передайте `-Configure -OpenAI`.

Остановка:

```powershell
docker compose --env-file .env.docker.local -f docker-compose.local.yml down
```

## Команды бота

- `/start` — запросить или проверить доступ;
- `/help` — показать помощь;
- `/new` — удалить контекст диалога;
- `/login` — начать настроенный корпоративный OAuth flow;
- `/logout` — отозвать и удалить OAuth-токены;
- `/mcp_status` — показать фактически подключённые серверы и разрешённые tools;
- `/mcp_discover bitrix` — получить текущий каталог Bitrix tools без их вызова, только администратор;
- `/mcp jira покажи SH-501` — принудительно вызвать выбранный MCP;
- `/skills` — выбрать skill, только администратор;
- `/skill_show`, `/skill_edit`, `/skill_cancel`, `/skill_rollback` — управление выбранным skill;
- `/revoke 123456789` — отозвать доступ пользователя.

Обычный запрос может использовать подключённые MCP автоматически. Ошибка или отсутствие OAuth не блокирует обычный ответ без MCP. Команда `/mcp` требует `/login` и хотя бы один разрешённый tool выбранного сервера.

## Конфигурация

Безопасный шаблон: [.env.example](.env.example). Основные значения:

- `ADMIN_TELEGRAM_IDS` — Telegram ID администраторов через запятую;
- `PILOT_TELEGRAM_IDS` — заранее разрешённые пользователи;
- `KEYCLOAK_ISSUER`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_SCOPES`;
- `KEYCLOAK_RESOURCE=https://mcp.modusbi.ru`;
- `KEYCLOAK_FLOW=device` локально или `authorization_code` после HTTPS-развёртывания;
- `TOKEN_ENCRYPTION_KEY` — постоянный Fernet key вне Git и БД;
- `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`;
- `MCP_SERVERS_FILE=config/mcp_servers.json`.

Все три MCP получают персональный Keycloak token текущего Telegram-пользователя. Статические MCP-токены из environment больше не используются.

Для `authorization_code` Keycloak должен разрешать точный redirect URI
`<PUBLIC_BASE_URL>/oauth/callback` и требовать PKCE `S256`. Бот генерирует собственные
`state`, verifier/challenge, не принимает `id_token_hint` извне и делает OAuth state
одноразовым.

После заполнения env DevOps может проверить публичные OAuth metadata без вывода
секретов:

```powershell
.\.venv\Scripts\python.exe .\scripts\check_keycloak_config.py
```

## Проверка

```powershell
.\scripts\verify.ps1
```

Проверка устанавливает зависимости, запускает Ruff, тесты критических сценариев, secret scan и HTTP readiness smoke.

Для production-развёртывания используйте [инструкцию DevOps](docs/devops-keycloak-deployment-runbook.md) и [общий deployment runbook](docs/deployment.md). Секреты запрещено хранить в Git, Jira и логах.

На Linux-сервере схема БД накатывается отдельным одноразовым сервисом `migrate` до запуска web/worker. Проверенный входной сценарий:

```bash
./scripts/server-up.sh /opt/modus-sales-bot/.env.production
```
