"""Релиз 1.0.1: «все схемы» у подключения к базе заказчика (§9.29, §9.34).

Только добавление: `db_connections.all_schemas` с постоянным значением по умолчанию
`false`. Данные не меняются: прежние подключения остаются со списком схем, снимки — как
были. Обратного хода нет.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "db_connections",
        sa.Column("all_schemas", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Миграция 0012 без обратного хода: откат — образы 1.0 и база из резервной копии "
        "(docs/portal-api.md §9.34)."
    )
