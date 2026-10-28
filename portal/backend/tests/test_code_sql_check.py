"""«Проверить запрос» и слой предпросмотра проекта «Код» (docs/portal-api.md §7.5.18)."""

import asyncio
import contextlib
import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from portal.core.settings import Settings
from portal.databases.ports import (
    DbRightsExcessError,
    DbTarget,
    DbUnavailableError,
    RightsReport,
    SampleRequest,
    SampleRows,
)
from tests.code_support import code_portal, new_project
from tests.db_support import (
    FakeCustomerDatabase,
    admin_client,
    column,
    create_connection,
    db_settings,
    refresh,
    table,
)
from tests.support import Portal, parse_events
from tests.test_db_api import WRITE
from tests.test_sql_run import DATA, DDL, FakeSandbox, _enabled

pytestmark = pytest.mark.anyio

PARCELS = [{"schema": "cadastre", "name": "parcels"}]


async def _web(client: httpx.AsyncClient, name: str = "Слой") -> dict[str, Any]:
    response = await client.post(
        "/api/code/projects", json={"source": "template", "kind": "js_plugin", "name": name}
    )
    assert response.status_code == 201, response.text
    project: dict[str, Any] = response.json()["project"]
    return project


async def _with_schema(client: httpx.AsyncClient) -> str:
    """Проект со схемой из «Мои схемы»."""
    project = await new_project(client)
    schema = await client.post("/api/sql/schemas", json={"name": "Кадастр", "content": DDL})
    patched = await client.patch(
        f"/api/code/projects/{project['id']}", json={"schema_id": schema.json()["id"]}
    )
    assert patched.status_code == 200, patched.text
    return str(project["id"])


async def _with_selection(
    portal: Portal, client: httpx.AsyncClient, project_id: str, **connection: Any
) -> str:
    admin = await admin_client(portal)
    target = (await create_connection(admin, **connection))["id"]
    assert (await refresh(client, target)).status_code == 200
    patched = await client.patch(
        f"/api/code/projects/{project_id}",
        json={"db": {"connection_id": target, "tables": PARCELS}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["schema_id"] is None
    return str(target)


async def _check(
    client: httpx.AsyncClient, project_id: str, sql: str, parameters: list[dict[str, str]]
) -> httpx.Response:
    return await client.post(
        f"/api/code/projects/{project_id}/sql-check", json={"sql": sql, "parameters": parameters}
    )


async def test_check_runs_bound_query_in_sandbox_only(settings: Settings) -> None:
    sandbox = FakeSandbox()
    async with code_portal(_enabled(settings), sql_sandbox=sandbox) as (portal, _):
        portal.model.sql_data = DATA
        client = await portal.employee()
        project_id = await _with_schema(client)
        response = await _check(
            client,
            project_id,
            "DELETE FROM parcels WHERE id = @id",
            [{"name": "@id", "value": "1'"}],
        )
        events = parse_events(response.text)
        assert [name for name, _ in events][-1] == "done"
        block = [data for name, data in events if name == "sql_run"][-1]["blocks"][0]
        assert (block["index"], block["line"], block["status"]) == (0, 1, "passed")
        assert sandbox.runs[-1][1] == "DELETE FROM parcels WHERE id = '1'''"
        # Итог нигде не сохраняется.
        assert await portal.rows("SELECT id FROM messages WHERE sql_run IS NOT NULL") == []

        other = await portal.employee("petrov")
        assert (await _check(other, project_id, "SELECT 1", [])).status_code == 404
        missing = await _check(client, project_id, "SELECT @a", [])
        assert missing.json()["error"]["details"] == {"names": ["@a"]}
        assert (await _check(client, project_id, " ", [])).json()["error"]["fields"][0] == {
            "field": "sql",
            "code": "required",
            "message": "Заполните поле.",
        }

        no_schema = await new_project(client, "OtherTool")
        response = await _check(client, no_schema["id"], "SELECT 1", [])
        assert (response.status_code, response.json()["error"]["code"]) == (
            422,
            "schema_required",
        )


async def test_one_check_per_project(settings: Settings) -> None:
    sandbox = FakeSandbox()
    async with code_portal(_enabled(settings), sql_sandbox=sandbox) as (portal, _):
        portal.model.sql_data = DATA
        client = await portal.employee()
        project_id = await _with_schema(client)
        gate = asyncio.Event()
        sandbox.script = [gate]
        first = asyncio.create_task(_check(client, project_id, "SELECT 1", []))
        await asyncio.wait_for(sandbox.started.wait(), timeout=5)
        busy = await _check(client, project_id, "SELECT 2", [])
        assert (busy.status_code, busy.json()["error"]["code"]) == (429, "sql_check_in_progress")
        gate.set()
        assert parse_events((await first).text)[-1][0] == "done"
        assert (await _check(client, project_id, "SELECT 3", [])).status_code == 200


async def test_check_needs_only_the_sandbox_flag(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await new_project(client)
        response = await _check(client, project["id"], "SELECT 1", [])
        assert (response.status_code, response.json()["error"]["code"]) == (
            403,
            "feature_disabled",
        )


async def test_hidden_selection_without_db_flag(settings: Settings) -> None:
    sandbox = FakeSandbox()
    async with code_portal(_enabled(settings), sql_sandbox=sandbox) as (portal, _):
        client = await portal.employee()
        project = await new_project(client)
        await portal.execute(
            "UPDATE code_projects SET db_connection_title = 'Кадастр', db_tables = '[]'::jsonb"
        )
        hidden = await _check(client, project["id"], "SELECT 1", [])
        assert hidden.json()["error"]["code"] == "schema_required"
        assert (await client.get(f"/api/code/projects/{project['id']}")).json()["db"] is None
        schema = await client.post("/api/sql/schemas", json={"name": "Кадастр", "content": DDL})
        patched = await client.patch(
            f"/api/code/projects/{project['id']}", json={"schema_id": schema.json()["id"]}
        )
        assert patched.status_code == 200
        title = await portal.rows("SELECT db_connection_title FROM code_projects")
        assert title[0][0] is None
        assert (await _check(client, project["id"], "SELECT 1", [])).status_code == 200


async def test_check_with_selection_reads_sample_and_runs_in_sandbox(settings: Settings) -> None:
    sandbox, fake = FakeSandbox(), FakeCustomerDatabase()
    async with code_portal(
        db_settings(_enabled(settings)), sql_sandbox=sandbox, customer_database=fake
    ) as (portal, _):
        client = await portal.employee()
        project = await new_project(client)
        await _with_selection(portal, client, project["id"], samples_allowed=True)
        response = await _check(client, project["id"], "DELETE FROM parcels WHERE status = 'x'", [])
        events = parse_events(response.text)
        assert [name for name, _ in events][-1] == "done"
        # В базу заказчика — только чтение образца; оператор — только в песочницу.
        assert [name for name, _ in fake.calls] == ["snapshot", "sample"]
        assert "DELETE" not in json.dumps(fake.calls)
        assert sandbox.runs[-1][1] == "DELETE FROM parcels WHERE status = 'x'"


async def test_preview_layer(settings: Settings) -> None:
    fake = FakeCustomerDatabase()
    async with code_portal(db_settings(settings), customer_database=fake) as (portal, _):
        client = await portal.employee()
        web = await _web(client)
        path = f"/api/code/projects/{web['id']}/preview-layer"
        query = {"schema": "cadastre", "table": "parcels"}
        empty = await client.get(path, params=query)
        assert (empty.status_code, empty.json()["error"]["code"]) == (422, "schema_required")
        connection = await _with_selection(
            portal,
            client,
            web["id"],
            samples_allowed=True,
            sample_exclusions=[{"schema": "cadastre", "table": "parcels", "column": "area"}],
        )
        layer = (await client.get(path, params=query)).json()
        assert layer["geometry"] == {"column": "geom", "type": "MultiPolygon"}
        assert (layer["rows"], layer["excluded_columns"], layer["truncated"]) == (2, 1, False)
        first, second = layer["features"]["features"]
        assert first["geometry"]["type"] == "MultiPolygon" and second["geometry"] is None
        assert set(first["properties"]) == {"id", "status"}
        assert fake.calls[-1] == ("sample", [("parcels", ("id", "status"), "geom")])

        other_table = await client.get(path, params={"schema": "public", "table": "notes"})
        assert other_table.json()["error"]["fields"][0]["field"] == "table"
        foreign = await (await portal.employee("petrov")).get(path, params=query)
        assert foreign.status_code == 404
        gptool = await new_project(client)
        wrong = await client.get(f"/api/code/projects/{gptool['id']}/preview-layer", params=query)
        assert wrong.json()["error"]["code"] == "wrong_project_kind"

        admin = await admin_client(portal, "boss2")
        await admin.patch(
            f"/api/admin/db-connections/{connection}", json={"samples_allowed": False}
        )
        denied = await client.get(path, params=query)
        assert denied.json()["error"]["code"] == "db_samples_not_allowed"
        await admin.patch(f"/api/admin/db-connections/{connection}", json={"samples_allowed": True})
        fake.findings = [WRITE]
        await admin.post(f"/api/admin/db-connections/{connection}/check")
        calls = len(fake.calls)
        unusable = await client.get(path, params=query)
        assert unusable.json()["error"]["code"] == "db_connection_unusable"
        assert len(fake.calls) == calls


async def test_preview_layer_of_any_schema_with_all_schemas(settings: Settings) -> None:
    """«Все схемы»: слой берётся из отмеченной таблицы любой схемы снимка (§7.5.18)."""
    fake = FakeCustomerDatabase()
    fake.tables = [*fake.tables, table("archive", "plots", [column("id", primary_key=True)])]
    async with code_portal(db_settings(settings), customer_database=fake) as (portal, _):
        client = await portal.employee()
        web = await _web(client)
        admin = await admin_client(portal)
        connection = await create_connection(
            admin, all_schemas=True, schemas=[], samples_allowed=True
        )
        assert (await refresh(client, connection["id"])).status_code == 200
        plots = {"schema": "archive", "name": "plots"}
        patched = await client.patch(
            f"/api/code/projects/{web['id']}",
            json={"db": {"connection_id": connection["id"], "tables": [plots]}},
        )
        assert patched.status_code == 200, patched.text
        layer = await client.get(
            f"/api/code/projects/{web['id']}/preview-layer",
            params={"schema": "archive", "table": "plots"},
        )
        assert layer.status_code == 200, layer.text
        assert fake.calls[-1] == ("sample", [("plots", ("id",), None)])


async def test_preview_layer_flags(settings: Settings) -> None:
    fake = FakeCustomerDatabase()
    preview_off = settings.code.preview.model_copy(update={"enabled": False})
    async with code_portal(db_settings(settings), customer_database=fake, preview=preview_off) as (
        portal,
        _,
    ):
        client = await portal.employee()
        web = await _web(client)
        response = await client.get(
            f"/api/code/projects/{web['id']}/preview-layer",
            params={"schema": "cadastre", "table": "parcels"},
        )
        assert response.status_code == 403
        # Веб-виды создаются и выгружаются и без предпросмотра.
        assert (await client.get(f"/api/code/projects/{web['id']}/export")).status_code == 200
    async with code_portal(settings, customer_database=fake) as (portal, _):
        client = await portal.employee()
        web = await _web(client)
        response = await client.get(
            f"/api/code/projects/{web['id']}/preview-layer",
            params={"schema": "cadastre", "table": "parcels"},
        )
        assert response.status_code == 403


class _GatedDatabase(FakeCustomerDatabase):
    """Слой из образца ждёт `gate`; `fail` — чем закончится обращение к порту."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.fail: Exception | None = None

    async def sample(
        self, target: DbTarget, tables: Sequence[SampleRequest], *, rows: int, preview: bool
    ) -> tuple[RightsReport, Sequence[SampleRows]]:
        result = await super().sample(target, tables, rows=rows, preview=preview)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return result


async def test_one_sample_layer_load_per_employee(settings: Settings) -> None:
    """Вторая загрузка слоя из образца у сотрудника — `429` без обращения к базе (§7.5.18)."""
    fake = _GatedDatabase()
    query = {"schema": "cadastre", "table": "parcels"}
    async with code_portal(db_settings(settings), customer_database=fake) as (portal, _):
        client = await portal.employee()
        web, other_web = await _web(client), await _web(client, "Другой слой")
        connection = await _with_selection(portal, client, web["id"], samples_allowed=True)
        selection = {"db": {"connection_id": connection, "tables": PARCELS}}
        await client.patch(f"/api/code/projects/{other_web['id']}", json=selection)
        petrov = await portal.employee("petrov")
        petrov_web = await _web(petrov)
        await petrov.patch(f"/api/code/projects/{petrov_web['id']}", json=selection)

        def layer(who: httpx.AsyncClient, project: dict[str, Any]) -> Any:
            return who.get(f"/api/code/projects/{project['id']}/preview-layer", params=query)

        # Отказы до обращения к базе отметку не ставят.
        wrong = await client.get(
            f"/api/code/projects/{web['id']}/preview-layer",
            params={"schema": "public", "table": "notes"},
        )
        assert wrong.status_code == 422

        fake.gate = asyncio.Event()
        first = asyncio.create_task(layer(client, web))
        await asyncio.wait_for(fake.entered.wait(), timeout=5)
        calls = len(fake.calls)
        for project in (web, other_web):
            busy = await layer(client, project)
            assert (busy.status_code, busy.json()["error"]["code"]) == (
                429,
                "db_sample_in_progress",
            )
            assert (
                busy.json()["error"]["message"] == "Образец уже загружается. Дождитесь окончания."
            )
        assert len(fake.calls) == calls
        # У другого сотрудника своя отметка.
        fake.entered.clear()
        second = asyncio.create_task(layer(petrov, petrov_web))
        await asyncio.wait_for(fake.entered.wait(), timeout=5)
        fake.gate.set()
        assert (await first).status_code == 200 and (await second).status_code == 200

        # После итога, ошибки базы, лишних прав на шаге 4 и обрыва клиентом — снова можно.
        fake.gate = None
        assert (await layer(client, web)).status_code == 200
        fake.fail = DbUnavailableError("timeout")
        assert (await layer(client, web)).status_code == 503
        fake.fail = DbRightsExcessError(RightsReport([WRITE], []))
        unusable = await layer(client, web)
        assert unusable.json()["error"]["code"] == "db_connection_unusable"
        await portal.execute("UPDATE db_connections SET rights_status = 'unknown'")
        fake.fail = None
        fake.gate, fake.entered = asyncio.Event(), asyncio.Event()
        aborted = asyncio.create_task(layer(client, web))
        await asyncio.wait_for(fake.entered.wait(), timeout=5)
        aborted.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await aborted
        fake.gate.set()
        fake.gate = None
        assert (await layer(client, web)).status_code == 200
