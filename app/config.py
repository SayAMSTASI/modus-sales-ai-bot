from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpServerConfig(BaseModel):
    server_label: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    server_description: str = ""
    server_url: str
    authorization_env: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    read_only: bool = False
    authorization_type: str | None = None
    oauth_issuer: str | None = None
    oauth_device_authorization_endpoint: str | None = None
    oauth_token_endpoint: str | None = None
    oauth_client_id_env: str | None = None
    app_id: str | None = None
    app_version_id: str | None = None
    app_version_name: str | None = None
    app_version_notes: str | None = None
    review_status: str | None = None
    connected_at: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./data/sales_bot.db"
    public_base_url: str = "http://localhost:8000"

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = "dev-webhook-secret"
    telegram_poll_timeout_seconds: int = 20
    telegram_drop_pending_updates: bool = False
    local_auto_approve_first_user: bool = True
    local_owner_telegram_user_id: int | None = None
    admin_username: str = "access_admin"
    admin_password: str = "dev-admin-password"
    safety_identifier_secret: str = "dev-safety-secret"
    openai_api_key: str = ""

    agent_backend: Literal["mock", "openai"] = "mock"
    openai_model: str = "gpt-5.6-luna"
    openai_prompt_id: str = ""
    openai_prompt_version: str = ""
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "low"
    openai_max_output_tokens: int = 1200
    openai_input_usd_per_mtok: float = 2.0
    openai_cached_input_usd_per_mtok: float = 0.2
    openai_output_usd_per_mtok: float = 12.0

    context_ttl_hours: int = 24
    context_max_messages: int = 12
    context_max_chars: int = 24_000
    max_message_chars: int = 12_000
    job_payload_ttl_hours: int = 24
    max_job_attempts: int = 3
    worker_poll_seconds: float = 1.0
    global_daily_request_limit: int = 1000
    global_daily_cost_limit_usd: float = 50.0

    mcp_servers_json: str = ""
    mcp_servers_file: Path = Path("config/mcp_servers.json")
    project_dir: Path = Path("config/project")

    @field_validator("database_url")
    @classmethod
    def normalize_postgres_scheme(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        return value

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        if self.app_env != "production":
            return self
        values = {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_WEBHOOK_SECRET": self.telegram_webhook_secret,
            "ADMIN_PASSWORD": self.admin_password,
            "SAFETY_IDENTIFIER_SECRET": self.safety_identifier_secret,
            "OPENAI_API_KEY": self.openai_api_key if self.agent_backend == "openai" else "mock",
        }
        weak = [
            name
            for name, value in values.items()
            if not value or value.startswith("dev-") or "replace" in value
        ]
        if weak:
            raise ValueError(f"Production secrets are missing or weak: {', '.join(weak)}")
        return self

    def mcp_servers(self) -> list[McpServerConfig]:
        if self.mcp_servers_json.strip():
            raw: Any = json.loads(self.mcp_servers_json)
            source = "MCP_SERVERS_JSON"
        elif self.mcp_servers_file.is_file():
            raw = json.loads(self.mcp_servers_file.read_text(encoding="utf-8"))
            source = str(self.mcp_servers_file)
        else:
            raw = []
            source = "default"
        if not isinstance(raw, list):
            raise ValueError(f"MCP server configuration must be a JSON array: {source}")
        servers = [McpServerConfig.model_validate(item) for item in raw]
        unsafe = [item.server_label for item in servers if not item.read_only]
        if unsafe:
            raise ValueError(f"Pilot only accepts read-only MCP servers: {', '.join(unsafe)}")
        return servers


@lru_cache
def get_settings() -> Settings:
    return Settings()
