"""Таблицы раздела «Код» (docs/portal-api.md §9.23–9.27)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from portal.core.db import metadata

code_projects = sa.Table(
    "code_projects",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("owner_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("packages", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("schema_id", sa.Uuid, sa.ForeignKey("sql_schemas.id", ondelete="SET NULL")),
    sa.Column("revision", sa.BigInteger, nullable=False, server_default="1"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    # С 1.0 (§9.23): вид проекта и выбор подключения (§7.6.9).
    sa.Column("kind", sa.Text, nullable=False, server_default="gptool"),
    sa.Column("db_connection_id", sa.Uuid, sa.ForeignKey("db_connections.id", ondelete="SET NULL")),
    sa.Column("db_connection_title", sa.Text),
    sa.Column("db_tables", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
)

code_files = sa.Table(
    "code_files",
    metadata,
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
    # С 1.0 (§9.24): место вставки и клиенты файла кастомизации Каталога.
    sa.Column("catalog_insertion", sa.Text),
    sa.Column("catalog_clients", postgresql.ARRAY(sa.Text)),
)

code_snapshots = sa.Table(
    "code_snapshots",
    metadata,
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
)

code_builds = sa.Table(
    "code_builds",
    metadata,
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
    sa.Column("rules", postgresql.JSONB(none_as_null=True)),
    sa.Column("archive", sa.LargeBinary),
    sa.Column("archive_files", postgresql.JSONB(none_as_null=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

code_edit_files = sa.Table(
    "code_edit_files",
    metadata,
    sa.Column(
        "message_id", sa.Uuid, sa.ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("path", sa.Text, primary_key=True),
    sa.Column("base_revision", sa.Integer),
    sa.Column("content", sa.Text, nullable=False),
)
