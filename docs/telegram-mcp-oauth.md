# Авторизация MCP через Telegram

Поддерживаются два режима одного public Keycloak client без `client_secret`:

- `KEYCLOAK_FLOW=device` — локальный запуск без публичного callback;
- `KEYCLOAK_FLOW=authorization_code` — серверный запуск с Authorization Code + PKCE S256.

Бот присылает только официальную ссылку Keycloak и никогда не принимает в Telegram логин, пароль, OTP, authorization code или OAuth-токены.

## Пользовательский сценарий

1. Пользователь с подтверждённым доступом отправляет `/login`.
2. В Device Flow бот получает device code и опрашивает token endpoint.
3. В Authorization Code Flow бот создаёт одноразовые `state`, PKCE verifier/challenge и присылает ссылку со своим `client_id` и callback.
4. Пользователь входит только на `auth.modusbi.ru`; Keycloak возвращает короткоживущий code на `<PUBLIC_BASE_URL>/oauth/callback`.
5. Callback проверяет state, активный доступ пользователя и TTL, затем один раз обменивает code + verifier на токены.
6. Access и refresh token шифруются Fernet-ключом `TOKEN_ENCRYPTION_KEY` и сохраняются в БД для конкретного Telegram ID.
7. Истёкший access token автоматически обновляется через refresh token.
8. При MCP-вызове access token текущего пользователя передаётся в поле `authorization` remote MCP tool Responses API.
9. `/logout` пытается отозвать token и всегда удаляет токены и незавершённые login-сессии.

Одна корпоративная сессия используется для Jira, Bitrix и KTalk, если Keycloak/MCP подтвердят общий issuer, audience и scopes. Если владельцы MCP потребуют разные клиенты или scopes, модель хранения нужно расширить до пары `Telegram ID + MCP server`.

## Безопасность

- конфигурация использует public client без `client_secret`;
- Authorization Code Flow требует точный HTTPS redirect URI и PKCE S256;
- OAuth state хранится только как SHA-256 hash, verifier — только зашифрованным, state нельзя использовать повторно;
- ссылка не содержит `id_token_hint`;
- токены не выводятся в Telegram, логи, метрики, Git или Jira;
- `TOKEN_ENCRYPTION_KEY` хранится вне БД в защищённой переменной окружения;
- отозванному пользователю блокируются OpenAI/MCP, удаляются контекст и OAuth-токены;
- приложению передаётся единый read-only allowlist MCP tools;
- записывающие MCP tools не включаются в конфигурацию.

Готовый запрос администраторам: [keycloak-admin-request.md](keycloak-admin-request.md).
