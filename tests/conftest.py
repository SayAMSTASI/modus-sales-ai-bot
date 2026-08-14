from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent import MockAgentClient
from app.config import Settings
from app.main import create_app
from app.metrics import NullMetricsExporter
from app.telegram import InMemoryTelegramClient
from app.worker import JobProcessor


@pytest.fixture
def runtime(tmp_path: Path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        telegram_webhook_secret="test-webhook-secret",
        admin_username="admin",
        admin_password="strong-test-password",
        safety_identifier_secret="strong-test-safety-secret",
        project_dir=Path("config/project"),
        max_job_attempts=1,
    )
    app = create_app(settings)
    agent = MockAgentClient()
    telegram = InMemoryTelegramClient()
    processor = JobProcessor(
        settings,
        app.state.session_factory,
        agent,
        telegram,
        NullMetricsExporter(),
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
        }


def telegram_update(update_id: int, user_id: int, text: str | None, chat_type: str = "private"):
    message = {
        "message_id": update_id,
        "from": {"id": user_id, "is_bot": False, "username": "pilot_user"},
        "chat": {"id": user_id if chat_type == "private" else -100, "type": chat_type},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


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

