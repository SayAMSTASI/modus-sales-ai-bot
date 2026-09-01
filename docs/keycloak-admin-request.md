# Запрос администраторам Keycloak для Modus Sales Telegram Bot

## Готовый текст запроса

Просим создать в Keycloak публичный OIDC-клиент для пилотного Telegram Sales Bot.

### Параметры клиента

- Realm: `master`. Если MCP используют другой realm, просим сообщить корректный issuer до создания клиента.
- Предлагаемый Client ID: `modus-sales-telegram-bot`.
- Client type: public.
- Client authentication: OFF.
- Standard Flow: ON.
- PKCE method: обязательно `S256`.
- OAuth 2.0 Device Authorization Grant: ON для локальной проверки без публичного callback.
- Implicit Flow: OFF.
- Direct Access Grants / Resource Owner Password Credentials: OFF.
- Service Accounts: OFF.
- Authorization Services: OFF.
- Разрешённые scopes: `openid profile email offline_access`.
- Client secret не создавать и не передавать.
- Valid Redirect URI для серверного режима: `https://<домен-бота>/oauth/callback` — точное значение без wildcard.
- Web Origin не требуется: callback обрабатывает серверный backend, браузерный JavaScript token exchange отсутствует.

### Токены и доступ к MCP

- Access token должен приниматься MCP-сервисами:
  - `https://mcp.modusbi.ru/jira/mcp`;
  - `https://mcp.modusbi.ru/bitrix/mcp`;
  - `https://mcp.modusbi.ru/ktalk/mcp`.
- Права должны определяться корпоративной учётной записью вошедшего пользователя, а не сервисной учётной записью бота.
- Просим подтвердить требуемый `audience`/`resource` для этих MCP. Если он обязателен, добавить соответствующий audience mapper в access token.
- Разрешить выдачу refresh token для Authorization Code и Device Flow и его обновление без `client_secret`.
- Настроить разумный короткий срок access token и сообщить сроки жизни access/refresh token и политику rotation/reuse refresh token.
- Оставить доступным revocation endpoint для удаления подключения по команде `/logout`.

### Что передать разработчику

Передать только безопасные настройки:

1. точный `issuer`;
2. `client_id`;
3. scopes;
4. обязательный `audience`/`resource`, если используется;
5. срок жизни access token;
6. срок жизни и политика refresh token;
7. подтверждение, что authorization, Device Authorization, token и revoke endpoints доступны приложению;
8. подтверждение, что токен одного пользователя принимается всеми тремя MCP либо описание отдельных клиентов/scopes.

Не передавать пароль пользователя, OTP или `client_secret`. Приложение использует public client. На сервере применяется Authorization Code + PKCE S256, локально — Device Flow.

## Проверка администратором

После создания клиента просим проверить:

1. Authorization Code Flow принимает точный callback бота, требует PKCE S256 и отклоняет посторонние redirect URI.
2. Device Authorization endpoint выдаёт `device_code`, `user_code`, ссылку входа, TTL и polling interval.
3. После корпоративного входа token endpoint выдаёт access token и refresh token без `client_secret`.
4. В access token присутствуют нужные user claims и audience MCP.
5. Bearer access token возвращает успешный ответ хотя бы для одного согласованного read-only tool каждого MCP.
6. Отозванный или истёкший token больше не принимается MCP.

## Данные для текущей реализации

- Issuer: `https://auth.modusbi.ru/realms/master`.
- Authorization endpoint: `https://auth.modusbi.ru/realms/master/protocol/openid-connect/auth`.
- Device Authorization endpoint: `https://auth.modusbi.ru/realms/master/protocol/openid-connect/auth/device`.
- Token endpoint: `https://auth.modusbi.ru/realms/master/protocol/openid-connect/token`.
- Revocation endpoint: `https://auth.modusbi.ru/realms/master/protocol/openid-connect/revoke`.
- Переменная приложения после получения значения: `KEYCLOAK_CLIENT_ID`.
- Resource: `https://mcp.modusbi.ru`.
- Callback приложения: `<PUBLIC_BASE_URL>/oauth/callback`.
- Секрет клиента не используется.
