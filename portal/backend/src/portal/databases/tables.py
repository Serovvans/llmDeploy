"""Таблицы подключений к базам заказчика (docs/portal-api.md §9.29–9.31)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from portal.core.db import metadata

db_connections = sa.Table(
    "db_connections",
    metadata,
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
        "sample_exclusions", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")
    ),
    sa.Column("access_all", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("config_changed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("rights_status", sa.Text, nullable=False, server_default="unknown"),
    sa.Column("rights_findings", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
    sa.Column("rights_warnings", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
    sa.Column("rights_checked_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", postgresql.JSONB(none_as_null=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

db_connection_users = sa.Table(
    "db_connection_users",
    metadata,
    sa.Column(
        "connection_id",
        sa.Uuid,
        sa.ForeignKey("db_connections.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)

db_snapshots = sa.Table(
    "db_snapshots",
    metadata,
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
