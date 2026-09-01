# Вводные для запуска MVP

В этот файл нельзя вставлять Telegram token, OpenAI API key, OAuth tokens, passwords, private keys или строки подключения с паролем. Укажите только имя секрета и ответственного за его защищённую передачу.

## Уже принято

- личный Telegram-чат;
- OpenAI Responses API, модель `gpt-5.6-luna`;
- общий набор skills;
- персональная OAuth-авторизация Jira, Bitrix и KTalk MCP через Keycloak;
- администраторы и управление доступом/skills только через Telegram;
- общий read-only MCP allowlist;
- агрегированные метрики без индивидуальных лимитов;
- локальный Docker MVP, затем развёртывание в контуре организации.

## Заполнить до локального live-теста

1. `ADMIN_TELEGRAM_IDS`: числовые Telegram ID администраторов.
   Ответ:
2. `PILOT_TELEGRAM_IDS`: заранее разрешённые Telegram ID, если нужны.
   Ответ:
3. Keycloak `client_id`, issuer, scopes и audience MCP.
   Ответ:
4. Утверждённые read-only tools Jira.
   Ответ:
5. Утверждённые read-only tools Bitrix.
   Ответ:
6. Утверждённые read-only tools KTalk.
   Ответ:
7. 3–5 live-сценариев приёмки, включая минимум один совместный skill + MCP.
   Ответ:

## Секреты, передаваемые отдельно

- `secret_ref=telegram_bot_token`; ответственный:
- `secret_ref=openai_api_key`; ответственный:
- `secret_ref=token_encryption_key`; генерирует приложение/DevOps:

## Заполнить до развёртывания в контуре

1. Linux/Docker host, ответственный DevOps.
   Ответ:
2. DNS и TLS endpoint для Telegram webhook.
   Ответ:
3. Исходящий HTTPS к `api.telegram.org`, `api.openai.com`, `auth.modusbi.ru`, `mcp.modusbi.ru`.
   Ответ:
4. Механизм защищённых переменных: Vault, Docker secrets или CI/CD secrets.
   Ответ:
5. PostgreSQL backup и срок хранения.
   Ответ:
6. OpenAI Project budget/alerts и актуальные тарифы модели.
   Ответ:
7. Срок хранения usage metrics и admin audit.
   Ответ:

Запрос администраторам Keycloak находится в [docs/keycloak-admin-request.md](docs/keycloak-admin-request.md).
