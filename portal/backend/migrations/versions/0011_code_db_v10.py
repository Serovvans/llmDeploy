"""Релиз 1.0: виды JS и HTML и подключения к базам заказчика (§9.29–9.33).

Только добавление: таблицы `db_connections`, `db_connection_users`, `db_snapshots`;
столбцы 1.0 у `dialogs`, `code_projects`, `code_files` — `NULL` или с постоянным значением
по умолчанию, с `CHECK` и частичными индексами. Данные не меняются: проекты 0.9 получают
`kind = 'gptool'` значением по умолчанию. Обратного хода нет.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None

_EMPTY_LIST = sa.text("'[]'::jsonb")


def _selection_columns(table: str) -> None:
    """Выбор подключения (§7.6.9): ссылка, название на момент выбора и таблицы."""
    op.add_column(
        table,
        sa.Column(
            "db_connection_id",
            sa.Uuid,
            sa.ForeignKey(
                "db_connections.id", ondelete="SET NULL", name=f"{table}_db_connection_fkey"
            ),
        ),
    )
    op.add_column(table, sa.Column("db_connection_title", sa.Text))
    op.add_column(
        table,
        sa.Column("db_tables", postgresql.JSONB, nullable=False, server_default=_EMPTY_LIST),
    )
    # Удаление подключения обнуляет ссылку по этому индексу.
    op.create_index(
        f"{table}_db_connection_idx",
        table,
        ["db_connection_id"],
        postgresql_where=sa.text("db_connection_id IS NOT NULL"),
    )


def upgrade() -> None:
    op.create_table(
        "db_connections",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("host", sa.Text, nullable=False),
        sa.Column("port", sa.Integer, nullable=False),
        sa.Column("database", sa.Text, nullable=False),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("password_encrypted", sa.LargeBinary, nullable=False),
        sa.Column("tls_mode", sa.Text, nullable=False),
        sa.Column("schemas", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("samples_allowed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "sample_exclusions", postgresql.JSONB, nullable=False, server_default=_EMPTY_LIST
        ),
        sa.Column("access_all", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("config_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rights_status", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("rights_findings", postgresql.JSONB, nullable=False, server_default=_EMPTY_LIST),
        sa.Column("rights_warnings", postgresql.JSONB, nullable=False, server_default=_EMPTY_LIST),
        sa.Column("rights_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="db_connections_port_check"),
        sa.CheckConstraint(
            "tls_mode IN ('verify', 'require', 'disable')", name="db_connections_tls_mode_check"
        ),
        sa.CheckConstraint(
            "rights_status IN ('unknown', 'read_only', 'excess')",
            name="db_connections_rights_status_check",
        ),
    )
    op.create_index(
        "db_connections_title_key", "db_connections", [sa.text("lower(title)")], unique=True
    )

    op.create_table(
        "db_connection_users",
        sa.Column(
            "connection_id",
            sa.Uuid,
            sa.ForeignKey("db_connections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    op.create_index("db_connection_users_user_idx", "db_connection_users", ["user_id"])

    op.create_table(
        "db_snapshots",
        sa.Column(
            "connection_id",
            sa.Uuid,
            sa.ForeignKey("db_connections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", postgresql.JSONB, nullable=False),
        sa.Column("tables", sa.Integer, nullable=False),
        sa.Column("truncated", sa.Boolean, nullable=False),
        sa.Column("postgis_schema", sa.Text),
    )

    _selection_columns("dialogs")
    op.create_check_constraint(
        "dialogs_db_connection_check", "dialogs", "kind = 'sql' OR db_connection_title IS NULL"
    )

    op.add_column(
        "code_projects",
        sa.Column("kind", sa.Text, nullable=False, server_default="gptool"),
    )
    op.create_check_constraint(
        "code_projects_kind_check",
        "code_projects",
        "kind IN ('gptool', 'js_plugin', 'html_widget', 'catalog', 'card_hooks')",
    )
    _selection_columns("code_projects")
    op.create_check_constraint(
        "code_projects_selection_check",
        "code_projects",
        "schema_id IS NULL OR db_connection_title IS NULL",
    )

    op.add_column("code_files", sa.Column("catalog_insertion", sa.Text))
    op.add_column("code_files", sa.Column("catalog_clients", postgresql.ARRAY(sa.Text)))
    op.create_check_constraint(
        "code_files_catalog_insertion_check",
        "code_files",
        "catalog_insertion IN ('head_start', 'head_end', 'body_start', 'body_end')",
    )
    op.create_check_constraint(
        "code_files_catalog_clients_check",
        "code_files",
        "catalog_clients <@ ARRAY['smartphone', 'tablet', 'desktop']::text[]"
        " AND cardinality(catalog_clients) > 0",
    )
    op.create_check_constraint(
        "code_files_catalog_check",
        "code_files",
        "(catalog_insertion IS NULL) = (catalog_clients IS NULL)",
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Миграция 0011 без обратного хода: откат — выключить флаги db.enabled, "
        "code.preview.enabled, code.enabled, а схемы — восстановление базы и файлов из копии "
        "(docs/portal-design.md §8.5)."
    )
