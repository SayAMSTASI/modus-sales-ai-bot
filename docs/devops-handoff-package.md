# Передача Modus Sales Bot в эксплуатацию

## Результат

Развернуть Telegram-бота, PostgreSQL, миграции, web и worker в Docker Compose,
перенести текущее состояние пилота и проверить OpenAI, Keycloak и MCP.

Архив содержит production-секреты и персональные данные. Его следует хранить
только во внутреннем защищённом контуре, не распаковывать в Git-каталог и удалить
после подтверждённого переноса в корпоративное хранилище секретов и backup.

## Состав зашифрованного архива

- `source/` — исходный код зафиксированного Git-коммита;
- `secrets/.env.production` — Telegram token, OpenAI key, Keycloak client,
  постоянный Fernet key и остальные параметры;
- `data/sales_bot.db` — полная SQLite-копия текущего пилота;
- `data/users-summary.json` — контрольная сводка пользователей без OAuth-токенов;
- `manifest.json` — версия, состав и контрольные количества;
- `SHA256SUMS.txt` — SHA-256 файлов внутри пакета;
- `DEPLOYMENT.md` — эта инструкция.

Пароль архива передаётся отдельным внутренним комментарием Service Hub. Не
пересылать пароль во внешние каналы и не делать комментарий общедоступным.

## Предварительные требования

- Linux host: от 2 vCPU, 4 GB RAM, 20 GB свободного диска;
- Docker Engine и Docker Compose plugin;
- домен с TLS-сертификатом для production webhook;
- точный redirect URI в Keycloak:
  `https://<домен>/oauth/callback`;
- исходящий TCP/443 к `api.telegram.org`, `api.openai.com`,
  `auth.modusbi.ru`, `mcp.modusbi.ru`;
- входящий HTTPS к `/telegram/webhook` и `/oauth/callback`.

На тестовом сервере `188.120.251.22` 2 сентября 2026 года исходящее соединение
к `api.telegram.org:443` завершалось таймаутом. OpenAI, Keycloak и MCP были
доступны. До запуска на этой машине нужно исправить firewall, security group,
маршрутизацию или выбрать другой host. Проверка:

```bash
curl -4 --fail --connect-timeout 10 https://api.telegram.org
```

Ответ HTTP 404 допустим; timeout или `Network is unreachable` — нет.

## Распаковка и защита файлов

```bash
sudo mkdir -p /opt/modus-sales-bot
sudo 7z x modus-sales-bot-devops-*.7z -o/opt/modus-sales-bot
sudo chown -R <service-user>:<service-group> /opt/modus-sales-bot
sudo chmod 700 /opt/modus-sales-bot/secrets
sudo chmod 600 /opt/modus-sales-bot/secrets/.env.production
cd /opt/modus-sales-bot/source
```

Сверить файлы до запуска:

```bash
cd /opt/modus-sales-bot
sha256sum -c SHA256SUMS.txt
```

Перенести значения `secrets/.env.production` в Vault или другое корпоративное
хранилище. Не включать env в образ, Git, логи или backup исходного кода.

## Настройка режима production webhook

В защищённом env проверить или заменить:

```env
APP_ENV=production
PUBLIC_BASE_URL=https://<домен>
KEYCLOAK_FLOW=authorization_code
KEYCLOAK_CLIENT_ID=modus-sales-telegram-bot
KEYCLOAK_ISSUER=https://auth.modusbi.ru/realms/master
KEYCLOAK_RESOURCE=https://mcp.modusbi.ru
KEYCLOAK_SCOPES=openid profile email offline_access
```

Keycloak client должен быть public: Client authentication OFF, Standard Flow
ON, PKCE S256, refresh token ON. Client secret не используется. Для временной
проверки без домена можно оставить `KEYCLOAK_FLOW=device` и запустить polling
Compose, но итоговый production-вариант — webhook.

Reverse proxy должен направлять только следующие пути на `127.0.0.1:8000`:

- `/telegram/webhook`;
- `/oauth/callback`;
- `/health/live` и `/health/ready` для мониторинга.

## Создание БД и перенос текущих данных

Поднять PostgreSQL и применить миграции, не запуская worker:

```bash
cd /opt/modus-sales-bot/source
export ENV_FILE=/opt/modus-sales-bot/secrets/.env.production
docker compose --env-file "$ENV_FILE" up -d db
docker compose --env-file "$ENV_FILE" run --rm migrate
```

Импорт работает только в пустые бизнес-таблицы и откажется объединять данные с
существующей БД:

```bash
docker compose --env-file "$ENV_FILE" run --rm --no-deps --user 0 \
  -v /opt/modus-sales-bot/data/sales_bot.db:/handoff/sales_bot.db:ro \
  web python scripts/import_sqlite_to_postgres.py /handoff/sales_bot.db
```

Root используется только в одноразовом importer-контейнере для чтения backup с
закрытыми правами. Постоянные web/worker-контейнеры продолжают работать от
непривилегированного пользователя `app`.

В env уже находится тот же `TOKEN_ENCRYPTION_KEY`, которым зашифрованы OAuth
credentials в SQLite. Его нельзя заменять при обычном запуске или обновлении.
Текущий access token может быть просрочен; бот должен обновить его по refresh
token либо пользователь повторно выполнит вход.

## Запуск

После DNS, TLS, reverse proxy и сетевых разрешений:

```bash
cd /opt/modus-sales-bot/source
chmod +x scripts/server-up.sh
./scripts/server-up.sh /opt/modus-sales-bot/secrets/.env.production
```

Скрипт проверит Compose, миграции, readiness, Keycloak metadata и зарегистрирует
Telegram webhook. Для подготовительного старта без webhook:

```bash
REGISTER_TELEGRAM_WEBHOOK=false \
  ./scripts/server-up.sh /opt/modus-sales-bot/secrets/.env.production
```

## Приёмка

```bash
docker compose --env-file "$ENV_FILE" ps
curl --fail http://127.0.0.1:8000/health/ready
docker compose --env-file "$ENV_FILE" logs --tail 200 web worker
```

Проверить в Telegram:

1. Администратор получает меню после `/start`.
2. Неизвестный пользователь не вызывает OpenAI/MCP до подтверждения.
3. Разрешение, блокировка и назначение администратора работают кнопками.
4. `/login` завершается, `/mcp_status` показывает Jira, Bitrix и KTalk.
5. Выполнен минимум один read-only вызов каждого MCP.
6. Контекст сохраняется, `/new` запрашивает оценку, статистика обновляется.
7. После `docker compose restart` сохраняются пользователи, OAuth и skills.
8. В логах нет Telegram/OpenAI keys, OAuth-токенов и полного текста сообщений.

## Backup и rollback

До пилота настроить ежедневный backup PostgreSQL и выполнить тестовое
восстановление. Сохранить immutable image tag и Git SHA из `manifest.json`.

Rollback приложения: вернуть предыдущий image/tag и выполнить readiness check.
До отката схемы БД восстановить проверенный backup. Не удалять текущий volume и
не менять `TOKEN_ENCRYPTION_KEY` без отдельного плана ротации и повторного входа
пользователей.
