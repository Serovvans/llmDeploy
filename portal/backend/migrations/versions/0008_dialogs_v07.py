"""Диалоги версии 0.7: режим пары «вопрос + ответ» в памяти диалога (§9.7, §9.22).

Только добавление: столбец `messages.memory_mode` с постоянным значением по умолчанию.
Прежние строки получают `full` — все пары «Учитывать», данные не меняются. Обратного
хода нет.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "messages", sa.Column("memory_mode", sa.Text, nullable=False, server_default="full")
    )
    op.create_check_constraint(
        "messages_memory_mode_check",
        "messages",
        "memory_mode IN ('full', 'text_only', 'excluded')",
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Миграция 0008 без обратного хода: откат — выключить флаг memory.enabled, а кода — "
        "восстановление базы и файлов из копии (docs/portal-design.md §8.4)."
    )
