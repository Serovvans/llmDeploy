"""Раздел «Код» 0.9: проекты, файлы, версии, сборка, правки помощника (§9.23–9.28).

Только добавление: новые таблицы; `dialogs.project_id` и `messages.code_edit` — `NULL`;
`CHECK` вида диалога заменяется более широким. Прежние строки годны без правки, данные не
меняются: диалоги `cogis` остаются как есть («только для чтения» — правило сервера при
включённом флаге, §7.5.15). Обратного хода нет.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "code_projects",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column(
            "packages", postgresql.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("schema_id", sa.Uuid, sa.ForeignKey("sql_schemas.id", ondelete="SET NULL")),
        sa.Column("revision", sa.BigInteger, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "code_projects_owner_name_key",
        "code_projects",
        ["owner_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index(
        "code_projects_owner_updated_idx",
        "code_projects",
        ["owner_id", sa.text("updated_at DESC"), "id"],
    )
    # Удаление схемы SQL обнуляет ссылку у проектов по этому индексу.
    op.create_index(
        "code_projects_schema_idx",
        "code_projects",
        ["schema_id"],
        postgresql_where=sa.text("schema_id IS NOT NULL"),
    )

    op.create_table(
        "code_files",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid,
            sa.ForeignKey("code_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "code_files_project_path_key",
        "code_files",
        ["project_id", sa.text("lower(path)")],
        unique=True,
    )

    op.create_table(
        "code_snapshots",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid,
            sa.ForeignKey("code_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("files", postgresql.JSONB, nullable=False),
        sa.Column("project_revision", sa.BigInteger, nullable=False),
        sa.Column("accepted_files", sa.Integer),
        sa.Column("restored_from", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reason IN ('edit_accepted', 'build_succeeded', 'restored', 'before_restore')",
            name="code_snapshots_reason_check",
        ),
        sa.UniqueConstraint(
            "project_id", "project_revision", name="code_snapshots_project_revision_key"
        ),
    )

    op.create_table(
        "code_builds",
        sa.Column(
            "project_id",
            sa.Uuid,
            sa.ForeignKey("code_projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("project_revision", sa.BigInteger, nullable=False),
        sa.Column("sdk_version", sa.Text),
        sa.Column("packages", postgresql.JSONB, nullable=False),
        sa.Column("diagnostics", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("diagnostics_truncated", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("rules", postgresql.JSONB),
        sa.Column("archive", sa.LargeBinary),
        sa.Column("archive_files", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'timeout')", name="code_builds_status_check"
        ),
        sa.CheckConstraint(
            "(archive IS NULL) = (archive_files IS NULL)", name="code_builds_archive_check"
        ),
    )

    op.add_column(
        "dialogs",
        sa.Column(
            "project_id",
            sa.Uuid,
            sa.ForeignKey("code_projects.id", ondelete="CASCADE", name="dialogs_project_fkey"),
        ),
    )
    op.create_unique_constraint("dialogs_project_id_key", "dialogs", ["project_id"])
    op.drop_constraint("dialogs_kind_check", "dialogs", type_="check")
    op.create_check_constraint(
        "dialogs_kind_check", "dialogs", "kind IN ('chat', 'sql', 'cogis', 'docparse', 'code')"
    )
    op.create_check_constraint(
        "dialogs_project_check", "dialogs", "(kind = 'code') = (project_id IS NOT NULL)"
    )

    op.add_column("messages", sa.Column("code_edit", postgresql.JSONB))
    # Перевод прежних правок диалога в `superseded` ищет их по этому индексу (§9.7).
    op.create_index(
        "messages_code_edit_proposed_idx",
        "messages",
        ["dialog_id"],
        postgresql_where=sa.text("code_edit->>'status' = 'proposed'"),
    )

    op.create_table(
        "code_edit_files",
        sa.Column(
            "message_id",
            sa.Uuid,
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("base_revision", sa.Integer),
        sa.Column("content", sa.Text, nullable=False),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Миграция 0010 без обратного хода: откат — выключить флаг code.enabled, а кода — "
        "восстановление базы и файлов из копии (docs/portal-design.md §8.5)."
    )
