"""Таблицы диалогов и разборов документов (docs/portal-api.md §9.6–9.9)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from portal.core.db import metadata

dialogs = sa.Table(
    "dialogs",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("owner_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("title", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

messages = sa.Table(
    "messages",
    metadata,
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
)

attachments = sa.Table(
    "attachments",
    metadata,
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

docparses = sa.Table(
    "docparses",
    metadata,
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
)
