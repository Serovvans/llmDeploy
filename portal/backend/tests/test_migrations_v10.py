"""Миграция `0011` на базе с данными версии 0.9 (docs/portal-api.md §9.32–9.33).

Так обновляется работающий сервис: `portal migrate` проводит базу 0.9 до головы. Прежние
проекты становятся `gptool` без выбора подключения, прежние ответы `sql` читаются как были.
"""

import asyncio
import json
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config

from portal.core.settings import Settings
from portal.dialogs.schemas import _sql_run
from tests.test_migrations_v05 import _run
from tests.test_migrations_v07 import _SEED

_OLD_RUN = {
    "blocks": [
        {
            "index": 0,
            "line": 1,
            "status": "failed",
            "reason": None,
            "fixture": [{"table": "t", "columns": ["id"], "rows": [["1"]], "truncated": False}],
            "result": {"columns": ["id"], "rows": [["1"], ["2"]], "truncated": False},
            "affected_rows": None,
            "error": "ошибка",
            "fixed_sql": None,
        }
    ]
}
_PROJECT = (
    "INSERT INTO code_projects (id, owner_id, name, created_at, updated_at) "
    "VALUES (:project, :user, 'AddressTool', now(), now())"
)
_FILE = (
    "INSERT INTO code_files (id, project_id, path, content, size_bytes, updated_at) "
    "VALUES (gen_random_uuid(), :project, 'Tool.cs', 'class Tool {}', 13, now())"
)
_SQL_RUN = "UPDATE messages SET sql_run = CAST(:run AS jsonb) WHERE content = 'SELECT 1'"


def test_previous_release_data_survives_and_database_tables_appear(settings: Settings) -> None:
    name = f"migration_{uuid4().hex}"
    asyncio.run(_run(settings, (f'CREATE DATABASE "{name}"',)))
    database = settings.database.model_copy(update={"name": name})
    target = settings.model_copy(update={"database": database})
    config = Config("alembic.ini")
    config.attributes["settings"] = target
    ids = {key: uuid4() for key in ("user", "chat", "sql", "cogis", "docparse", "question")}
    project = uuid4()

    command.upgrade(config, "0010")
    asyncio.run(_run(target, _SEED, **ids))
    asyncio.run(_run(target, (_PROJECT,), project=project, user=ids["user"]))
    asyncio.run(_run(target, (_FILE,), project=project))
    asyncio.run(_run(target, (_SQL_RUN,), run=json.dumps(_OLD_RUN)))
    command.upgrade(config, "head")

    def query(statement: str, **params: Any) -> list[Any]:
        return asyncio.run(_run(target, (statement,), **params))

    assert query("SELECT version_num FROM alembic_version") == [("0012",)]
    assert query(
        "SELECT kind, db_connection_id, db_connection_title, db_tables FROM code_projects"
    ) == [("gptool", None, None, [])]
    assert query("SELECT catalog_insertion, catalog_clients FROM code_files") == [(None, None)]
    assert query(
        "SELECT db_connection_id, db_connection_title, db_tables FROM dialogs WHERE id = :id",
        id=ids["sql"],
    ) == [(None, None, [])]
    assert query("SELECT count(*) FROM db_connections") == [(0,)]
    stored = query("SELECT sql_run FROM messages WHERE sql_run IS NOT NULL")
    assert stored == [(_OLD_RUN,)]

    # Прежняя запись читается с полями 1.0 по умолчанию (§9.32).
    block = _sql_run(_OLD_RUN)["blocks"][0]  # type: ignore[index]
    assert block["db"] is None and block["error_kind"] == "message"
    assert (
        block["fixture"][0] | {"origin": "invented", "invented_reason": None} == block["fixture"][0]
    )
    assert block["result"]["row_count"] == 2
