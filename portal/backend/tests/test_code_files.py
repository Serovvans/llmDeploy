"""Мастер, импорт и выгрузка ZIP раздела «Код» (docs/portal-api.md §7.5.4, §7.5.5)."""

import io
import zipfile
from typing import Any
from uuid import uuid4

import httpx
import pytest

from portal.code.domain import CodeLimits, ProjectView
from portal.code.edit import parse_edit
from portal.code.paths import conflicts, path_error, project_name_error
from portal.code.wizard import class_names
from portal.core.settings import Settings
from tests.code_support import code_portal, edit_answer, file_of, new_project

pytestmark = pytest.mark.anyio

_CSPROJ = b"""<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="npgsql" Version="8.0.3" />
    <PackageReference Include="Dapper"><Version>2.1.35</Version></PackageReference>
  </ItemGroup>
</Project>"""


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


async def _import(client: httpx.AsyncClient, data: bytes, name: str = "Imported") -> Any:
    return await client.post(
        "/api/code/projects/import",
        files={"file": ("project.zip", data, "application/zip")},
        data={"name": name},
    )


def _wizard(**tool: Any) -> dict[str, Any]:
    return {
        "name": "AddressTool",
        "tools": [
            {
                "title_ru": "Добавить контакт",
                "title_en": "Add Municipal Contact",
                "category": "Утилиты",
                "icon": "fa fa-envelope",
                "uses_database": True,
                "description": "Добавляет контакт.",
                "inputs": [
                    {
                        "name": "Email",
                        "type": "string",
                        "required": True,
                        "title_ru": "Почта",
                        "title_en": "Email",
                    }
                ],
                "outputs": [
                    {"name": "Result", "type": "string", "title_ru": "Итог", "title_en": "Result"}
                ],
                **tool,
            }
        ],
    }


async def test_import_takes_cs_files_and_known_packages(settings: Settings) -> None:
    data = _zip(
        {
            "Solution/Proj/A.cs": b"class A {}",
            "Solution/Proj/Sub/B.cs": "// Привет".encode("cp1251"),
            "Solution/Proj/bin/Debug/X.cs": b"skipped",
            "Solution/.vs/C.cs": b"skipped",
            "Solution/Proj/readme.md": b"ignored",
            "Solution/Proj/Bad name.cs": b"x",
            "Solution/Proj/Zero.cs": b"a\x00b",
            "Solution/Proj/sub/b.CS": b"duplicate",
            "Solution/Proj/Proj.csproj": _CSPROJ,
        }
    )
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        response = await _import(client, data)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["assistant_request"] is None
        assert sorted(item["path"] for item in body["project"]["files"]) == ["A.cs", "Sub/B.cs"]
        assert file_of(body["project"], "Sub/B.cs")["content"] == "// Привет"
        assert body["project"]["packages"] == ["Npgsql"]
        report = body["import"]
        assert report["ignored_files"] == 2  # readme.md и сам .csproj: в проект не входят
        assert sorted((item["path"], item["reason"]) for item in report["skipped_files"]) == [
            ("Solution/Proj/Bad name.cs", "invalid_path"),
            ("Solution/Proj/Zero.cs", "not_text"),
            ("Solution/Proj/sub/b.CS", "duplicate"),
        ]
        assert sorted(report["unmatched_packages"], key=lambda item: item["id"]) == [
            {"id": "Dapper", "version": "2.1.35", "offline_version": None},
            {"id": "npgsql", "version": "8.0.3", "offline_version": "9.0.3"},
        ]
        snapshots = await client.get(f"/api/code/projects/{body['project']['id']}/snapshots")
        assert snapshots.json() == {"items": []}


@pytest.mark.parametrize(
    ("data", "status", "code"),
    [
        (b"not a zip at all", 415, "unsupported_file_type"),
        (b"PK\x03\x04broken", 422, "file_unreadable"),
        (_zip({"readme.md": b"x", "bin/A.cs": b"x"}), 422, "import_no_files"),
    ],
)
async def test_import_refusals(settings: Settings, data: bytes, status: int, code: str) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        response = await _import(client, data)
        assert response.status_code == status, response.text
        assert response.json()["error"]["code"] == code
        assert (await client.get("/api/code/projects")).json()["items"] == []


async def test_import_size_limit(settings: Settings) -> None:
    async with code_portal(settings, import_max_bytes=1000) as (portal, _):
        client = await portal.employee()
        response = await _import(client, _zip({"A.cs": b"x" * 5000}))
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"


async def test_export_has_project_folder_csproj_and_memo(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await new_project(client)
        await client.patch(f"/api/code/projects/{project['id']}", json={"packages": ["Npgsql"]})
        response = await client.get(f"/api/code/projects/{project['id']}/export")
        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            "attachment; filename=\"project.zip\"; filename*=UTF-8''AddressTool.zip"
        )
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = set(archive.namelist())
        assert {
            "AddressTool/AddContactTool.cs",
            "AddressTool/GpToolFactory.cs",
            "AddressTool/AddressTool.csproj",
            "AddressTool/Прочтите.txt",
        } <= names
        csproj = archive.read("AddressTool/AddressTool.csproj").decode()
        assert '<PackageReference Include="Npgsql" Version="9.0.3" />' in csproj
        assert "cogis\\eLiteGIS.Server.dll" in csproj
        memo = archive.read("AddressTool/Прочтите.txt")
        assert memo.startswith("\ufeff".encode()) and "eLiteGIS.Server.dll" in memo.decode()
        assert not any(name.lower().endswith(".dll") for name in names)


async def test_patch_packages_adds_in_offline_order_and_checks_schema(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        pid = (await new_project(client))["id"]
        patched = await client.patch(
            f"/api/code/projects/{pid}",
            json={"packages": ["Microsoft.Extensions.Logging.Abstractions", "npgsql"]},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["packages"] == [
            "Npgsql",
            "Microsoft.Extensions.Logging.Abstractions",
        ]
        unknown = await client.patch(f"/api/code/projects/{pid}", json={"packages": ["Dapper"]})
        assert unknown.status_code == 422
        empty = await client.patch(f"/api/code/projects/{pid}", json={})
        assert empty.status_code == 422
        missing = await client.patch(
            f"/api/code/projects/{pid}",
            json={"schema_id": "11111111-1111-1111-1111-111111111111"},
        )
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "schema_not_found"


async def test_wizard_builds_scaffold_with_database_packages(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        response = await client.post(
            "/api/code/projects", json={"source": "wizard", "wizard": _wizard()}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        project = body["project"]
        assert sorted(item["path"] for item in project["files"]) == [
            "AddMunicipalContactTool.cs",
            "GpToolFactory.cs",
        ]
        tool = file_of(project, "AddMunicipalContactTool.cs")["content"]
        assert "namespace AddressTool" in tool and "TargetWorkspace" in tool
        assert "Добавить контакт" in tool
        assert project["packages"]  # база данных добавляет пакеты набора
        assert body["assistant_request"] and "Добавляет контакт." in body["assistant_request"]


@pytest.mark.parametrize("title", ["Gp", "Gp tool", "gp"])
def test_tool_class_never_takes_template_base_type(title: str) -> None:
    # `class GpTool : GpTool` не собирается (CS0146): имя базового типа занято.
    assert class_names([title, "Factory"]) == ["GpTool2", "FactoryTool"]


async def test_wizard_scaffold_with_base_type_title(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        response = await client.post(
            "/api/code/projects", json={"source": "wizard", "wizard": _wizard(title_en="Gp")}
        )
        assert response.status_code == 201, response.text
        project = response.json()["project"]
        assert "public sealed class GpTool2 : GpTool" in file_of(project, "GpTool2.cs")["content"]
        assert "typeof(GpTool2)" in file_of(project, "GpToolFactory.cs")["content"]


@pytest.mark.parametrize("way", ["wizard", "transfer", "example", "import"])
async def test_scaffold_type_is_not_a_project_name(settings: Settings, way: str) -> None:
    # Пространство имён `GpTool` скрыло бы базовый класс каркаса (CS0118).
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        wizard = {**_wizard(), "name": "GpTool"}
        if way == "import":
            response = await _import(client, _zip({"A.cs": b"class A {}"}), name="GpTool")
        else:
            body: dict[str, Any] = {"source": way, "wizard": wizard}
            if way == "example":
                body = {"source": "example", "name": "GpTool"}
            elif way == "transfer":
                body = {
                    "source": "wizard",
                    "wizard": wizard,
                    "carried": {"text": "Итоги", "source_dialog_id": str(uuid4())},
                }
            response = await client.post("/api/code/projects", json=body)
        assert response.status_code == 422, response.text
        fields = response.json()["error"]["fields"]
        assert [(item["field"], item["code"]) for item in fields] == [("name", "invalid_format")]


@pytest.mark.parametrize(
    ("name", "uses_database"),
    [
        ("addmunicipalcontacttool", False),
        ("validateInput", False),
        ("InnerExecute", False),
        ("targetworkspace", True),
        ("OpenConnection", True),
        ("GetConnectionString", True),
        ("WorkspaceName", True),
    ],
)
@pytest.mark.parametrize("group", ["inputs", "outputs"])
async def test_parameter_cannot_repeat_tool_member(
    settings: Settings, name: str, uses_database: bool, group: str
) -> None:
    parameter: dict[str, Any] = {"name": name, "type": "string", "title_ru": "a", "title_en": "a"}
    if group == "inputs":
        parameter["required"] = False
    tool = {"uses_database": uses_database, "inputs": [], "outputs": [], group: [parameter]}
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        response = await client.post(
            "/api/code/projects", json={"source": "wizard", "wizard": _wizard(**tool)}
        )
        assert response.status_code == 422, response.text
        fields = [item["field"] for item in response.json()["error"]["fields"]]
        assert fields == [f"tools.0.{group}.0.name"]


async def test_database_member_names_are_free_without_database(settings: Settings) -> None:
    parameter = {"name": "WorkspaceName", "type": "string", "required": False}
    tool = {"uses_database": False, "inputs": [{**parameter, "title_ru": "a", "title_en": "a"}]}
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        response = await client.post(
            "/api/code/projects", json={"source": "wizard", "wizard": _wizard(**tool)}
        )
        assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    ("tool", "field"),
    [
        ({"title_ru": "Строка\u2028разрыв"}, "tools.0.title_ru"),
        ({"icon": "fa;alert"}, "tools.0.icon"),
        (
            {"inputs": [{"name": "class", "type": "string", "required": True,
                         "title_ru": "a", "title_en": "a"}]},
            "tools.0.inputs.0.name",
        ),
        (
            {"inputs": [{"name": "TargetWorkspace", "type": "string", "required": True,
                         "title_ru": "a", "title_en": "a"}]},
            "tools.0.inputs.0.name",
        ),
        (
            {"inputs": [{"name": "A", "type": "nope", "required": True,
                         "title_ru": "a", "title_en": "a"}]},
            "tools.0.inputs.0.type",
        ),
    ],
)  # fmt: skip
async def test_wizard_form_is_checked(settings: Settings, tool: dict[str, Any], field: str) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        response = await client.post(
            "/api/code/projects", json={"source": "wizard", "wizard": _wizard(**tool)}
        )
        assert response.status_code == 422, response.text
        assert field in [item["field"] for item in response.json()["error"]["fields"]]
        assert (await client.get("/api/code/projects")).json()["items"] == []


async def test_project_count_limit(settings: Settings) -> None:
    async with code_portal(settings, max_projects=1) as (portal, _):
        client = await portal.employee()
        await new_project(client)
        refused = await client.post(
            "/api/code/projects", json={"source": "example", "name": "Second"}
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["details"] == {"max": 1}


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("AddressTool", True),
        ("class", False),
        ("Con", False),
        ("1Tool", False),
        ("A_b", True),
        ("GpTool", False),
        ("DateTime", False),
        ("GpCategoryAttribute", False),
        ("gptool", True),
    ],
)
def test_project_name_rules(name: str, valid: bool) -> None:
    assert (project_name_error(name) is None) is valid


def test_path_conflicts_are_case_insensitive() -> None:
    assert conflicts("A.CS/B.cs", ["a.cs"])
    assert conflicts("a.cs", ["A.cs/B.cs"])
    assert not conflicts("Helpers/A.cs", ["Helpers/B.cs"])
    assert path_error("Tools./A.cs", (".cs",)) == "invalid_format"
    assert path_error("C:/A.cs", (".cs",)) == "outside_project"


def test_parse_edit_last_duplicate_wins_and_nul_is_rejected() -> None:
    limits = CodeLimits(50, 16384, 262144, (".cs",))
    text = edit_answer(("A.cs", "first"), ("a.cs", "second"), ("Z.cs", "bad\x00"))
    parsed = parse_edit(text, ProjectView("P", []), limits, "complete")
    assert [(item.path, item.content) for item in parsed.files] == [("a.cs", "second\n")]
    assert [item["path"] for item in parsed.rejected] == ["Z.cs"]
    assert parsed.incomplete is False
