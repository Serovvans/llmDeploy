"""Таблицы диалогов, сообщений и вложений (docs/portal-api.md §9.6–9.8).

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "dialogs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('chat', 'sql', 'cogis', 'docparse')", name="dialogs_kind_check"
        ),
    )
    op.create_index(
        "ix_dialogs_owner_kind_updated",
        "dialogs",
        ["owner_id", "kind", sa.text("updated_at DESC"), "id"],
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "dialog_id", sa.Uuid, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text),
        sa.Column("reasoning", sa.Text),
        sa.Column("reasoning_seconds", sa.Integer),
        sa.Column("params", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("sources", postgresql.JSONB),
        sa.Column("sources_found", sa.Integer),
        sa.Column("sql_check", postgresql.JSONB),
        sa.Column("sql_dangers", postgresql.ARRAY(sa.Text)),
        sa.Column("dropped_messages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dialog_id", "position", name="messages_dialog_position_key"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
        sa.CheckConstraint(
            "status IN ('complete', 'streaming', 'stopped', 'length_limit', 'error')",
            name="messages_status_check",
        ),
    )
    op.create_table(
        "attachments",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "dialog_id", sa.Uuid, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("message_id", sa.Uuid, sa.ForeignKey("messages.id", ondelete="CASCADE")),
        sa.Column("file_name", sa.Text, nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("page_count", sa.Integer),
        sa.Column("text_content", sa.Text),
        sa.Column("image_pages", postgresql.ARRAY(sa.Integer), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_attachments_dialog_id", "attachments", ["dialog_id"])
    op.create_index("ix_attachments_message_id", "attachments", ["message_id"])


def downgrade() -> None:
    op.drop_table("attachments")
    op.drop_table("messages")
    op.drop_table("dialogs")
