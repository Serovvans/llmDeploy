"""Таблицы базы знаний и очереди заданий (docs/portal-api.md §9.11–9.14).

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("is_cogis", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.LargeBinary, nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("page_count", sa.Integer),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text),
        sa.Column("pages_done", sa.Integer, nullable=False, server_default="0"),
        sa.Column("recognizing", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("scope IN ('shared', 'personal')", name="kb_documents_scope_check"),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'ready', 'error')",
            name="kb_documents_status_check",
        ),
        sa.CheckConstraint("NOT is_cogis OR scope = 'shared'", name="kb_documents_cogis_check"),
    )
    # Повтор файла ищется только внутри коллекции, куда добавляют (§8.1).
    op.create_index(
        "ux_kb_documents_shared_sha256",
        "kb_documents",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("scope = 'shared' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ux_kb_documents_personal_sha256",
        "kb_documents",
        ["owner_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("scope = 'personal' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_kb_documents_scope_created", "kb_documents", ["scope", sa.text("created_at DESC")]
    )
    op.create_index(
        "ix_kb_documents_owner_created", "kb_documents", ["owner_id", sa.text("created_at DESC")]
    )
    op.create_table(
        "kb_pages",
        sa.Column(
            "document_id",
            sa.Uuid,
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("number", sa.Integer, primary_key=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("recognized", sa.Boolean, nullable=False),
    )
    op.create_table(
        "kb_fragments",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("document_id", sa.Uuid, nullable=False),
        sa.Column("page_number", sa.Integer, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("start_offset", sa.Integer, nullable=False),
        sa.Column("end_offset", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["kb_pages.document_id", "kb_pages.number"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("document_id", "ordinal", name="kb_fragments_document_ordinal_key"),
    )
    op.create_table(
        "kb_jobs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid,
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "kind", name="kb_jobs_document_kind_key"),
        sa.CheckConstraint("kind IN ('index', 'delete')", name="kb_jobs_kind_check"),
    )
    op.create_index("ix_kb_jobs_run_after", "kb_jobs", ["run_after"])


def downgrade() -> None:
    op.drop_table("kb_jobs")
    op.drop_table("kb_fragments")
    op.drop_table("kb_pages")
    op.drop_table("kb_documents")
