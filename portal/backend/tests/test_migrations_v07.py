"""Миграция `0008` на базе с данными версии 0.6 (docs/portal-api.md §9.22).

Так обновляется работающий сервис: `portal migrate` проводит базу 0.6 до головы. Прежние
диалоги всех видов остаются как были, все пары получают режим «Учитывать».
"""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from portal.core.settings import Settings
from tests.test_migrations_v05 import _run

_SEED = (
    "INSERT INTO users (id, login, full_name, role, password_hash, created_at) "
    "VALUES (:user, 'ivanov', 'Иванов', 'employee', 'x', now())",
    "INSERT INTO dialogs (id, owner_id, kind, created_at, updated_at) VALUES "
    "(:chat, :user, 'chat', now(), now()), (:sql, :user, 'sql', now(), now()), "
    "(:cogis, :user, 'cogis', now(), now()), (:docparse, :user, 'docparse', now(), now())",
    "INSERT INTO docparses (dialog_id, status, summary_status, template_id, template_title, "
    "free_form, file_name, media_type, storage_key, size_bytes, summary, document_text, "
    "created_at) VALUES (:docparse, 'ready', 'complete', 'egrn', 'Выписка', false, 'e.pdf', "
    "'application/pdf', 'docparse/1', 1, 'Кратко', 'Текст', now())",
    "INSERT INTO messages (id, dialog_id, position, role, content, status, created_at) VALUES "
    "(:question, :chat, 0, 'user', 'Вопрос', 'complete', now()), "
    "(gen_random_uuid(), :chat, 1, 'assistant', 'Ответ', 'complete', now()), "
    "(gen_random_uuid(), :sql, 0, 'user', 'Запрос', 'complete', now()), "
    "(gen_random_uuid(), :sql, 1, 'assistant', 'SELECT 1', 'complete', now()), "
    "(gen_random_uuid(), :cogis, 0, 'user', 'Плагин', 'complete', now()), "
    "(gen_random_uuid(), :cogis, 1, 'assistant', '', 'stopped', now()), "
    "(gen_random_uuid(), :docparse, 0, 'user', 'Срок?', 'complete', now()), "
    "(gen_random_uuid(), :docparse, 1, 'assistant', 'Год', 'complete', now())",
    "INSERT INTO attachments "
    "(id, dialog_id, message_id, file_name, media_type, size_bytes, storage_key, created_at) "
    "VALUES (gen_random_uuid(), :chat, :question, 'a.txt', 'text/plain', 1, "
    "'attachments/1', now())",
)


def test_previous_release_data_survives_and_every_pair_is_considered(settings: Settings) -> None:
    name = f"migration_{uuid4().hex}"
    asyncio.run(_run(settings, (f'CREATE DATABASE "{name}"',)))
    database = settings.database.model_copy(update={"name": name})
    target = settings.model_copy(update={"database": database})
    config = Config("alembic.ini")
    config.attributes["settings"] = target
    ids = {key: uuid4() for key in ("user", "chat", "sql", "cogis", "docparse", "question")}

    command.upgrade(config, "0007")
    asyncio.run(_run(target, _SEED, **ids))
    command.upgrade(config, "head")

    def query(statement: str, **params: Any) -> list[Any]:
        return asyncio.run(_run(target, (statement,), **params))

    assert query("SELECT version_num FROM alembic_version") == [("0008",)]
    assert query(
        "SELECT d.kind, m.position, m.role, m.content, m.status, m.memory_mode FROM messages m "
        "JOIN dialogs d ON d.id = m.dialog_id ORDER BY d.kind, m.position"
    ) == [
        ("chat", 0, "user", "Вопрос", "complete", "full"),
        ("chat", 1, "assistant", "Ответ", "complete", "full"),
        ("cogis", 0, "user", "Плагин", "complete", "full"),
        ("cogis", 1, "assistant", "", "stopped", "full"),
        ("docparse", 0, "user", "Срок?", "complete", "full"),
        ("docparse", 1, "assistant", "Год", "complete", "full"),
        ("sql", 0, "user", "Запрос", "complete", "full"),
        ("sql", 1, "assistant", "SELECT 1", "complete", "full"),
    ]
    assert query("SELECT message_id FROM attachments") == [(ids["question"],)]
    assert query("SELECT summary, document_text FROM docparses") == [("Кратко", "Текст")]
    with pytest.raises(sa.exc.IntegrityError):
        query("UPDATE messages SET memory_mode = 'pinned' WHERE id = :id", id=ids["question"])
