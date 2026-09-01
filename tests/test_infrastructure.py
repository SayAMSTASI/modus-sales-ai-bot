from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_local_compose_runs_postgres_and_polling_bot():
    compose = load_compose("docker-compose.local.yml")
    assert set(compose["services"]) == {"db", "bot"}
    assert compose["services"]["bot"]["command"] == "python -m app.local_bot"
    assert "ports" not in compose["services"]["db"]


def test_organization_compose_has_web_worker_and_healthchecks():
    compose = load_compose("docker-compose.yml")
    assert set(compose["services"]) == {"db", "web", "worker"}
    assert compose["services"]["web"]["healthcheck"]
    assert compose["services"]["db"]["healthcheck"]
    assert "ports" not in compose["services"]["db"]
    assert compose["services"]["worker"]["depends_on"]["web"]["condition"] == (
        "service_healthy"
    )


def test_secret_files_and_local_data_are_gitignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env.*" in gitignore
    assert "data/" in gitignore
    assert "secrets/" in gitignore


def test_production_image_contains_operational_scripts():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "scripts/check_keycloak_config.py" in dockerfile
    assert "scripts/register_webhook.py" in dockerfile


def test_webhook_registration_includes_admin_callbacks():
    script = (ROOT / "scripts" / "register_webhook.py").read_text(encoding="utf-8")
    assert '"allowed_updates": ["message", "callback_query"]' in script
