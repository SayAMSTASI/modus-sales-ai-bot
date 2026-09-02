# Инструкция DevOps: Keycloak и развёртывание Modus Sales Telegram Bot

## 1. Результат работ

После выполнения инструкции должны работать:

- Telegram webhook `https://<домен>/telegram/webhook`;
- OAuth callback `https://<домен>/oauth/callback`;
- public OIDC client Keycloak с Authorization Code + PKCE S256;
- персональный access/refresh token пользователя для `https://mcp.modusbi.ru`;
- PostgreSQL, Alembic migration, web и worker в Docker Compose;
- Jira, Bitrix и KTalk MCP с токеном вошедшего пользователя и read-only allowlist;
- `/logout` и отзыв доступа администратором.

Пароли, API keys, bot token, OAuth tokens и ключ шифрования нельзя передавать через Git, Jira, Telegram или обычные сообщения.

## 2. Что требуется получить заранее

- домен, например `sales-bot.modusbi.ru`;
- DNS-запись на сервер;
- TLS-сертификат;
- Telegram bot token;
- OpenAI Project API key;
- числовые Telegram ID администраторов;
- сервер Linux с Docker Engine и Compose plugin;
- исходящий HTTPS к:
  - `api.telegram.org`;
  - `api.openai.com`;
  - `auth.modusbi.ru`;
  - `mcp.modusbi.ru`.

## 3. Настройка Keycloak

Realm: `master`.

Создать отдельный клиент:

```text
Client ID: modus-sales-telegram-bot
Client type: OpenID Connect
Client authentication: OFF
Standard Flow: ON
PKCE method: S256
OAuth 2.0 Device Authorization Grant: ON
Implicit Flow: OFF
Direct Access Grants: OFF
Service Accounts: OFF
Authorization Services: OFF
```

Настроить scopes:

```text
openid profile email offline_access
```

Настроить точный Valid Redirect URI без wildcard:

```text
https://<домен>/oauth/callback
```

Web Origins не требуются: browser-side token exchange отсутствует.

Access token должен предназначаться ресурсу:

```text
https://mcp.modusbi.ru
```

Если audience не появляется автоматически, добавить audience mapper для `https://mcp.modusbi.ru`. В токене должны сохраняться идентификатор пользователя и claims, необходимые MCP для применения его корпоративных прав.

Разрешить:

- выдачу refresh token для public client;
- scope `offline_access`;
- обновление токена без `client_secret`;
- revocation access/refresh token.

Не использовать клиент `chatgpt-mcp`: его callback и PKCE-сессии принадлежат ChatGPT. Client secret для нового клиента создавать не нужно.

## 4. DNS, TLS и reverse proxy

Опубликовать web-контейнер только через HTTPS reverse proxy. Внутренний порт Compose по умолчанию слушает `127.0.0.1:8000`.

Пример Nginx:

```nginx
server {
    listen 443 ssl;
    server_name <домен>;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location = /telegram/webhook {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location = /oauth/callback {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location = /health/live {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Не добавлять Basic Auth, корпоративный SSO или VPN-проверку перед `/telegram/webhook` и `/oauth/callback`: Telegram и браузер пользователя должны достигать этих маршрутов напрямую. Callback защищён одноразовым OAuth state и PKCE.

## 5. Подготовка секретов и env

Создать вне Git файл, например:

```text
/opt/modus-sales-bot/.env.production
```

Права:

```bash
chmod 600 /opt/modus-sales-bot/.env.production
```

Минимальное содержимое:

```env
APP_ENV=production

POSTGRES_DB=sales_bot
POSTGRES_USER=sales_bot
POSTGRES_PASSWORD=<secret>
DATABASE_URL=postgresql+psycopg://sales_bot:<secret>@db:5432/sales_bot

PUBLIC_BASE_URL=https://<домен>
HTTP_BIND_ADDRESS=127.0.0.1
HTTP_PORT=8000

TELEGRAM_BOT_TOKEN=<secret>
TELEGRAM_WEBHOOK_SECRET=<secret>
ADMIN_TELEGRAM_IDS=<telegram_id>[,<telegram_id>]
PILOT_TELEGRAM_IDS=
SAFETY_IDENTIFIER_SECRET=<secret>

AGENT_BACKEND=openai
OPENAI_API_KEY=<secret>
OPENAI_MODEL=gpt-5.6-luna
OPENAI_REASONING_EFFORT=low

KEYCLOAK_FLOW=authorization_code
KEYCLOAK_ISSUER=https://auth.modusbi.ru/realms/master
KEYCLOAK_CLIENT_ID=modus-sales-telegram-bot
KEYCLOAK_SCOPES=openid profile email offline_access
KEYCLOAK_RESOURCE=https://mcp.modusbi.ru
TOKEN_ENCRYPTION_KEY=<fernet-secret>
```

`TOKEN_ENCRYPTION_KEY` должен быть постоянным Fernet key. Его потеря сделает сохранённые OAuth-токены нечитаемыми; компрометация требует замены ключа и повторного `/login` всех пользователей.

Для production предпочтительно передавать значения из Vault/CI/CD secret storage. Если используется env-файл, не включать его в backup репозитория и артефакты сборки.

## 6. Развёртывание

В каталоге репозитория:

```bash
chmod +x scripts/server-up.sh
./scripts/server-up.sh /opt/modus-sales-bot/.env.production
```

`migrate` обязан завершиться с кодом 0 до старта web. В production приложение не создаёт таблицы неявно; версия схемы фиксируется в `alembic_version`.
После readiness скрипт проверяет OAuth metadata и регистрирует Telegram webhook. Для подготовительного запуска без регистрации webhook используйте `REGISTER_TELEGRAM_WEBHOOK=false`.

Проверить health:

```bash
curl --fail http://127.0.0.1:8000/health/ready
curl --fail https://<домен>/health/live
```

Ожидается HTTP 200 и JSON со статусом `ok`/`ready`.

## 7. Проверка OAuth metadata

Команда не выполняет пользовательский вход и не выводит секреты:

```bash
docker compose --env-file "$ENV_FILE" run --rm web \
  python scripts/check_keycloak_config.py
```

Ожидаемый результат:

```text
keycloak-config-ok flow=authorization_code ...
```

Проверка подтверждает issuer, resource, Bearer header, scopes, Authorization Code, refresh token и PKCE S256. Она не подтверждает настройки конкретного client — это проверяется живым `/login`.

## 8. Регистрация Telegram webhook

После успешного healthcheck:

```bash
docker compose --env-file "$ENV_FILE" run --rm web \
  python scripts/register_webhook.py --url https://<домен>
```

Скрипт регистрирует `message` и `callback_query`, поэтому будут работать обычные команды и кнопки администратора.

## 9. Live-приёмка

Проверить последовательно:

1. Администратор отправляет боту `/start` и получает активный доступ.
2. Неизвестный пользователь отправляет `/start`; администратор получает кнопки «Разрешить» и «Отклонить».
3. До подтверждения запрос пользователя не вызывает OpenAI/MCP.
4. После подтверждения пользователь отправляет `/login`.
5. Ссылка содержит:
   - `client_id=modus-sales-telegram-bot`;
   - `response_type=code`;
   - callback бота;
   - `code_challenge_method=S256`;
   - `resource=https://mcp.modusbi.ru`.
6. Ссылка не содержит `client_secret`, access/refresh token или `id_token_hint`.
7. После входа Keycloak возвращает браузер на `/oauth/callback`.
8. Браузер показывает успешное завершение, бот присылает уведомление.
9. Повторное открытие callback отклоняется как использованная OAuth-сессия.
10. Выполнить `/mcp_status`: Jira, Bitrix и KTalk должны отображаться подключёнными.
11. Выполнить `/mcp jira покажи SH-501` и получить реальный ответ Jira.
12. Выполнить `/mcp bitrix покажи мой профиль Bitrix` и получить реальный профиль.
13. Выполнить `/mcp ktalk покажи последние записи` и получить результат либо зафиксированную ошибку upstream KTalk.
14. Выполнить `/logout`; следующий `/mcp jira ...` снова требует `/login`.
15. Администратор отзывает доступ; новые OpenAI/MCP-вызовы блокируются без перезапуска.

## 10. Проверка логов и секретов

```bash
docker compose --env-file "$ENV_FILE" logs --tail 200 web worker
```

В логах не должно быть:

- Telegram/OpenAI secrets;
- authorization code;
- access/refresh token;
- PKCE verifier;
- паролей, OTP;
- полного текста пользовательских сообщений.

## 11. Backup и перезапуск

До пилота настроить ежедневный backup volume PostgreSQL и выполнить минимум одно тестовое восстановление. После перезапуска проверить сохранность:

- allowlist;
- OAuth-токенов;
- версий skills;
- admin audit и usage metrics.

Не менять `TOKEN_ENCRYPTION_KEY` при обычном рестарте или обновлении.

## 12. Что DevOps должен вернуть разработчику

Без секретов:

- итоговый домен и `PUBLIC_BASE_URL`;
- `client_id`;
- подтверждённые issuer, scopes и resource/audience;
- access/refresh token lifetime и refresh rotation policy;
- результат `check_keycloak_config.py`;
- результат healthcheck и `/login`;
- подтверждение, что Jira MCP принял персональный token;
- список сетевых ограничений или ошибок;
- утверждённые read-only tools Bitrix/KTalk либо контакты их владельцев.
