"""Диалоги версии 0.8: пересказ и перенесённое в памяти диалога (§9.6, §9.22).

Только добавление: все новые столбцы `dialogs` — `NULL`, прежние строки годны без правки,
данные не меняются. Удаление диалога-источника переноса обнуляет только ссылку на него
(`ON DELETE SET NULL`). Обратного хода нет.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("dialogs", sa.Column("summary", sa.Text))
    op.add_column("dialogs", sa.Column("summary_until_position", sa.Integer))
    op.create_check_constraint(
        "dialogs_summary_check",
        "dialogs",
        "(summary IS NULL) = (summary_until_position IS NULL)",
    )
    op.add_column("dialogs", sa.Column("carried_text", sa.Text))
    op.add_column(
        "dialogs",
        sa.Column(
            "carried_source_id",
            sa.Uuid,
            sa.ForeignKey("dialogs.id", ondelete="SET NULL", name="dialogs_carried_source_fkey"),
        ),
    )
    op.add_column("dialogs", sa.Column("carried_source_title", sa.Text))
    # Удаление источника ищет перенесённые из него диалоги по этому индексу.
    op.create_index(
        "dialogs_carried_source_idx",
        "dialogs",
        ["carried_source_id"],
        postgresql_where=sa.text("carried_source_id IS NOT NULL"),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Миграция 0009 без обратного хода: откат — выключить флаг memory.enabled, а кода — "
        "восстановление базы и файлов из копии (docs/portal-design.md §8.4)."
    )
