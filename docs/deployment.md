# Развёртывание Modus Sales Bot в контуре организации

Пошаговая инструкция для исполнителя: [devops-keycloak-deployment-runbook.md](devops-keycloak-deployment-runbook.md).

## Целевой результат

Внутри корпоративного контура работают PostgreSQL, одноразовый мигратор, webhook/API и один worker. Внешний TLS reverse proxy принимает Telegram webhook. Секреты передаются отдельно через корпоративное хранилище секретов или закрытый env-файл на сервере и никогда не попадают в Git, Jira, образ или логи.

## Подготовка инфраструктуры

Нужны:

- Linux VM или container host: от 2 vCPU, 4 GB RAM и 20 GB диска;
- Docker Engine с Compose plugin;
- DNS-имя и TLS-сертификат;
- входящий HTTPS от Telegram к `/telegram/webhook`;
- исходящий HTTPS к `api.telegram.org`, `api.openai.com`, `auth.modusbi.ru` и `mcp.modusbi.ru`;
- PostgreSQL 17 с ежедневным backup и проверяемым восстановлением;
- отдельный OpenAI Project API key с бюджетным ограничением;
- public OAuth client Keycloak с Authorization Code + PKCE и Device Flow для локальной проверки.

## Передача конфигурации

На сервере создать закрытый файл, например `/opt/modus-sales-bot/.env.production`, на основе `.env.example`. Права файла — только пользователю сервиса. Обязательные значения:

- `APP_ENV=production`;
- `POSTGRES_PASSWORD` и совпадающий `DATABASE_URL` с host `db`;
- `PUBLIC_BASE_URL`;
- `TELEGRAM_BOT_TOKEN` и случайный `TELEGRAM_WEBHOOK_SECRET`;
- `ADMIN_TELEGRAM_IDS`, `PILOT_TELEGRAM_IDS` и `SAFETY_IDENTIFIER_SECRET`;
- `QUESTION_AUDIT_RETENTION_DAYS`, `QUESTION_AUDIT_MAX_CHARS` и
  `ADMIN_USERS_PAGE_SIZE`;
- `AGENT_BACKEND=openai`, `OPENAI_API_KEY`, утверждённая модель и актуальные тарифы;
- `KEYCLOAK_ISSUER`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_SCOPES`;
- `KEYCLOAK_RESOURCE=https://mcp.modusbi.ru`;
- `KEYCLOAK_FLOW=authorization_code`;
- постоянный `TOKEN_ENCRYPTION_KEY` в защищённой переменной окружения.

Access/refresh tokens пользователей хранятся только в PostgreSQL в зашифрованном виде. Fernet key нельзя хранить в БД: при его потере сохранённые OAuth-сессии невозможно расшифровать, при компрометации ключ нужно заменить и потребовать повторный `/login`.

## Запуск

```bash
chmod +x scripts/server-up.sh
./scripts/server-up.sh /opt/modus-sales-bot/.env.production
```

Скрипт валидирует Compose, собирает образ, запускает PostgreSQL, выполняет `alembic upgrade head`, поднимает web/worker, проверяет Keycloak metadata, ждёт readiness и регистрирует Telegram webhook. Если DNS/TLS ещё не готовы, перед запуском можно временно задать `REGISTER_TELEGRAM_WEBHOOK=false`.

Reverse proxy направляет `https://<domain>/telegram/webhook` и `https://<domain>/oauth/callback` на web-контейнер. Значение `PUBLIC_BASE_URL` должно быть ровно `https://<domain>`, а Keycloak Valid Redirect URI — ровно `https://<domain>/oauth/callback`. Порт 8000 не публикуется наружу напрямую.

После запуска зарегистрировать webhook командой `scripts/register_webhook.py`, передав token и webhook secret только через защищённую сессию сервера.

## Приёмка рабочего состояния

1. Healthcheck web, worker и PostgreSQL зелёные после перезапуска host.
2. Telegram принимает `/start` только в private chat; повторный `update_id` не создаёт второй ответ.
3. Пользователь проходит `/login` на `auth.modusbi.ru`; бот не принимает пароль или OTP.
4. Access/refresh tokens зашифрованы в PostgreSQL, обновляются и удаляются при `/logout` или revoke.
5. Утверждён read-only allowlist для каждого MCP; write tools недоступны.
6. KTalk-ссылка возвращает реальный протокол, Jira — реальную задачу, Bitrix — разрешённые пользователю данные.
7. OpenAI request использует `store=false`; секреты и текст диалога отсутствуют в
   технических логах. Текст вопросов сохраняется только в таблице административного
   журнала на настроенный срок; ответы агента туда не копируются.
8. Backup PostgreSQL восстановлен на тестовом стенде.
9. Есть алерты на недоступность, рост ошибок, лимиты OpenAI и заполнение диска.
10. Зафиксированы версия образа, commit SHA и команда rollback.

## Обновление и rollback

### Быстрое обновление роли и skills

Изменение `sales-core` или рабочего skill через Telegram применяется к следующему
запросу без перезапуска и пересборки контейнера. Это самый быстрый способ проверить
редакцию в пилоте. Редакция хранится в PostgreSQL как версия с автором и допускает
rollback через интерфейс администратора.

`sales-core` является главной ролью и маршрутизатором. Остальные skills загружаются
после него как рабочие модули. Системный read-only policy из `prompt.md` имеет более
высокий приоритет и не может быть отменён редакцией skill.

Для постоянного базового варианта проверенную редакцию нужно отдельно перенести в Git.
Git-версия станет базовой после следующего развёртывания; активная DB-версия продолжит
иметь приоритет, пока администратор не откатит её к базе.

### Обновление кода одной командой

На сервере с уже настроенным checkout и внешним env-файлом:

```bash
chmod +x scripts/server-update.sh
./scripts/server-update.sh /opt/modus-sales-bot/.env.production origin/main
```

Скрипт:

1. запрещает обновление поверх локальных изменений;
2. делает `git fetch` и только fast-forward merge;
3. поднимает PostgreSQL и сохраняет `pg_dump` рядом с защищённым env-файлом;
4. присваивает образу immutable tag из Git SHA;
5. повторно использует `server-up.sh`: Compose validation, build, migration,
   readiness, Keycloak check и регистрацию webhook;
6. сохраняет предыдущий/current commit, image tag и путь backup в `deploy-state`.

Автоматический rollback после неуспешной миграции намеренно не выполняется. Скрипт
печатает предыдущий commit и backup; совместимость миграции и способ возврата образа
должны быть проверены DevOps до применения обновления.

Без SSH и доступа к production можно проверить только код, тесты, shell-синтаксис и
Compose-конфигурацию. Фактический `git fetch`, backup, rollout, readiness и webhook
должны быть выполнены и зафиксированы DevOps в целевом контуре.
