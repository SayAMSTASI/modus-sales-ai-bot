"""Add user question audit and conversation feedback.

Revision ID: 20260902_02
Revises: 20260902_01
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "20260902_02"
down_revision = "20260902_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "user_question_audit" not in existing:
        op.create_table(
            "user_question_audit",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("update_job_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("question_text", sa.Text(), nullable=False),
            sa.Column("scenario", sa.String(64), nullable=False),
            sa.Column("mcp_server", sa.String(64)),
            sa.Column("result", sa.String(32), nullable=False),
            sa.Column("request_id", sa.String(128)),
        )
        op.create_index(
            "ix_user_question_audit_update_job_id",
            "user_question_audit",
            ["update_job_id"],
        )
        op.create_index(
            "ix_user_question_audit_telegram_user_id",
            "user_question_audit",
            ["telegram_user_id"],
        )
        op.create_index(
            "ix_user_question_audit_asked_at",
            "user_question_audit",
            ["asked_at"],
        )
        op.create_index(
            "ix_user_question_audit_result",
            "user_question_audit",
            ["result"],
        )

    if "pending_conversation_feedback" not in existing:
        op.create_table(
            "pending_conversation_feedback",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "conversation_started_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column("question_count", sa.Integer(), nullable=False),
            sa.Column("answer_count", sa.Integer(), nullable=False),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_pending_conversation_feedback_telegram_user_id",
            "pending_conversation_feedback",
            ["telegram_user_id"],
        )
        op.create_index(
            "ix_pending_conversation_feedback_requested_at",
            "pending_conversation_feedback",
            ["requested_at"],
        )

    if "conversation_feedback" not in existing:
        op.create_table(
            "conversation_feedback",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("user_hash", sa.String(80), nullable=False),
            sa.Column("rating", sa.Integer(), nullable=False),
            sa.Column(
                "conversation_started_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column("question_count", sa.Integer(), nullable=False),
            sa.Column("answer_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_feedback_rating"),
        )
        op.create_index(
            "ix_conversation_feedback_telegram_user_id",
            "conversation_feedback",
            ["telegram_user_id"],
        )
        op.create_index(
            "ix_conversation_feedback_user_hash",
            "conversation_feedback",
            ["user_hash"],
        )
        op.create_index(
            "ix_conversation_feedback_rating",
            "conversation_feedback",
            ["rating"],
        )
        op.create_index(
            "ix_conversation_feedback_created_at",
            "conversation_feedback",
            ["created_at"],
        )


def downgrade() -> None:
    op.drop_table("conversation_feedback")
    op.drop_table("pending_conversation_feedback")
    op.drop_table("user_question_audit")
