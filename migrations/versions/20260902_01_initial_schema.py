"""Initial pilot schema.

Revision ID: 20260902_01
Revises:
Create Date: 2026-09-02
"""

from collections.abc import Callable

import sqlalchemy as sa
from alembic import op

revision = "20260902_01"
down_revision = None
branch_labels = None
depends_on = None


def _create(name: str, operation: Callable[[], None], existing: set[str]) -> None:
    if name not in existing:
        operation()


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    _create(
        "user_access",
        lambda: op.create_table(
            "user_access",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("telegram_username", sa.String(128)),
            sa.Column("corporate_name", sa.String(255)),
            sa.Column("corporate_email", sa.String(255)),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("role", sa.String(64), nullable=False),
            sa.Column("allowed_tools_json", sa.Text(), nullable=False),
            sa.Column("request_number", sa.String(32), nullable=False, unique=True),
            sa.Column("daily_request_limit", sa.Integer(), nullable=False),
            sa.Column("daily_token_limit", sa.Integer(), nullable=False),
            sa.Column("daily_cost_limit_usd", sa.Float(), nullable=False),
            sa.Column("approved_by", sa.String(128)),
            sa.Column("approved_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        ),
        existing,
    )
    _create(
        "update_jobs",
        lambda: op.create_table(
            "update_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("update_id", sa.BigInteger(), unique=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("payload_text", sa.Text()),
            sa.Column("response_text", sa.Text()),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("error_code", sa.String(128)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
        ),
        existing,
    )
    _create(
        "conversation_messages",
        lambda: op.create_table(
            "conversation_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
        existing,
    )
    _create(
        "usage_events",
        lambda: op.create_table(
            "usage_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("user_hash", sa.String(80), nullable=False),
            sa.Column("request_id", sa.String(128), nullable=False, unique=True),
            sa.Column("scenario", sa.String(64), nullable=False),
            sa.Column("result", sa.String(32), nullable=False),
            sa.Column("duration_ms", sa.Integer(), nullable=False),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("input_tokens", sa.Integer(), nullable=False),
            sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
            sa.Column("output_tokens", sa.Integer(), nullable=False),
            sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        ),
        existing,
    )
    _create(
        "admin_audit",
        lambda: op.create_table(
            "admin_audit",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("admin_name", sa.String(128), nullable=False),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column("target_telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=False),
        ),
        existing,
    )
    _create(
        "oauth_credentials",
        lambda: op.create_table(
            "oauth_credentials",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("access_token_encrypted", sa.Text(), nullable=False),
            sa.Column("refresh_token_encrypted", sa.Text()),
            sa.Column("token_type", sa.String(32), nullable=False),
            sa.Column("scope", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("refresh_expires_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        ),
        existing,
    )
    _create(
        "oauth_device_sessions",
        lambda: op.create_table(
            "oauth_device_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("device_code_encrypted", sa.Text(), nullable=False),
            sa.Column("user_code", sa.String(255), nullable=False),
            sa.Column("verification_uri", sa.Text(), nullable=False),
            sa.Column("verification_uri_complete", sa.Text()),
            sa.Column("interval_seconds", sa.Integer(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
        existing,
    )
    _create(
        "oauth_authorization_sessions",
        lambda: op.create_table(
            "oauth_authorization_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("code_verifier_encrypted", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ),
        existing,
    )
    _create(
        "skill_versions",
        lambda: op.create_table(
            "skill_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("skill_name", sa.String(128), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("skill_name", "version", name="uq_skill_version"),
        ),
        existing,
    )
    _create(
        "skill_edit_sessions",
        lambda: op.create_table(
            "skill_edit_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("admin_telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("skill_name", sa.String(128), nullable=False),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("draft_content", sa.Text()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        ),
        existing,
    )

    indexes = {
        "user_access": ["telegram_user_id", "status"],
        "update_jobs": ["telegram_user_id", "chat_id", "status", "available_at"],
        "conversation_messages": ["telegram_user_id", "chat_id", "expires_at"],
        "usage_events": ["occurred_at", "user_hash"],
        "admin_audit": ["occurred_at"],
        "oauth_credentials": ["telegram_user_id", "expires_at"],
        "oauth_device_sessions": [
            "telegram_user_id",
            "expires_at",
            "next_poll_at",
        ],
        "oauth_authorization_sessions": [
            "state_hash",
            "telegram_user_id",
            "expires_at",
        ],
        "skill_versions": ["skill_name", "is_active"],
        "skill_edit_sessions": ["admin_telegram_user_id"],
    }
    for table_name, columns in indexes.items():
        if table_name in existing:
            continue
        for column in columns:
            op.create_index(f"ix_{table_name}_{column}", table_name, [column])


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in [
        "skill_edit_sessions",
        "skill_versions",
        "oauth_authorization_sessions",
        "oauth_device_sessions",
        "oauth_credentials",
        "admin_audit",
        "usage_events",
        "conversation_messages",
        "update_jobs",
        "user_access",
    ]:
        if table_name in existing:
            op.drop_table(table_name)
