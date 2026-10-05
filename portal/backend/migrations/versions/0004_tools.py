"""Разборы документов и схемы SQL (docs/portal-api.md §9.9, §9.10).

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "docparses",
        sa.Column(
            "dialog_id", sa.Uuid, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("summary_status", sa.Text, nullable=False, server_default="streaming"),
        sa.Column("template_id", sa.Text, nullable=False),
        sa.Column("template_title", sa.Text, nullable=False),
        sa.Column("free_form", sa.Boolean, nullable=False),
        sa.Column("file_name", sa.Text, nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("page_count", sa.Integer),
        sa.Column("fields", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("document_text", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('processing', 'ready')", name="docparses_status_check"),
        sa.CheckConstraint(
            "summary_status IN ('streaming', 'complete', 'stopped', 'length_limit', 'error')",
            name="docparses_summary_status_check",
        ),
    )
    op.create_table(
        "sql_schemas",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "name", name="sql_schemas_owner_name_key"),
    )


def downgrade() -> None:
    op.drop_table("sql_schemas")
    op.drop_table("docparses")
