# Развёртывание Modus Sales Bot в контуре организации

## Целевой результат

Внутри корпоративного контура работают PostgreSQL, webhook/API и один worker. Внешний TLS reverse proxy принимает Telegram webhook. Секреты передаются отдельно через корпоративное хранилище секретов или закрытый env-файл на сервере и никогда не попадают в Git, Jira, образ или логи.

## Подготовка инфраструктуры

Нужны:

- Linux VM или container host: от 2 vCPU, 4 GB RAM и 20 GB диска;
- Docker Engine с Compose plugin;
- DNS-имя и TLS-сертификат;
- входящий HTTPS от Telegram к `/telegram/webhook`;
- исходящий HTTPS к `api.telegram.org`, `api.openai.com`, `auth.modusbi.ru` и `mcp.modusbi.ru`;
- PostgreSQL 17 с ежедневным backup и проверяемым восстановлением;
- отдельный OpenAI Project API key с бюджетным ограничением;
- public OAuth client Keycloak с Device Authorization Grant.

## Передача конфигурации

На сервере создать закрытый файл, например `/opt/modus-sales-bot/.env.production`, на основе `.env.example`. Права файла — только пользователю сервиса. Обязательные значения:

- `APP_ENV=production`;
- `POSTGRES_PASSWORD` и совпадающий `DATABASE_URL` с host `db`;
- `PUBLIC_BASE_URL`;
- `TELEGRAM_BOT_TOKEN` и случайный `TELEGRAM_WEBHOOK_SECRET`;
- `ADMIN_PASSWORD` и `SAFETY_IDENTIFIER_SECRET`;
- `AGENT_BACKEND=openai`, `OPENAI_API_KEY`, утверждённая модель и актуальные тарифы;
- `MODUSBI_MCP_OAUTH_CLIENT_ID` после выполнения SH-519.

Access/refresh tokens пользователей нельзя хранить в env-файле. До промышленного запуска необходимо реализовать персональный Device Flow и зашифрованное хранение токенов в PostgreSQL.

## Запуск

```bash
export ENV_FILE=/opt/modus-sales-bot/.env.production
docker compose --env-file "$ENV_FILE" pull
docker compose --env-file "$ENV_FILE" up -d --build
docker compose --env-file "$ENV_FILE" ps
curl --fail http://127.0.0.1:8000/health/ready
```

Reverse proxy направляет `https://<domain>/telegram/webhook` на `http://127.0.0.1:8000/telegram/webhook`. Порт 8000 не публикуется наружу напрямую.

После запуска зарегистрировать webhook командой `scripts/register_webhook.py`, передав token и webhook secret только через защищённую сессию сервера.

## Приёмка рабочего состояния

1. Healthcheck web, worker и PostgreSQL зелёные после перезапуска host.
2. Telegram принимает `/start` только в private chat; повторный `update_id` не создаёт второй ответ.
3. Пользователь проходит `/connect jira|bitrix|ktalk` на `auth.modusbi.ru`; бот не принимает пароль или OTP.
4. Access/refresh tokens зашифрованы в PostgreSQL, обновляются и удаляются при `/disconnect` или revoke.
5. Утверждён read-only allowlist для каждого MCP; write tools недоступны.
6. KTalk-ссылка возвращает реальный протокол, Jira — реальную задачу, Bitrix — разрешённые пользователю данные.
7. OpenAI request использует `store=false`; секреты и текст диалога отсутствуют в технических логах.
8. Backup PostgreSQL восстановлен на тестовом стенде.
9. Есть алерты на недоступность, рост ошибок, лимиты OpenAI и заполнение диска.
10. Зафиксированы версия образа, commit SHA и команда rollback.

## Обновление и rollback

Перед обновлением сделать backup БД, собрать образ с новым immutable tag, выполнить smoke-test и только затем переключать сервисы. Для rollback вернуть предыдущий tag; миграции БД должны иметь отдельно проверенный план обратной совместимости.
