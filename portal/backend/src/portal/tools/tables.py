"""Таблица схем SQL (docs/portal-api.md §9.10)."""

import sqlalchemy as sa

from portal.core.db import metadata

sql_schemas = sa.Table(
    "sql_schemas",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("owner_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("owner_id", "name", name="sql_schemas_owner_name_key"),
)
