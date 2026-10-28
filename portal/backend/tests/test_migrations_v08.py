"""Миграция `0009` на базе с данными версии 0.7 (docs/portal-api.md §9.22).

Так обновляется работающий сервис: `portal migrate` проводит базу 0.7 до головы. Прежние
диалоги всех видов остаются как были; пересказа и перенесённого у них нет.
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

_EXCLUDED = "UPDATE messages SET memory_mode = 'excluded' WHERE id = :question"


def test_previous_release_data_survives_without_summary_or_carried(settings: Settings) -> None:
    name = f"migration_{uuid4().hex}"
    asyncio.run(_run(settings, (f'CREATE DATABASE "{name}"',)))
    database = settings.database.model_copy(update={"name": name})
    target = settings.model_copy(update={"database": database})
    config = Config("alembic.ini")
    config.attributes["settings"] = target
    ids = {key: uuid4() for key in ("user", "chat", "sql", "cogis", "docparse", "question")}

    command.upgrade(config, "0008")
    asyncio.run(_run(target, (*_SEED, _EXCLUDED), **ids))
    command.upgrade(config, "head")

    def query(statement: str, **params: Any) -> list[Any]:
        return asyncio.run(_run(target, (statement,), **params))

    assert query("SELECT version_num FROM alembic_version") == [("0012",)]
    assert query(
        "SELECT kind, summary, summary_until_position, carried_text, carried_source_id, "
        "carried_source_title FROM dialogs ORDER BY kind"
    ) == [(kind, None, None, None, None, None) for kind in ("chat", "cogis", "docparse", "sql")]
    assert query(
        "SELECT d.kind, m.position, m.content, m.memory_mode FROM messages m "
        "JOIN dialogs d ON d.id = m.dialog_id ORDER BY d.kind, m.position"
    ) == [
        ("chat", 0, "Вопрос", "excluded"),
        ("chat", 1, "Ответ", "full"),
        ("cogis", 0, "Плагин", "full"),
        ("cogis", 1, "", "full"),
        ("docparse", 0, "Срок?", "full"),
        ("docparse", 1, "Год", "full"),
        ("sql", 0, "Запрос", "full"),
        ("sql", 1, "SELECT 1", "full"),
    ]
    assert query("SELECT message_id FROM attachments") == [(ids["question"],)]
    assert query("SELECT summary, document_text FROM docparses") == [("Кратко", "Текст")]

    # Пересказ без границы не записывается; удаление источника обнуляет только ссылку.
    with pytest.raises(sa.exc.IntegrityError):
        query("UPDATE dialogs SET summary = 'П' WHERE id = :id", id=ids["chat"])
    query(
        "UPDATE dialogs SET carried_text = 'Т', carried_source_id = :source, "
        "carried_source_title = 'Название' WHERE id = :id",
        source=ids["sql"],
        id=ids["cogis"],
    )
    query("DELETE FROM dialogs WHERE id = :id", id=ids["sql"])
    assert query(
        "SELECT carried_text, carried_source_id, carried_source_title FROM dialogs WHERE id = :id",
        id=ids["cogis"],
    ) == [("Т", None, "Название")]
