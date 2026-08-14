# Авторизация MCP через Telegram

## Решение

Для локального MVP используется OAuth 2.0 Device Authorization Grant. Telegram-бот управляет запуском авторизации и уведомлениями, но никогда не принимает логин, пароль, OTP, access token, refresh token или client secret сообщением.

## Пользовательский сценарий

1. Пользователь отправляет `/connect bitrix`, `/connect ktalk` или `/connect jira`.
2. Бот запрашивает device code у Keycloak.
3. Бот присылает официальную ссылку `auth.modusbi.ru`, одноразовый user code и срок действия.
4. Пользователь открывает ссылку и вводит корпоративные учётные данные только на странице Keycloak.
5. Бот ожидает подтверждение на token endpoint с интервалом, указанным Keycloak.
6. После успеха бот сохраняет access/refresh token в зашифрованном виде, привязанном к Telegram user ID.
7. Бот присылает уведомление: подключение успешно или требуется повторная авторизация.
8. Перед MCP-вызовом access token обновляется через refresh token при необходимости и передаётся OpenAI Responses API в поле `authorization` соответствующего remote MCP tool.

## Команды MVP

- `/connect bitrix`
- `/connect ktalk`
- `/connect jira`
- `/connections`
- `/disconnect bitrix|ktalk|jira`

## Обнаруженная конфигурация OAuth

- issuer: `https://auth.modusbi.ru/realms/master`
- device authorization endpoint: `https://auth.modusbi.ru/realms/master/protocol/openid-connect/auth/device`
- token endpoint: `https://auth.modusbi.ru/realms/master/protocol/openid-connect/token`
- scopes MCP: `openid profile email`
- bearer token: только HTTP Authorization header

## Требуемая настройка Keycloak

Нужен публичный OAuth client для локального Telegram MVP:

- известный `client_id`;
- Device Authorization Grant включён;
- Standard Flow/PKCE может быть включён как резервный вариант;
- client authentication выключена для публичного клиента;
- минимальные scopes `openid profile email`;
- ограниченный срок access token;
- refresh token разрешён только при необходимости;
- права пользователя определяются его собственной учётной записью.

`asdk_app_*` — идентификаторы приложения/версии OpenAI developer mode, а не Keycloak `client_id`.

## Хранение и безопасность

- один набор токенов на пару `Telegram user ID + MCP server`;
- токены не выводятся в Telegram, web UI, логи или CSV;
- в БД хранятся только зашифрованные значения;
- `/disconnect` отзывает token на revocation endpoint, если возможно, затем удаляет локальную запись;
- revoke доступа к боту блокирует MCP и удаляет пользовательский контекст;
- `allowed_tools` остаётся пустым до отдельного read-only discovery и утверждения;
- логин и пароль пользователя никогда не проходят через Telegram-бота.
