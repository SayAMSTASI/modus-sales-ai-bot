from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.agent import MockAgentClient
from app.config import Settings
from app.main import create_app
from app.metrics import NullMetricsExporter
from app.oauth import (
    AuthorizationCodeStart,
    DeviceAuthorization,
    OAuthTokenStore,
    TokenPollResult,
    TokenResponse,
)
from app.telegram import InMemoryTelegramClient
from app.worker import JobProcessor


class FakeOAuthClient:
    def __init__(self) -> None:
        self.poll_result = TokenPollResult(state="pending", next_interval=5)
        self.refreshed_token = TokenResponse(
            access_token="refreshed-access",
            refresh_token="refreshed-refresh",
            token_type="Bearer",
            scope="openid profile email",
            expires_in=300,
            refresh_expires_in=3600,
        )
        self.revoked_tokens: list[str] = []
        self.authorization_code_exchanges: list[tuple[str, str]] = []

    def start_device_authorization(self) -> DeviceAuthorization:
        return DeviceAuthorization(
            device_code="secret-device-code",
            user_code="ABCD-EFGH",
            verification_uri="https://auth.example/device",
            verification_uri_complete="https://auth.example/device?user_code=ABCD-EFGH",
            expires_in=600,
            interval=5,
        )

    def poll_device_token(self, device_code: str, interval: int) -> TokenPollResult:
        assert device_code == "secret-device-code"
        return self.poll_result

    def start_authorization_code(self) -> AuthorizationCodeStart:
        return AuthorizationCodeStart(
            authorization_url=(
                "https://auth.example/authorize?response_type=code&state=test-state"
                "&code_challenge=test-challenge&code_challenge_method=S256"
            ),
            state="test-state",
            code_verifier="secret-code-verifier",
            expires_in=600,
        )

    def exchange_authorization_code(self, code: str, verifier: str) -> TokenResponse:
        self.authorization_code_exchanges.append((code, verifier))
        return TokenResponse(
            access_token="code-access",
            refresh_token="code-refresh",
            token_type="Bearer",
            scope="openid profile email offline_access",
            expires_in=300,
            refresh_expires_in=3600,
        )

    def refresh(self, refresh_token: str) -> TokenResponse:
        assert refresh_token == "secret-refresh"
        return self.refreshed_token

    def revoke(self, token: str) -> None:
        self.revoked_tokens.append(token)


@pytest.fixture
def runtime(tmp_path: Path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        telegram_webhook_secret="test-webhook-secret",
        safety_identifier_secret="strong-test-safety-secret",
        admin_telegram_ids="900",
        pilot_telegram_ids="900",
        keycloak_client_id="test-public-client",
        token_encryption_key=Fernet.generate_key().decode(),
        project_dir=Path("config/project"),
        max_job_attempts=1,
    )
    agent = MockAgentClient()
    telegram = InMemoryTelegramClient()
    oauth_client = FakeOAuthClient()
    oauth_store = OAuthTokenStore(settings, oauth_client)  # type: ignore[arg-type]
    app = create_app(settings, oauth_store)
    processor = JobProcessor(
        settings,
        app.state.session_factory,
        agent,
        telegram,
        NullMetricsExporter(),
        oauth_store,
    )
    with TestClient(app) as client:
        yield {
            "settings": settings,
            "app": app,
            "client": client,
            "agent": agent,
            "telegram": telegram,
            "processor": processor,
            "factory": app.state.session_factory,
            "oauth_client": oauth_client,
            "oauth_store": oauth_store,
        }


def telegram_update(
    update_id: int,
    user_id: int,
    text: str | None,
    chat_type: str = "private",
):
    message = {
        "message_id": update_id,
        "from": {"id": user_id, "is_bot": False, "username": "pilot_user"},
        "chat": {"id": user_id if chat_type == "private" else -100, "type": chat_type},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id, "is_bot": False},
            "message": {
                "message_id": update_id,
                "chat": {"id": user_id, "type": "private"},
            },
            "data": data,
        },
    }


@pytest.fixture
def post_update(runtime):
    def post(update):
        return runtime["client"].post(
            "/telegram/webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
        )

    return post


@pytest.fixture
def drain(runtime):
    def run():
        count = 0
        while runtime["processor"].run_once():
            count += 1
            if count > 100:
                raise AssertionError("Worker did not drain")
        return count

    return run
