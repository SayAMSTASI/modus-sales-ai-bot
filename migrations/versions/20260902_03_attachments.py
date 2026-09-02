"""Add encrypted outbound attachment retry payload.

Revision ID: 20260902_03
Revises: 20260902_02
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "20260902_03"
down_revision = "20260902_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("update_jobs")}
    if "response_attachments_encrypted" not in columns:
        op.add_column(
            "update_jobs",
            sa.Column("response_attachments_encrypted", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("update_jobs")}
    if "response_attachments_encrypted" in columns:
        op.drop_column("update_jobs", "response_attachments_encrypted")
