"""База знаний версии 0.15: «Министерства», теги, картинки, записи, очередь (§9.20).

Только добавление: новые таблицы и столбцы, снятие `NOT NULL`, замена `CHECK` более
широкими. Существующие строки годны без правки, документы не переобрабатываются.
Обратного хода нет.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def _documents() -> None:
    op.drop_constraint("kb_documents_scope_check", "kb_documents", type_="check")
    op.create_check_constraint(
        "kb_documents_scope_check",
        "kb_documents",
        "scope IN ('shared', 'personal', 'ministries')",
    )
    op.create_index(
        "ux_kb_documents_ministries_sha256",
        "kb_documents",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("scope = 'ministries' AND deleted_at IS NULL"),
    )
    # NULL — только у записи, исходный файл которой удалён (§8.7).
    op.alter_column("kb_documents", "storage_key", nullable=True)
    op.add_column("kb_documents", sa.Column("duration_seconds", sa.Integer))
    op.add_column(
        "kb_documents", sa.Column("seconds_done", sa.Integer, nullable=False, server_default="0")
    )
    op.add_column("kb_documents", sa.Column("audio_key", sa.Text))
    op.add_column(
        "kb_documents",
        sa.Column("images_truncated", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    # 1 — индекс построен порядком обработки до 0.15: таковы все существующие документы.
    op.add_column(
        "kb_documents", sa.Column("index_version", sa.Integer, nullable=False, server_default="1")
    )
    op.add_column("kb_documents", sa.Column("reprocess_error", sa.Text))


def _tags() -> None:
    op.create_table(
        "kb_document_tags",
        sa.Column(
            "document_id",
            sa.Uuid,
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tag_key", sa.Text, primary_key=True),
        sa.Column("tag", sa.Text, nullable=False),
    )
    op.create_index("ix_kb_document_tags_tag_key", "kb_document_tags", ["tag_key"])


def _images() -> None:
    op.create_table(
        "kb_images",
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
        sa.CheckConstraint(
            "media_type IN ('image/png', 'image/jpeg')", name="kb_images_media_type_check"
        ),
    )
    op.create_index("ix_kb_images_document", "kb_images", ["document_id"])


def _pages_and_fragments() -> None:
    op.add_column("kb_pages", sa.Column("start_ms", sa.Integer))
    op.add_column("kb_pages", sa.Column("marks", postgresql.JSONB))
    op.add_column("kb_fragments", sa.Column("section", sa.Text))
    op.add_column("kb_fragments", sa.Column("context", sa.Text))
    op.add_column(
        "kb_fragments",
        sa.Column("image_id", sa.Uuid, sa.ForeignKey("kb_images.id", ondelete="CASCADE")),
    )
    op.add_column("kb_fragments", sa.Column("start_ms", sa.Integer))


def _queue() -> None:
    control = op.create_table(
        "kb_queue_control",
        sa.Column("id", sa.Boolean, primary_key=True, server_default=sa.true()),
        sa.Column("bulk_paused", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.CheckConstraint("id", name="kb_queue_control_single_row_check"),
    )
    op.bulk_insert(control, [{"id": True, "bulk_paused": False}])

    op.add_column(
        "kb_jobs",
        sa.Column("attachment_id", sa.Uuid, sa.ForeignKey("attachments.id", ondelete="CASCADE")),
    )
    op.create_index(
        "ux_kb_jobs_attachment",
        "kb_jobs",
        ["attachment_id"],
        unique=True,
        postgresql_where=sa.text("attachment_id IS NOT NULL"),
    )
    op.alter_column("kb_jobs", "document_id", nullable=True)
    op.create_check_constraint(
        "kb_jobs_subject_check", "kb_jobs", "num_nonnulls(document_id, attachment_id) = 1"
    )
    op.drop_constraint("kb_jobs_kind_check", "kb_jobs", type_="check")
    op.create_check_constraint(
        "kb_jobs_kind_check",
        "kb_jobs",
        "kind IN ('index', 'delete', 'context', 'reprocess', 'transcribe')",
    )
    # Существующие задания — только index и delete: 20 и 0 (§11). Значение по умолчанию
    # нужно лишь на время заполнения: приоритет всегда задаёт приложение.
    op.add_column(
        "kb_jobs", sa.Column("priority", sa.SmallInteger, nullable=False, server_default="20")
    )
    op.execute("UPDATE kb_jobs SET priority = 0 WHERE kind = 'delete'")
    op.alter_column("kb_jobs", "priority", server_default=None)
    op.create_index("ix_kb_jobs_priority_created", "kb_jobs", ["priority", "created_at"])


def upgrade() -> None:
    _documents()
    _tags()
    _images()
    _pages_and_fragments()
    _queue()


def downgrade() -> None:
    raise NotImplementedError(
        "Миграция 0006 без обратного хода: откат — восстановление базы и файлов из копии "
        "(docs/portal-design.md §8.2)."
    )
