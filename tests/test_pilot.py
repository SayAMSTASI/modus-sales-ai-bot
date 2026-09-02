from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from app.agent import (
    AgentUsage,
    McpUnavailableError,
    MockAgentClient,
    OpenAIAgentClient,
    calculate_cost_usd,
    load_project_instructions,
)
from app.config import Settings
from app.local_bot import LocalBotRuntime
from app.logging_config import configure_logging
from app.models import (
    AdminAudit,
    ConversationMessage,
    OAuthAuthorizationSession,
    OAuthCredential,
    OAuthDeviceSession,
    SkillVersion,
    UpdateJob,
    UsageEvent,
    UserAccess,
)
from app.oauth import KeycloakOAuthClient, TokenPollResult, TokenResponse
from app.skills import active_skill_version
from app.telegram import InMemoryTelegramClient
from app.worker import JobProcessor, shared_allowed_tools
from tests.conftest import callback_update, telegram_update


def test_local_logging_does_not_expose_http_request_urls():
    configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def approve_user(runtime, post_update, drain, user_id: int = 101) -> None:
    assert post_update(telegram_update(1, user_id, "/start")).status_code == 200
    drain()
    assert post_update(callback_update(2, 900, f"access:approve:{user_id}")).status_code == 200
    drain()


def save_token(runtime, user_id: int = 101, *, expires_in: int = 300) -> None:
    with runtime["factory"]() as session:
        runtime["oauth_store"].save(
            session,
            user_id,
            TokenResponse(
                access_token="secret-access",
                refresh_token="secret-refresh",
                token_type="Bearer",
                scope="openid profile email",
                expires_in=expires_in,
                refresh_expires_in=3600,
            ),
        )
        session.commit()


def test_webhook_rejects_wrong_secret(runtime):
    response = runtime["client"].post(
        "/telegram/webhook",
        json=telegram_update(1, 101, "/start"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert response.status_code == 401


def test_private_chat_is_required(runtime, post_update):
    response = post_update(telegram_update(1, 101, "/start", chat_type="group"))
    assert response.json()["reason"] == "private_chat_required"
    with runtime["factory"]() as session:
        assert session.scalar(select(func.count(UpdateJob.id))) == 0


def test_unknown_user_requires_telegram_approval(runtime, post_update, drain):
    assert post_update(telegram_update(1, 101, "/start")).json()["accepted"] is True
    drain()

    assert runtime["telegram"].messages[-2][0] == 900
    assert "Новый запрос доступа" in runtime["telegram"].messages[-2][1]
    buttons = runtime["telegram"].markups[-2]["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == ["Разрешить", "Отклонить"]
    assert "OpenAI и MCP не вызываются" in runtime["telegram"].messages[-1][1]
    assert runtime["agent"].calls == 0

    post_update(callback_update(2, 900, "access:approve:101"))
    drain()
    with runtime["factory"]() as session:
        user = session.scalar(select(UserAccess).where(UserAccess.telegram_user_id == 101))
        audit = session.scalar(select(AdminAudit).where(AdminAudit.action == "approve"))
        assert user.status == "active"
        assert audit is not None
    assert any(
        chat_id == 101 and "Доступ подтверждён" in text
        for chat_id, text in runtime["telegram"].messages
    )


def test_pending_message_never_calls_agent(runtime, post_update, drain):
    post_update(telegram_update(1, 101, "/start"))
    drain()
    post_update(telegram_update(2, 101, "Покажи сделки"))
    drain()
    assert runtime["agent"].calls == 0


def test_approved_flow_records_context_and_aggregate_metrics(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    post_update(telegram_update(3, 101, "Покажи сделку"))
    drain()

    assert runtime["agent"].calls == 1
    with runtime["factory"]() as session:
        event = session.scalar(select(UsageEvent))
        assert event.user_hash.startswith("tg_")
        assert event.input_tokens > 0
        assert event.estimated_cost_usd > 0
        assert session.scalar(select(func.count(ConversationMessage.id))) == 2


def test_new_clears_context(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    post_update(telegram_update(3, 101, "Первый вопрос"))
    drain()
    post_update(telegram_update(4, 101, "/new"))
    drain()
    with runtime["factory"]() as session:
        assert session.scalar(select(func.count(ConversationMessage.id))) == 0


def test_second_message_receives_context(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    post_update(telegram_update(3, 101, "Первый вопрос"))
    drain()
    post_update(telegram_update(4, 101, "Уточни ответ"))
    drain()
    assert [item["role"] for item in runtime["agent"].last_messages] == [
        "developer",
        "user",
        "assistant",
        "user",
    ]


def test_mcp_status_reports_authorization_and_configured_allowlists(
    runtime, post_update, drain
):
    approve_user(runtime, post_update, drain)
    post_update(telegram_update(3, 101, "/mcp_status"))
    drain()
    status = runtime["telegram"].messages[-1][1]
    assert "jira: не подключён" in status
    assert "bitrix: не подключён" in status
    assert "ktalk: не подключён" in status

    save_token(runtime)
    post_update(telegram_update(4, 101, "/mcp_status"))
    drain()
    status = runtime["telegram"].messages[-1][1]
    assert "jira: подключён" in status
    assert "bitrix: подключён" in status
    assert "ktalk: подключён" in status


def test_agent_receives_factual_mcp_capability_note(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    post_update(telegram_update(3, 101, "Что умеет Bitrix MCP?"))
    drain()
    note = runtime["agent"].last_messages[0]
    assert note["role"] == "developer"
    assert "bitrix: не подключён" in note["content"]


def test_admin_can_discover_mcp_tools_without_enabling_them(
    runtime, post_update, drain
):
    post_update(telegram_update(1, 900, "/start"))
    drain()
    save_token(runtime, user_id=900)
    post_update(telegram_update(2, 900, "/mcp_discover bitrix"))
    drain()
    message = runtime["telegram"].messages[-1][1]
    assert "найдено tools — 2" in message
    assert "crm_deal_list" in message
    assert runtime["mcp_discovery"].calls == [("bitrix", "secret-access")]


def test_login_poll_and_logout_store_only_encrypted_tokens(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    post_update(telegram_update(3, 101, "/login"))
    drain()
    assert "auth.example" in runtime["telegram"].messages[-1][1]

    with runtime["factory"]() as session:
        device = session.scalar(select(OAuthDeviceSession))
        assert device.device_code_encrypted != "secret-device-code"
        device.next_poll_at = datetime.now(UTC)
        session.commit()
    runtime["oauth_client"].poll_result = TokenPollResult(
        state="authorized",
        token=TokenResponse(
            access_token="secret-access",
            refresh_token="secret-refresh",
            token_type="Bearer",
            scope="openid profile email",
            expires_in=300,
            refresh_expires_in=3600,
        ),
    )
    assert runtime["processor"].poll_oauth_once() is True
    drain()
    with runtime["factory"]() as session:
        credential = session.scalar(select(OAuthCredential))
        assert credential.access_token_encrypted != "secret-access"
        assert credential.refresh_token_encrypted != "secret-refresh"

    post_update(telegram_update(4, 101, "/logout"))
    drain()
    with runtime["factory"]() as session:
        assert session.scalar(select(func.count(OAuthCredential.id))) == 0
    assert runtime["oauth_client"].revoked_tokens == ["secret-refresh"]


def test_authorization_code_pkce_callback_is_one_time(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    runtime["settings"].keycloak_flow = "authorization_code"
    runtime["settings"].public_base_url = "https://bot.example"
    post_update(telegram_update(3, 101, "/login"))
    drain()
    assert "code_challenge_method=S256" in runtime["telegram"].messages[-1][1]

    with runtime["factory"]() as session:
        authorization = session.scalar(select(OAuthAuthorizationSession))
        assert authorization.state_hash != "test-state"
        assert authorization.code_verifier_encrypted != "secret-code-verifier"

    response = runtime["client"].get(
        "/oauth/callback",
        params={"state": "test-state", "code": "one-time-code"},
    )
    assert response.status_code == 200
    assert runtime["oauth_client"].authorization_code_exchanges == [
        ("one-time-code", "secret-code-verifier")
    ]
    drain()
    with runtime["factory"]() as session:
        assert session.scalar(select(func.count(OAuthAuthorizationSession.id))) == 0
        credential = session.scalar(select(OAuthCredential))
        assert credential.access_token_encrypted != "code-access"
    replay = runtime["client"].get(
        "/oauth/callback",
        params={"state": "test-state", "code": "one-time-code"},
    )
    assert replay.status_code == 400


def test_authorization_code_url_uses_own_callback_resource_and_pkce(runtime):
    runtime["settings"].public_base_url = "https://bot.example"
    client = KeycloakOAuthClient(runtime["settings"])
    authorization = client.start_authorization_code()
    parsed = urlparse(authorization.authorization_url)
    parameters = parse_qs(parsed.query)
    assert parsed.path.endswith("/protocol/openid-connect/auth")
    assert parameters["client_id"] == ["test-public-client"]
    assert parameters["redirect_uri"] == ["https://bot.example/oauth/callback"]
    assert parameters["resource"] == ["https://mcp.modusbi.ru"]
    assert parameters["code_challenge_method"] == ["S256"]
    assert "id_token_hint" not in parameters


def test_authorization_code_callback_rejects_expired_state(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    runtime["settings"].keycloak_flow = "authorization_code"
    runtime["settings"].public_base_url = "https://bot.example"
    post_update(telegram_update(3, 101, "/login"))
    drain()
    with runtime["factory"]() as session:
        authorization = session.scalar(select(OAuthAuthorizationSession))
        authorization.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    response = runtime["client"].get(
        "/oauth/callback",
        params={"state": "test-state", "code": "expired-code"},
    )
    assert response.status_code == 400
    assert runtime["oauth_client"].authorization_code_exchanges == []


def test_expired_access_token_is_refreshed(runtime):
    save_token(runtime, expires_in=1)
    with runtime["factory"]() as session:
        credential = session.scalar(select(OAuthCredential))
        credential.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    with runtime["factory"]() as session:
        assert runtime["oauth_store"].access_token(session, 101) == "refreshed-access"
        session.commit()


def test_mcp_uses_current_users_token(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    save_token(runtime)
    post_update(telegram_update(3, 101, "/mcp jira покажи SH-501"))
    drain()
    assert runtime["agent"].last_required_mcp_server == "jira"
    assert runtime["agent"].last_mcp_access_token == "secret-access"
    assert runtime["agent"].last_messages[-1]["content"] == "покажи SH-501"


def test_mcp_requires_login(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    post_update(telegram_update(3, 101, "/mcp jira покажи SH-501"))
    drain()
    assert runtime["agent"].calls == 0
    assert "/login" in runtime["telegram"].messages[-1][1]


def test_admin_edits_applies_and_rolls_back_skill(runtime, post_update, drain):
    post_update(telegram_update(1, 900, "/start"))
    drain()
    post_update(telegram_update(2, 900, "/skills"))
    drain()
    post_update(callback_update(3, 900, "skill:select:sales-core"))
    drain()
    post_update(telegram_update(4, 900, "/skill_edit"))
    drain()
    post_update(telegram_update(5, 900, "# Updated sales skill\nТолько тестовый текст."))
    drain()
    assert "Применить" == runtime["telegram"].markups[-1]["inline_keyboard"][0][0]["text"]
    post_update(callback_update(6, 900, "skill:apply"))
    drain()

    with runtime["factory"]() as session:
        assert active_skill_version(session, "sales-core").version == 1
    post_update(telegram_update(7, 900, "Проверить skill"))
    drain()
    assert runtime["agent"].last_instruction_overrides["sales-core"].startswith(
        "# Updated sales skill"
    )

    post_update(telegram_update(8, 900, "/skill_rollback"))
    drain()
    with runtime["factory"]() as session:
        assert active_skill_version(session, "sales-core") is None
        assert session.scalar(select(func.count(SkillVersion.id))) == 1


def test_admin_can_revoke_without_restart(runtime, post_update, drain):
    approve_user(runtime, post_update, drain)
    save_token(runtime)
    post_update(telegram_update(3, 900, "/revoke 101"))
    drain()
    post_update(telegram_update(4, 101, "Не должен уйти в OpenAI"))
    drain()
    assert runtime["agent"].calls == 0
    with runtime["factory"]() as session:
        user = session.scalar(select(UserAccess).where(UserAccess.telegram_user_id == 101))
        assert user.status == "revoked"
        assert session.scalar(select(func.count(OAuthCredential.id))) == 0


def test_project_bundle_loads_prompt_skill_and_override(runtime):
    instructions = load_project_instructions(
        runtime["settings"].project_dir,
        {"sales-core": "OVERRIDDEN SKILL"},
    )
    assert "корпоративный AI-агент команды продаж" in instructions
    assert "OVERRIDDEN SKILL" in instructions


def test_local_runtime_processes_configured_admin(runtime):
    class FakePollingTelegram(InMemoryTelegramClient):
        updates = [telegram_update(100, 900, "/start")]

        def get_me(self):
            return {"id": 1, "username": "local_test_bot"}

        def delete_webhook(self, *, drop_pending_updates: bool):
            return None

        def get_updates(self, *, offset: int | None, timeout: int):
            result = self.updates
            self.updates = []
            return result

    telegram = FakePollingTelegram()
    processor = JobProcessor(
        runtime["settings"],
        runtime["factory"],
        MockAgentClient(),
        telegram,
        runtime["processor"].metrics,
        runtime["oauth_store"],
    )
    local = LocalBotRuntime(runtime["settings"], runtime["factory"], telegram, processor)
    assert local.start()["username"] == "local_test_bot"
    assert local.poll_once() == 1
    assert "Доступ активен" in telegram.messages[-1][1]


def test_cost_formula_separates_cached_input(runtime):
    usage = AgentUsage(input_tokens=1000, cached_input_tokens=400, output_tokens=250)
    expected = (600 * 2.0 + 400 * 0.2 + 250 * 12.0) / 1_000_000
    assert calculate_cost_usd(usage, runtime["settings"]) == round(expected, 8)


def test_openai_mcp_tools_require_explicit_user_token():
    settings = Settings(
        app_env="test",
        openai_api_key="test-key",
        project_dir="config/project",
        mcp_servers_json=json.dumps(
            [
                {
                    "server_label": "jira",
                    "server_url": "https://example.org/mcp",
                    "authorization_type": "oauth",
                    "allowed_tools": ["jira_get_issue"],
                    "read_only": True,
                }
            ]
        ),
    )
    client = OpenAIAgentClient(settings)
    tools = client._tools(
        ["jira:jira_get_issue"],
        required_mcp_server="jira",
        mcp_access_token="current-user-token",
    )
    assert tools[0]["allowed_tools"] == ["jira_get_issue"]
    assert tools[0]["authorization"] == "current-user-token"
    try:
        client._tools(["jira:jira_get_issue"], required_mcp_server="jira")
    except McpUnavailableError:
        pass
    else:
        raise AssertionError("MCP server without current user token was accepted")


def test_optional_mcp_failure_retries_plain_openai_request():
    settings = Settings(
        app_env="test",
        openai_api_key="test-key",
        project_dir="config/project",
        mcp_servers_json=json.dumps(
            [
                {
                    "server_label": "jira",
                    "server_url": "https://example.org/mcp",
                    "authorization_type": "oauth",
                    "allowed_tools": ["jira_get_issue"],
                    "read_only": True,
                }
            ]
        ),
    )
    client = OpenAIAgentClient(settings)

    class FakeResponses:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("remote MCP unavailable")
            return SimpleNamespace(
                id="response-1",
                model="gpt-5.6-luna",
                output_text="Ответ без MCP",
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=5,
                    input_tokens_details=SimpleNamespace(cached_tokens=0),
                ),
            )

    fake_responses = FakeResponses()
    client.client = SimpleNamespace(responses=fake_responses)
    result = client.respond(
        messages=[{"role": "user", "content": "Обычный вопрос"}],
        safety_identifier="tg_test",
        allowed_tools=["jira:jira_get_issue"],
        mcp_access_token="current-user-token",
    )
    assert result.text == "Ответ без MCP"
    assert fake_responses.calls[0]["tools"]
    assert fake_responses.calls[1]["tools"] == []


def test_checked_in_mcp_registry_is_read_only():
    settings = Settings(app_env="test", mcp_servers_file="config/mcp_servers.json")
    servers = settings.mcp_servers()
    assert [server.server_label for server in servers] == ["jira", "bitrix", "ktalk"]
    assert all(server.authorization_type == "oauth" for server in servers)
    assert all(server.read_only is True for server in servers)
    assert servers[0].allowed_tools == [
        "jira_get_issue",
        "jira_search",
        "jira_search_fields",
        "jira_get_field_options",
        "jira_get_project_issues",
        "jira_get_transitions",
        "jira_get_agile_boards",
        "jira_get_board_issues",
        "jira_get_sprints_from_board",
        "jira_get_sprint_issues",
        "jira_batch_get_changelogs",
    ]
    assert len(servers[1].allowed_tools) == 13
    assert len(servers[2].allowed_tools) == 5

    client = OpenAIAgentClient(
        Settings(
            app_env="test",
            openai_api_key="test-key",
            mcp_servers_file="config/mcp_servers.json",
        )
    )
    attached = client._tools(
        shared_allowed_tools(settings),
        mcp_access_token="current-user-token",
    )
    assert [item["server_label"] for item in attached] == ["jira", "bitrix", "ktalk"]
