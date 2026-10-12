"""Миграция `0006` на базе с данными версии до 0.15 (docs/portal-api.md §9.20).

Сервис обновляется с данными: существующие документы, фрагменты и задания после миграции
годны без правки, ни одна строка не теряется.
"""

import asyncio
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from portal.core.db import database_url
from portal.core.settings import Settings

_SEED = """
INSERT INTO users (id, login, full_name, role, password_hash, created_at)
VALUES (:user, 'ivanov', 'Иванов', 'employee', 'x', now());
INSERT INTO kb_documents
    (id, owner_id, scope, title, media_type, size_bytes, sha256, storage_key, status, created_at)
VALUES
    (:ready, :user, 'shared', 'готовый.txt', 'text/plain', 1, '\\x01', 'kb/1', 'ready', now()),
    (:gone, :user, 'personal', 'удалённый.txt', 'text/plain', 1, '\\x02', 'kb/2', 'ready', now());
UPDATE kb_documents SET deleted_at = now() WHERE id = :gone;
INSERT INTO kb_pages (document_id, number, text, recognized) VALUES (:ready, 1, 'текст', false);
INSERT INTO kb_fragments (id, document_id, page_number, ordinal, start_offset, end_offset)
VALUES (:fragment, :ready, 1, 0, 0, 5);
INSERT INTO kb_jobs (id, document_id, kind, created_at)
VALUES (:index_job, :ready, 'index', now()), (:delete_job, :gone, 'delete', now());
"""


async def _run(settings: Settings, statements: str, **params: Any) -> list[Sequence[Any]]:
    """Выполнить операторы по одному; вернуть строки последнего, если он их отдаёт."""
    engine = create_async_engine(database_url(settings), isolation_level="AUTOCOMMIT")
    rows: list[Sequence[Any]] = []
    try:
        async with engine.connect() as connection:
            for statement in filter(None, (part.strip() for part in statements.split(";"))):
                result = await connection.execute(sa.text(statement), params)
                rows = [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()
    return rows


def test_migration_keeps_existing_documents_and_jobs_usable(settings: Settings) -> None:
    name = f"migration_{uuid4().hex}"
    asyncio.run(_run(settings, f'CREATE DATABASE "{name}"'))
    database = settings.database.model_copy(update={"name": name})
    target = settings.model_copy(update={"database": database})
    config = Config("alembic.ini")
    config.attributes["settings"] = target
    ids = {key: uuid4() for key in ("user", "ready", "gone", "fragment", "index_job", "delete_job")}

    command.upgrade(config, "0005")
    asyncio.run(_run(target, _SEED, **ids))
    command.upgrade(config, "head")

    def query(statement: str) -> list[Sequence[Any]]:
        return asyncio.run(_run(target, statement, **ids))

    assert query(
        "SELECT scope, storage_key, index_version, seconds_done, images_truncated, "
        "reprocess_error, audio_key FROM kb_documents ORDER BY storage_key"
    ) == [
        ("shared", "kb/1", 1, 0, False, None, None),
        ("personal", "kb/2", 1, 0, False, None, None),
    ]
    assert query("SELECT section, context, image_id, start_ms FROM kb_fragments") == [
        (None, None, None, None)
    ]
    assert query("SELECT kind, priority, attachment_id FROM kb_jobs ORDER BY priority") == [
        ("delete", 0, None),
        ("index", 20, None),
    ]
    assert query("SELECT id, bulk_paused FROM kb_queue_control") == [(True, False)]
    assert query("SELECT count(*) FROM kb_document_tags") == [(0,)]

    # Новое допустимо: третья коллекция со своим правилом повтора, запись без исходника.
    query(
        "INSERT INTO kb_documents (id, owner_id, scope, title, media_type, size_bytes, sha256, "
        "status, created_at) VALUES (gen_random_uuid(), :user, 'ministries', 'запись.mp3', "
        "'audio/mpeg', 1, '\\x01', 'ready', now())"
    )
    with pytest.raises(sa.exc.IntegrityError):
        query(
            "INSERT INTO kb_documents (id, owner_id, scope, title, media_type, size_bytes, "
            "sha256, storage_key, status, created_at) VALUES (gen_random_uuid(), :user, "
            "'ministries', 'повтор.txt', 'text/plain', 1, '\\x01', 'kb/3', 'ready', now())"
        )
    # Задание — либо о документе, либо о вложении: ни то ни другое — отказ.
    with pytest.raises(sa.exc.IntegrityError):
        query(
            "INSERT INTO kb_jobs (id, kind, priority, created_at) "
            "VALUES (gen_random_uuid(), 'transcribe', 10, now())"
        )
    with pytest.raises(NotImplementedError):
        command.downgrade(config, "0005")
