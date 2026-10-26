"""Веб-виды проектов «Кода» (docs/portal-api.md §7.5.16): шаблоны, ограничения, выгрузка.

Виды `js_plugin`, `html_widget`, `catalog` и `card_hooks` — без пакетов, сборки, архива и
действия `fix`; помощник для них — свои промпты, без правил GPTool и справочника SDK.
"""

import io
import zipfile
from typing import Any

import httpx
import pytest

from portal.code.sql_check import bind_parameters, find_parameters
from portal.core.errors import AppError
from portal.core.settings import Settings
from portal.kb.ports import Retrieval
from portal.llm.ports import ChatRequest, ContentDelta, Finished, TextPart
from tests.code_support import code_portal, new_project
from tests.support import Portal, parse_events

pytestmark = pytest.mark.anyio

TEMPLATES = {
    "js_plugin": "plugin.js",
    "html_widget": "widget.html",
    "catalog": "catalog.css",
    "card_hooks": "card-hooks.html",
}


async def _web(client: httpx.AsyncClient, kind: str, name: str = "Плагин карты") -> dict[str, Any]:
    response = await client.post(
        "/api/code/projects", json={"source": "template", "kind": kind, "name": name}
    )
    assert response.status_code == 201, response.text
    project: dict[str, Any] = response.json()["project"]
    return project


def _texts(request: ChatRequest) -> str:
    return "\n".join(
        part.text
        for message in request.messages
        for part in message.parts
        if isinstance(part, TextPart)
    )


async def test_template_projects_have_one_file_of_their_kind(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        for kind, path in TEMPLATES.items():
            project = await _web(client, kind, f"Проект {kind}")
            assert project["kind"] == kind
            assert [item["path"] for item in project["files"]] == [path]
            assert project["build"] is None and project["packages"] == []
        unknown = await client.post(
            "/api/code/projects", json={"source": "template", "kind": "gptool", "name": "x"}
        )
        assert unknown.json()["error"]["fields"][0] == {
            "field": "kind",
            "code": "unknown_value",
            "message": "Недопустимое значение.",
        }


async def test_web_kind_rules(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await _web(client, "js_plugin")
        pid, file = project["id"], project["files"][0]

        added = await client.post(
            f"/api/code/projects/{pid}/files", json={"path": "more.js", "content": ""}
        )
        assert (added.status_code, added.json()["error"]["code"]) == (409, "single_file_project")
        deleted = await client.delete(f"/api/code/projects/{pid}/files/{file['id']}")
        assert deleted.status_code == 409
        renamed = await client.patch(
            f"/api/code/projects/{pid}/files/{file['id']}",
            json={"revision": file["revision"], "path": "plugin.cs"},
        )
        assert renamed.json()["error"]["fields"][0]["code"] == "extension_not_allowed"

        for method, path in [("POST", "build"), ("GET", "build/archive")]:
            response = await client.request(method, f"/api/code/projects/{pid}/{path}")
            assert (response.status_code, response.json()["error"]["code"]) == (
                409,
                "wrong_project_kind",
            ), path
        packages = await client.patch(
            f"/api/code/projects/{pid}", json={"packages": [settings.code.packages[0].id]}
        )
        assert packages.json()["error"]["fields"][0] == {
            "field": "packages",
            "code": "invalid_format",
            "message": "Неверный формат.",
        }
        fix = await client.post(
            f"/api/dialogs/{project['dialog_id']}/messages",
            json={"content": "Исправь", "action": "fix", "file_ids": None, "target": None},
        )
        assert fix.json()["error"]["fields"][0]["field"] == "action"
        memory = await client.get(
            f"/api/dialogs/{project['dialog_id']}/memory", params={"action": "fix"}
        )
        assert memory.status_code == 422


async def test_catalog_files_and_export_with_memo(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await _web(client, "catalog", "Каталог: шапка")
        pid = project["id"]
        added = await client.post(
            f"/api/code/projects/{pid}/files", json={"path": "menu.js", "content": "// меню"}
        )
        assert added.json()["catalog"] == {
            "insertion": "body_end",
            "clients": ["smartphone", "tablet", "desktop"],
        }
        changed = await client.patch(
            f"/api/code/projects/{pid}",
            json={
                "catalog_files": [
                    {
                        "file_id": added.json()["id"],
                        "insertion": "head_start",
                        "clients": ["desktop"],
                    }
                ]
            },
        )
        assert changed.status_code == 200, changed.text
        gptool = await new_project(client)
        wrong = await client.patch(
            f"/api/code/projects/{gptool['id']}",
            json={
                "catalog_files": [
                    {"file_id": gptool["files"][0]["id"], "insertion": "head_end", "clients": []}
                ]
            },
        )
        assert wrong.status_code == 422

        response = await client.get(f"/api/code/projects/{pid}/export")
        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert sorted(archive.namelist()) == ["catalog.css", "menu.js", "Памятка.txt"]
        memo = archive.read("Памятка.txt")
        assert memo.startswith(b"\xef\xbb\xbf")
        text = memo.decode("utf-8-sig")
        assert "menu.js" in text and "catalog.css" in text
        assert "{files}" not in text and "{customizations}" not in text


async def test_web_assistant_request_and_edit_limits(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await _web(client, "js_plugin")
        portal.knowledge.result = Retrieval((), "", "")
        answer = (
            "Готово.\n\n```javascript path=plugin.js\nconsole.log(1);\n```\n\n"
            "```javascript path=../evil.js\nx\n```\n\n```css path=style.css\nx\n```\n"
        )
        portal.model.scripts.append([ContentDelta(answer), Finished("stop")])
        response = await client.post(
            f"/api/dialogs/{project['dialog_id']}/messages",
            json={"content": "Добавь вывод", "action": "write", "file_ids": None, "target": None},
        )
        edit = dict(parse_events(response.text))["code_edit"]
        assert [item["path"] for item in edit["files"]] == ["plugin.js"]
        assert {item["path"]: item["reason"] for item in edit["rejected_files"]} == {
            "../evil.js": "outside_project",
            "style.css": "extension_not_allowed",
        }
        sent = _texts(portal.model.requests[-1])
        assert "<описание_api>" in sent
        assert "Правила GPTool" not in sent and "<справочник" not in sent and "<сборка" not in sent


async def test_web_routes_need_the_code_section(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        web = await client.get("/api/code/web")
        assert web.status_code == 200
        assert {item["id"] for item in web.json()["libraries"]} == {"jquery", "knockout", "leaflet"}
        cached = await client.get("/api/code/web", headers={"If-None-Match": web.headers["ETag"]})
        assert cached.status_code == 304
        assert (await client.get("/api/code/web/libraries/evil.js")).status_code == 404
    portal_off: Portal
    async with code_portal(settings, enabled=False) as (portal_off, _):
        client = await portal_off.employee()
        assert (await client.get("/api/code/web")).status_code == 403


@pytest.mark.parametrize(
    ("sql", "names"),
    [
        ("SELECT * FROM t WHERE id = @id AND a = @id", ["@id"]),
        ("SELECT $1, $2, $1", ["$1", "$2"]),
        ("SELECT geom @> other, a <@ b, doc @@ q, x@y, a$1", []),
        ("SELECT '@a', \"@b\", $$ @c $$, $t$ @d $t$ -- @e\n/* @f /* @g */ */", []),
        ("SELECT E'\\'@a', @b", ["@b"]),
        ("SELECT (@x), = @y", ["@x", "@y"]),
    ],
)
def test_parameters_outside_opaque_regions(sql: str, names: list[str]) -> None:
    assert list(dict.fromkeys(item.name for item in find_parameters(sql))) == names


def test_parameter_values_are_string_constants() -> None:
    bound = bind_parameters(
        "SELECT * FROM t WHERE n = @n AND id = $1", [("@n", "O'Brien"), ("$1", "7")]
    )
    assert bound == "SELECT * FROM t WHERE n = 'O''Brien' AND id = '7'"


@pytest.mark.parametrize(
    ("given", "code"),
    [
        ([], "required"),
        ([("@a", "1"), ("@b", "2")], "unknown_value"),
        ([("@a", "1"), ("@a", "2")], "invalid_format"),
        ([("@a", "x" * 1001)], "too_long"),
    ],
)
def test_parameter_set_must_match(given: list[tuple[str, str]], code: str) -> None:
    with pytest.raises(AppError) as raised:
        bind_parameters("SELECT @a", given)
    assert raised.value.fields[0].code == code
    if code == "required":
        assert raised.value.details == {"names": ["@a"]}
