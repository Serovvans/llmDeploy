"""Миграция `0010` на базе с данными версии 0.8 (docs/portal-api.md §9.6, §9.23–9.27).

Так обновляется работающий сервис: `portal migrate` проводит базу 0.8 до головы. Прежние
диалоги, в том числе помощника CoGIS, остаются как были: без проекта и без правок.
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
from tests.test_migrations_v07 import _SEED

_SUMMARY = (
    "UPDATE dialogs SET summary = 'Пересказ', summary_until_position = 1, "
    "carried_text = 'Перенесено', carried_source_title = 'Источник' WHERE id = :cogis"
)
_PROJECT = (
    "INSERT INTO code_projects (id, owner_id, name, created_at, updated_at) "
    "VALUES (:project, :user, 'AddressTool', now(), now())"
)


def test_previous_release_data_survives_and_code_tables_appear(settings: Settings) -> None:
    name = f"migration_{uuid4().hex}"
    asyncio.run(_run(settings, (f'CREATE DATABASE "{name}"',)))
    database = settings.database.model_copy(update={"name": name})
    target = settings.model_copy(update={"database": database})
    config = Config("alembic.ini")
    config.attributes["settings"] = target
    ids = {key: uuid4() for key in ("user", "chat", "sql", "cogis", "docparse", "question")}

    command.upgrade(config, "0009")
    asyncio.run(_run(target, (*_SEED, _SUMMARY), **ids))
    command.upgrade(config, "head")

    def query(statement: str, **params: Any) -> list[Any]:
        return asyncio.run(_run(target, (statement,), **params))

    assert query("SELECT version_num FROM alembic_version") == [("0011",)]
    assert query("SELECT kind, project_id FROM dialogs ORDER BY kind") == [
        (kind, None) for kind in ("chat", "cogis", "docparse", "sql")
    ]
    assert query(
        "SELECT summary, carried_text, carried_source_title FROM dialogs WHERE id = :id",
        id=ids["cogis"],
    ) == [("Пересказ", "Перенесено", "Источник")]
    assert query("SELECT count(*), count(code_edit) FROM messages") == [(8, 0)]
    assert query("SELECT message_id FROM attachments") == [(ids["question"],)]

    # Диалог `code` — только с проектом, проект — с одним диалогом; удаление каскадом.
    project = uuid4()
    with pytest.raises(sa.exc.IntegrityError):
        query(
            "INSERT INTO dialogs (id, owner_id, kind, created_at, updated_at) "
            "VALUES (:id, :user, 'code', now(), now())",
            id=uuid4(),
            user=ids["user"],
        )
    query(_PROJECT, project=project, user=ids["user"])
    dialog = uuid4()
    query(
        "INSERT INTO dialogs (id, owner_id, kind, project_id, created_at, updated_at) "
        "VALUES (:id, :user, 'code', :project, now(), now())",
        id=dialog,
        user=ids["user"],
        project=project,
    )
    with pytest.raises(sa.exc.IntegrityError):
        query(
            "INSERT INTO dialogs (id, owner_id, kind, project_id, created_at, updated_at) "
            "VALUES (:id, :user, 'cogis', :project, now(), now())",
            id=uuid4(),
            user=ids["user"],
            project=project,
        )
    with pytest.raises(sa.exc.IntegrityError):
        query(_PROJECT.replace("'AddressTool'", "'addresstool'"), project=uuid4(), user=ids["user"])
    query("DELETE FROM code_projects WHERE id = :id", id=project)
    assert query("SELECT count(*) FROM dialogs WHERE id = :id", id=dialog) == [(0,)]
