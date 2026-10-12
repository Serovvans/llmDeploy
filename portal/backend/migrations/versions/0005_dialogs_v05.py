"""Диалоги версии 0.15: итог проверки SQL, вложения-записи, параметры вопросов (§9.20).

Только добавление: новые столбцы допускают `NULL`, ни одна строка не удаляется.
Обратного хода нет.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

# Прежнее поле knowledge вопроса чата → набор коллекций (docs/portal-api.md §5.3).
# Вопрос без поля knowledge (такого быть не должно) получает пустой набор: без базы знаний.
_CHAT_PARAMS = """
UPDATE messages
SET params = (params - 'knowledge') || jsonb_build_object(
    'collections',
    CASE params ->> 'knowledge'
        WHEN 'shared' THEN '["shared"]'::jsonb
        WHEN 'shared_and_personal' THEN '["shared", "personal"]'::jsonb
        ELSE '[]'::jsonb
    END,
    'tags', '[]'::jsonb
)
FROM dialogs
WHERE dialogs.id = messages.dialog_id
  AND dialogs.kind = 'chat'
  AND messages.role = 'user'
  AND NOT messages.params ? 'collections'
"""

_SQL_PARAMS = """
UPDATE messages
SET params = params || '{"test_run": false}'::jsonb
FROM dialogs
WHERE dialogs.id = messages.dialog_id
  AND dialogs.kind = 'sql'
  AND messages.role = 'user'
  AND NOT messages.params ? 'test_run'
"""


def upgrade() -> None:
    op.add_column("messages", sa.Column("sql_run", postgresql.JSONB))
    op.add_column("attachments", sa.Column("duration_seconds", sa.Integer))
    op.add_column("attachments", sa.Column("transcription_status", sa.Text))
    op.add_column("attachments", sa.Column("transcription_error", sa.Text))
    op.create_check_constraint(
        "attachments_transcription_status_check",
        "attachments",
        "transcription_status IN ('queued', 'processing', 'ready', 'error')",
    )
    op.execute(_CHAT_PARAMS)
    op.execute(_SQL_PARAMS)


def downgrade() -> None:
    raise NotImplementedError(
        "Миграция 0005 без обратного хода: откат — восстановление базы и файлов из копии "
        "(docs/portal-design.md §8.2)"
    )
