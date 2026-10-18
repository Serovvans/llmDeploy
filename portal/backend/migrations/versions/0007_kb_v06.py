"""База знаний версии 0.16: версия описания картинки и новое описание до замены индекса (§9.21).

Только добавление: два столбца `kb_images`. Существующие строки получают
`description_version = 1` — описание составлено по правилам до 0.16 — и ищутся с прежним
описанием, пока документ не обработан заново. Обратного хода нет.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "kb_images",
        sa.Column("description_version", sa.SmallInteger, nullable=False, server_default="1"),
    )
    op.add_column("kb_images", sa.Column("pending_description", sa.Text))


def downgrade() -> None:
    raise NotImplementedError(
        "Миграция 0007 без обратного хода: откат — восстановление базы и файлов из копии "
        "(docs/portal-design.md §8.3)."
    )
