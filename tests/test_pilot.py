from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime

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
from app.ingest import ingest_update
from app.local_bot import LocalBotRuntime
from app.logging_config import configure_logging
from app.models import (
    AdminAudit,
    ConversationMessage,
    JobStatus,
    UpdateJob,
    UsageEvent,
    UserAccess,
)
from app.security import csrf_token
from app.telegram import InMemoryTelegramClient
from app.worker import JobProcessor
from tests.conftest import telegram_update


def auth_header(username: str = "admin", password: str = "strong-test-password") -> dict[str, str]:
    value = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {value}"}


def test_local_logging_does_not_expose_http_request_urls():
    configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def onboard_and_approve(runtime, post_update, drain, user_id: int = 101) -> None:
    response = post_update(telegram_update(1, user_id, "/start"))
    assert response.status_code == 200
    drain()
    response = runtime["client"].post(
        f"/admin/users/{user_id}/approve",
        headers=auth_header(),
        data={
            "csrf": csrf_token(runtime["settings"]),
            "corporate_name": "Иван Пилотов",
            "corporate_email": "pilot@example.org",
            "role": "pilot_user",
            "allowed_tools": "",
            "daily_request_limit": 50,
            "daily_token_limit": 250000,
            "daily_cost_limit_usd": 10,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    drain()


def test_webhook_rejects_wrong_secret(runtime):
    response = runtime["client"].post(
        "/telegram/webhook",
        json=telegram_update(1, 101, "/start"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert response.status_code == 401


def test_private_chat_and_owner_are_required(runtime, post_update):
    response = post_update(telegram_update(1, 101, "/start", chat_type="group"))
    assert response.json()["reason"] == "private_chat_required"
    with runtime["factory"]() as session:
        assert session.scalar(select(func.count(UpdateJob.id))) == 0


def test_start_creates_pending_request_and_is_idempotent(runtime, post_update, drain):
    response = post_update(telegram_update(1, 101, "/start"))
    assert response.json()["accepted"] is True
    assert drain() == 1
    assert "Заявка P-" in runtime["telegram"].messages[-1][1]

    duplicate = post_update(telegram_update(1, 101, "/start"))
    assert duplicate.json()["reason"] == "duplicate"
    with runtime["factory"]() as session:
        user = session.scalar(select(UserAccess).where(UserAccess.telegram_user_id == 101))
        assert user.status == "pending"
        assert session.scalar(select(func.count(UpdateJob.id))) == 1


def test_pending_message_never_calls_agent(runtime, post_update, drain):
    post_update(telegram_update(1, 101, "/start"))
    drain()
    post_update(telegram_update(2, 101, "Покажи сделки"))
    drain()
    assert runtime["agent"].calls == 0
    assert "подтверждения доступа" in runtime["telegram"].messages[-1][1]


def test_approved_flow_records_metrics_without_dialog_text(runtime, post_update, drain):
    onboard_and_approve(runtime, post_update, drain)
    secret_dialog_text = "Покажи сделку СЕКРЕТНЫЙ-ТЕКСТ-123"
    response = post_update(telegram_update(2, 101, secret_dialog_text))
    assert response.json()["accepted"] is True
    drain()

    assert runtime["agent"].calls == 1
    assert runtime["telegram"].messages[-1][1].startswith("Демо-ответ")
    with runtime["factory"]() as session:
        event = session.scalar(select(UsageEvent))
        assert event.user_hash.startswith("tg_")
        assert event.input_tokens > 0
        assert event.estimated_cost_usd > 0
        assert session.scalar(select(func.count(ConversationMessage.id))) == 2
        job = session.scalar(select(UpdateJob).where(UpdateJob.update_id == 2))
        assert job.status == JobStatus.done
        assert job.payload_text is None
        assert job.response_text is None
        serialized_metrics = json.dumps(
            {
                "hash": event.user_hash,
                "request": event.request_id,
                "scenario": event.scenario,
                "result": event.result,
            },
            ensure_ascii=False,
        )
        assert secret_dialog_text not in serialized_metrics


def test_new_clears_short_lived_context(runtime, post_update, drain):
    onboard_and_approve(runtime, post_update, drain)
    post_update(telegram_update(2, 101, "Первый вопрос"))
    drain()
    post_update(telegram_update(3, 101, "/new"))
    drain()
    with runtime["factory"]() as session:
        assert session.scalar(select(func.count(ConversationMessage.id))) == 0


def test_second_message_receives_short_lived_context(runtime, post_update, drain):
    onboard_and_approve(runtime, post_update, drain)
    post_update(telegram_update(2, 101, "Первый вопрос"))
    drain()
    post_update(telegram_update(3, 101, "Уточни ответ"))
    drain()

    assert [item["role"] for item in runtime["agent"].last_messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert runtime["agent"].last_messages[0]["content"] == "Первый вопрос"


def test_project_bundle_loads_prompt_and_skill(runtime):
    instructions = load_project_instructions(runtime["settings"].project_dir)
    assert "корпоративный AI-агент команды продаж" in instructions
    assert "# Skill: sales-core" in instructions
    assert "Базовая работа sales-агента" in instructions


def test_project_bundle_loads_skill_references(tmp_path):
    project = tmp_path / "project"
    skill = project / "skills" / "example"
    references = skill / "references"
    references.mkdir(parents=True)
    (project / "prompt.md").write_text("P" * 100, encoding="utf-8")
    (skill / "SKILL.md").write_text("Example skill", encoding="utf-8")
    (references / "template.md").write_text("Example reference", encoding="utf-8")

    instructions = load_project_instructions(project)
    assert "Example skill" in instructions
    assert "Example reference" in instructions


def test_local_polling_first_start_claims_owner_and_uses_mcp_allowlist(runtime):
    runtime["settings"].mcp_servers_json = json.dumps(
        [
            {
                "server_label": "bitrix",
                "server_url": "https://example.org/mcp",
                "allowed_tools": ["get_deal"],
                "read_only": True,
            }
        ]
    )
    first = ingest_update(
        runtime["settings"],
        runtime["factory"],
        telegram_update(1, 101, "/start"),
        auto_approve_local_owner=True,
    )
    second = ingest_update(
        runtime["settings"],
        runtime["factory"],
        telegram_update(2, 202, "/start"),
        auto_approve_local_owner=True,
    )
    assert first["accepted"] is True
    assert second["accepted"] is True

    with runtime["factory"]() as session:
        owner = session.scalar(select(UserAccess).where(UserAccess.telegram_user_id == 101))
        outsider = session.scalar(select(UserAccess).where(UserAccess.telegram_user_id == 202))
        assert owner.status == "active"
        assert json.loads(owner.allowed_tools_json) == ["bitrix:get_deal"]
        assert outsider.status == "pending"


def test_local_runtime_polls_ingests_and_processes(runtime):
    class FakePollingTelegram(InMemoryTelegramClient):
        updates = [telegram_update(100, 303, "/start")]
        webhook_deleted = False

        def get_me(self):
            return {"id": 1, "username": "local_test_bot"}

        def delete_webhook(self, *, drop_pending_updates: bool):
            self.webhook_deleted = True

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
    )
    local = LocalBotRuntime(runtime["settings"], runtime["factory"], telegram, processor)

    assert local.start()["username"] == "local_test_bot"
    assert telegram.webhook_deleted is True
    assert local.poll_once() == 1
    assert local.offset == 101
    assert "Доступ активен" in telegram.messages[-1][1]


def test_revocation_is_rechecked_by_worker(runtime, post_update, drain):
    onboard_and_approve(runtime, post_update, drain)
    post_update(telegram_update(2, 101, "Этот запрос уже в очереди"))
    response = runtime["client"].post(
        "/admin/users/101/revoke",
        headers=auth_header(),
        data={"csrf": csrf_token(runtime["settings"])},
        follow_redirects=False,
    )
    assert response.status_code == 303
    drain()
    assert runtime["agent"].calls == 0
    with runtime["factory"]() as session:
        user = session.scalar(select(UserAccess).where(UserAccess.telegram_user_id == 101))
        audit = session.scalar(select(AdminAudit).where(AdminAudit.action == "revoke"))
        assert user.status == "revoked"
        assert audit is not None


def test_delivery_retry_does_not_repeat_openai_or_apply_spent_limit(
    runtime,
    post_update,
    drain,
):
    onboard_and_approve(runtime, post_update, drain)
    with runtime["factory"]() as session:
        user = session.scalar(select(UserAccess).where(UserAccess.telegram_user_id == 101))
        user.daily_request_limit = 1
        session.commit()

    class FailOnceTelegram(InMemoryTelegramClient):
        failed = False

        def send_message(self, chat_id: int, text: str) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary Telegram failure")
            super().send_message(chat_id, text)

    telegram = FailOnceTelegram()
    runtime["settings"].max_job_attempts = 2
    processor = JobProcessor(
        runtime["settings"],
        runtime["factory"],
        runtime["agent"],
        telegram,
        runtime["processor"].metrics,
    )
    post_update(telegram_update(2, 101, "Покажи клиента"))
    assert processor.run_once() is True
    assert runtime["agent"].calls == 1
    with runtime["factory"]() as session:
        job = session.scalar(select(UpdateJob).where(UpdateJob.update_id == 2))
        job.available_at = datetime.now(UTC)
        session.commit()
    assert processor.run_once() is True
    assert runtime["agent"].calls == 1
    assert telegram.messages[-1][1].startswith("Демо-ответ")


def test_cost_formula_separates_cached_input(runtime):
    usage = AgentUsage(input_tokens=1000, cached_input_tokens=400, output_tokens=250)
    expected = (600 * 2.0 + 400 * 0.2 + 250 * 12.0) / 1_000_000
    assert calculate_cost_usd(usage, runtime["settings"]) == round(expected, 8)


def test_mcp_command_forces_selected_server(runtime, post_update, drain):
    onboard_and_approve(runtime, post_update, drain)
    response = post_update(telegram_update(2, 101, "/mcp jira покажи SH-501"))
    assert response.status_code == 200
    drain()
    assert runtime["agent"].last_required_mcp_server == "jira"
    assert runtime["agent"].last_messages[-1]["content"] == "покажи SH-501"


def test_mcp_tools_are_intersected_with_user_allowlist(monkeypatch):
    monkeypatch.setenv("BITRIX_TOKEN_FOR_TEST", "test-token")
    settings = Settings(
        app_env="test",
        openai_api_key="test-key",
        project_dir="config/project",
        mcp_servers_json=json.dumps(
            [
                {
                    "server_label": "bitrix",
                    "server_url": "https://example.org/mcp",
                    "authorization_env": "BITRIX_TOKEN_FOR_TEST",
                    "allowed_tools": ["get_deal", "update_deal"],
                    "read_only": True,
                }
            ]
        ),
    )
    client = OpenAIAgentClient(settings)
    tools = client._tools(["bitrix:get_deal", "unknown"])
    assert tools[0]["allowed_tools"] == ["get_deal"]
    assert tools[0]["authorization"] == "test-token"


def test_required_mcp_server_is_rejected_without_access_token():
    settings = Settings(
        app_env="test",
        openai_api_key="test-key",
        project_dir="config/project",
        mcp_servers_json=json.dumps(
            [
                {
                    "server_label": "jira",
                    "server_url": "https://example.org/mcp",
                    "authorization_env": "MISSING_JIRA_TOKEN_FOR_TEST",
                    "allowed_tools": ["jira_get_issue"],
                    "read_only": True,
                }
            ]
        ),
    )
    client = OpenAIAgentClient(settings)
    try:
        client._tools(["jira:jira_get_issue"], required_mcp_server="jira")
    except McpUnavailableError as exc:
        assert "access token" in str(exc)
    else:
        raise AssertionError("MCP server without access token was accepted")


def test_non_read_only_mcp_server_is_rejected():
    settings = Settings(
        app_env="test",
        mcp_servers_json=json.dumps(
            [
                {
                    "server_label": "bitrix",
                    "server_url": "https://example.org/mcp",
                    "allowed_tools": ["update_deal"],
                    "read_only": False,
                }
            ]
        ),
    )
    try:
        settings.mcp_servers()
    except ValueError as exc:
        assert "read-only" in str(exc)
    else:
        raise AssertionError("Unsafe MCP configuration was accepted")


def test_checked_in_mcp_registry_contains_guarded_modus_connections():
    settings = Settings(
        app_env="test",
        mcp_servers_file="config/mcp_servers.json",
    )
    servers = settings.mcp_servers()
    assert [server.server_label for server in servers] == ["jira", "bitrix", "ktalk"]
    assert all(server.authorization_type == "oauth" for server in servers)
    assert servers[0].allowed_tools == [
        "jira_get_issue",
        "jira_search",
        "jira_get_all_projects",
    ]
    assert servers[1].allowed_tools == []
    assert servers[2].allowed_tools == []
    assert all(server.read_only is True for server in servers)
