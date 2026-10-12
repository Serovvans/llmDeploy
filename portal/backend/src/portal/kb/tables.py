"""Таблицы базы знаний и очереди заданий (docs/portal-api.md §9.11–9.14, §9.17–9.19)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from portal.core.db import metadata

kb_documents = sa.Table(
    "kb_documents",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("owner_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("is_cogis", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("media_type", sa.Text, nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("sha256", sa.LargeBinary, nullable=False),
    sa.Column("storage_key", sa.Text),
    sa.Column("page_count", sa.Integer),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.Column("pages_done", sa.Integer, nullable=False, server_default="0"),
    sa.Column("recognizing", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("deleted_at", sa.DateTime(timezone=True)),
    sa.Column("duration_seconds", sa.Integer),
    sa.Column("seconds_done", sa.Integer, nullable=False, server_default="0"),
    sa.Column("audio_key", sa.Text),
    sa.Column("images_truncated", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("index_version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("reprocess_error", sa.Text),
)

kb_document_tags = sa.Table(
    "kb_document_tags",
    metadata,
    sa.Column(
        "document_id",
        sa.Uuid,
        sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("tag_key", sa.Text, primary_key=True),
    sa.Column("tag", sa.Text, nullable=False),
)

kb_pages = sa.Table(
    "kb_pages",
    metadata,
    sa.Column(
        "document_id",
        sa.Uuid,
        sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("number", sa.Integer, primary_key=True),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("recognized", sa.Boolean, nullable=False),
    sa.Column("start_ms", sa.Integer),
    # Без отметок — SQL NULL, а не JSON null.
    sa.Column("marks", postgresql.JSONB(none_as_null=True)),
)

kb_images = sa.Table(
    "kb_images",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "document_id",
        sa.Uuid,
        sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("page_number", sa.Integer),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("sha256", sa.LargeBinary, nullable=False),
    sa.Column("storage_key", sa.Text, nullable=False),
    sa.Column("media_type", sa.Text, nullable=False),
    sa.Column("width", sa.Integer, nullable=False),
    sa.Column("height", sa.Integer, nullable=False),
    sa.Column("description", sa.Text),
)

kb_fragments = sa.Table(
    "kb_fragments",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("document_id", sa.Uuid, nullable=False),
    sa.Column("page_number", sa.Integer, nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("start_offset", sa.Integer, nullable=False),
    sa.Column("end_offset", sa.Integer, nullable=False),
    sa.Column("section", sa.Text),
    sa.Column("context", sa.Text),
    sa.Column("image_id", sa.Uuid, sa.ForeignKey("kb_images.id", ondelete="CASCADE")),
    sa.Column("start_ms", sa.Integer),
    sa.ForeignKeyConstraint(
        ["document_id", "page_number"],
        ["kb_pages.document_id", "kb_pages.number"],
        ondelete="CASCADE",
    ),
    sa.UniqueConstraint("document_id", "ordinal", name="kb_fragments_document_ordinal_key"),
)

kb_jobs = sa.Table(
    "kb_jobs",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("document_id", sa.Uuid, sa.ForeignKey("kb_documents.id", ondelete="CASCADE")),
    # Таблица модуля диалогов: задание `transcribe` — о вложении чата (§11.7).
    sa.Column("attachment_id", sa.Uuid),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("priority", sa.SmallInteger, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column(
        "run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column("locked_until", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("document_id", "kind", name="kb_jobs_document_kind_key"),
)

kb_queue_control = sa.Table(
    "kb_queue_control",
    metadata,
    sa.Column("id", sa.Boolean, primary_key=True, server_default=sa.true()),
    sa.Column("bulk_paused", sa.Boolean, nullable=False, server_default=sa.false()),
)
