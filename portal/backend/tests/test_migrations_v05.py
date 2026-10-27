"""Миграции `0005` и `0006` подряд на базе с данными версии до 0.15 (docs/portal-api.md §9.20).

Так обновляется работающий сервис: один вызов `portal migrate` проводит обе ревизии.
Каждая миграция по отдельности проверена в `test_chat_knowledge.py` и
`test_kb_migration.py`; здесь — их стык на общей базе.
"""

import asyncio
import json
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from portal.core.db import database_url
from portal.core.settings import Settings

_SEED = (
    "INSERT INTO users (id, login, full_name, role, password_hash, created_at) "
    "VALUES (:user, 'ivanov', 'Иванов', 'employee', 'x', now())",
    "INSERT INTO dialogs (id, owner_id, kind, created_at, updated_at) "
    "VALUES (:chat, :user, 'chat', now(), now()), (:sql, :user, 'sql', now(), now())",
    "INSERT INTO messages (id, dialog_id, position, role, status, params, created_at) VALUES "
    "(:question, :chat, 0, 'user', 'complete', CAST(:chat_params AS jsonb), now()), "
    "(gen_random_uuid(), :chat, 1, 'assistant', 'complete', '{}', now()), "
    "(gen_random_uuid(), :sql, 0, 'user', 'complete', CAST(:sql_params AS jsonb), now())",
    "INSERT INTO attachments "
    "(id, dialog_id, message_id, file_name, media_type, size_bytes, storage_key, created_at) "
    "VALUES (gen_random_uuid(), :chat, :question, 'a.txt', 'text/plain', 1, "
    "'attachments/1', now())",
    "INSERT INTO kb_documents (id, owner_id, scope, title, media_type, size_bytes, sha256, "
    "storage_key, status, created_at) "
    "VALUES (:document, :user, 'shared', 'd.txt', 'text/plain', 1, '\\x01', 'kb/1', 'queued', "
    "now())",
    "INSERT INTO kb_jobs (id, document_id, kind, created_at) "
    "VALUES (gen_random_uuid(), :document, 'index', now())",
)
_CHAT_PARAMS = {"mode": "fast", "knowledge": "shared_and_personal"}
_SQL_PARAMS = {"action": "write", "dialect": "postgres", "schema_id": None}


async def _run(settings: Settings, statements: tuple[str, ...], **params: Any) -> list[Any]:
    """Выполнить операторы по порядку; вернуть строки последнего."""
    engine = create_async_engine(database_url(settings), isolation_level="AUTOCOMMIT")
    rows: list[Any] = []
    try:
        async with engine.connect() as connection:
            for statement in statements:
                result = await connection.execute(sa.text(statement), params)
                rows = [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()
    return rows


def test_both_migrations_in_one_run_keep_previous_version_data(settings: Settings) -> None:
    name = f"migration_{uuid4().hex}"
    asyncio.run(_run(settings, (f'CREATE DATABASE "{name}"',)))
    database = settings.database.model_copy(update={"name": name})
    target = settings.model_copy(update={"database": database})
    config = Config("alembic.ini")
    config.attributes["settings"] = target
    ids = {key: uuid4() for key in ("user", "chat", "sql", "question", "document")}

    command.upgrade(config, "0004")
    asyncio.run(
        _run(
            target,
            _SEED,
            chat_params=json.dumps(_CHAT_PARAMS),
            sql_params=json.dumps(_SQL_PARAMS),
            **ids,
        )
    )
    command.upgrade(config, "head")

    def query(statement: str) -> list[Any]:
        return asyncio.run(_run(target, (statement,)))

    assert query("SELECT version_num FROM alembic_version") == [("0011",)]
    assert query(
        "SELECT d.kind, m.role, m.params, m.sql_run FROM messages m "
        "JOIN dialogs d ON d.id = m.dialog_id ORDER BY d.kind, m.position"
    ) == [
        ("chat", "user", {"mode": "fast", "collections": ["shared", "personal"], "tags": []}, None),
        ("chat", "assistant", {}, None),
        ("sql", "user", {**_SQL_PARAMS, "test_run": False}, None),
    ]
    assert query(
        "SELECT storage_key, duration_seconds, transcription_status, transcription_error "
        "FROM attachments"
    ) == [("attachments/1", None, None, None)]
    assert query("SELECT status, storage_key, index_version FROM kb_documents") == [
        ("queued", "kb/1", 1)
    ]
    assert query("SELECT kind, priority, attachment_id FROM kb_jobs") == [("index", 20, None)]
