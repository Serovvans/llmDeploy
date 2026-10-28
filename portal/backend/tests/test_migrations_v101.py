"""Миграция `0012` на базе с данными версии 1.0 (docs/portal-api.md §9.34).

Подключение 1.0 со списком схем, снимком, исключениями и списком доступа, диалог и проект
с выбором подключения после миграции остаются как были, подключение — со списком схем.
"""

import asyncio
import json
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config

from portal.core.settings import Settings
from tests.test_migrations_v05 import _run
from tests.test_migrations_v07 import _SEED

_EXCLUSIONS = [{"schema": "cadastre", "table": "owners", "column": "phone"}]
_TABLES = [{"schema": "cadastre", "name": "parcels"}]
_SNAPSHOT = [{"schema": "cadastre", "name": "parcels", "kind": "table", "columns": []}]
_CONNECTION = (
    "INSERT INTO db_connections (id, title, host, port, database, username, "
    "password_encrypted, tls_mode, schemas, samples_allowed, sample_exclusions, access_all, "
    "config_changed_at, rights_status, created_at, updated_at) VALUES (:connection, 'Кадастр', "
    "'db.customer.test', 5432, 'cadastre', 'portal_reader', '\\x00', 'disable', "
    "ARRAY['cadastre', 'public'], true, CAST(:exclusions AS jsonb), false, now(), 'read_only', "
    "now(), now())",
    "INSERT INTO db_connection_users (connection_id, user_id) VALUES (:connection, :user)",
    "INSERT INTO db_snapshots (connection_id, taken_at, content, tables, truncated, "
    "postgis_schema) VALUES (:connection, now(), CAST(:snapshot AS jsonb), 1, false, 'public')",
    "UPDATE dialogs SET db_connection_id = :connection, db_connection_title = 'Кадастр', "
    "db_tables = CAST(:tables AS jsonb) WHERE id = :sql",
    "INSERT INTO code_projects (id, owner_id, name, kind, db_connection_id, "
    "db_connection_title, db_tables, created_at, updated_at) VALUES (:project, :user, 'Слой', "
    "'js_plugin', :connection, 'Кадастр', CAST(:tables AS jsonb), now(), now())",
)


def test_connections_of_previous_release_keep_the_schema_list(settings: Settings) -> None:
    name = f"migration_{uuid4().hex}"
    asyncio.run(_run(settings, (f'CREATE DATABASE "{name}"',)))
    database = settings.database.model_copy(update={"name": name})
    target = settings.model_copy(update={"database": database})
    config = Config("alembic.ini")
    config.attributes["settings"] = target
    ids = {key: uuid4() for key in ("user", "chat", "sql", "cogis", "docparse", "question")}
    connection, project = uuid4(), uuid4()

    command.upgrade(config, "0011")
    asyncio.run(_run(target, _SEED, **ids))
    asyncio.run(
        _run(
            target,
            _CONNECTION,
            connection=connection,
            project=project,
            user=ids["user"],
            sql=ids["sql"],
            exclusions=json.dumps(_EXCLUSIONS),
            snapshot=json.dumps(_SNAPSHOT),
            tables=json.dumps(_TABLES),
        )
    )
    command.upgrade(config, "head")

    def query(statement: str) -> list[Any]:
        return asyncio.run(_run(target, (statement,)))

    assert query("SELECT version_num FROM alembic_version") == [("0012",)]
    assert query(
        "SELECT all_schemas, schemas, samples_allowed, sample_exclusions, access_all, "
        "rights_status FROM db_connections"
    ) == [(False, ["cadastre", "public"], True, _EXCLUSIONS, False, "read_only")]
    assert query("SELECT user_id FROM db_connection_users") == [(ids["user"],)]
    assert query("SELECT content, tables, truncated FROM db_snapshots") == [(_SNAPSHOT, 1, False)]
    selection = (connection, "Кадастр", _TABLES)
    assert query(
        "SELECT db_connection_id, db_connection_title, db_tables FROM dialogs "
        "WHERE db_connection_id IS NOT NULL"
    ) == [selection]
    assert query("SELECT db_connection_id, db_connection_title, db_tables FROM code_projects") == [
        selection
    ]
