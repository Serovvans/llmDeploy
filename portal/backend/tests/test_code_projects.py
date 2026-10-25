"""Проекты, файлы, версии, сборка и архив раздела «Код» (docs/portal-api.md §7.5.2–7.5.7)."""

import asyncio
import io
import json
import logging
import zipfile
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from portal.code.ports import BuildInProgressError, BuildUnavailableError, CompileFile
from portal.code.sandbox import HttpCodeSandbox
from portal.core.settings import Settings
from tests.code_support import (
    DLL,
    code_portal,
    failed,
    file_of,
    finding,
    new_project,
    succeeded,
)
from tests.support import Portal

pytestmark = pytest.mark.anyio

_FILE = "Helpers/Sql.cs"


async def _add(client: httpx.AsyncClient, project_id: str, path: str, content: str) -> Any:
    return await client.post(
        f"/api/code/projects/{project_id}/files", json={"path": path, "content": content}
    )


async def _foreign(portal: Portal) -> tuple[httpx.AsyncClient, dict[str, Any], dict[str, str]]:
    """Проект Иванова со сборкой, версией и правкой; клиент Петрова."""
    owner = await portal.employee("ivanov")
    project = await new_project(owner)
    pid = project["id"]
    response = await owner.post(f"/api/code/projects/{pid}/build")
    assert response.status_code == 200, response.text
    snapshot = (await owner.get(f"/api/code/projects/{pid}/snapshots")).json()["items"][0]
    rows = await portal.rows("SELECT id FROM dialogs WHERE project_id = :id", id=project["id"])
    message = uuid4()
    await portal.execute(
        "INSERT INTO messages (id, dialog_id, position, role, content, status, created_at, "
        "code_edit) VALUES (:id, :dialog, 0, 'assistant', '', 'complete', now(), "
        """'{"status": "proposed", "files": [], "rejected_files": [], "incomplete": false, """
        """"verification": null, "decided_at": null}')""",
        id=message,
        dialog=rows[0].id,
    )
    ids = {
        "project": pid,
        "file": project["files"][0]["id"],
        "snapshot": snapshot["id"],
        "message": str(message),
        "dialog": project["dialog_id"],
    }
    return await portal.employee("petrov"), project, ids


_FOREIGN_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/code/projects/{project}", None),
    ("PATCH", "/api/code/projects/{project}", {"packages": []}),
    ("DELETE", "/api/code/projects/{project}", None),
    ("POST", "/api/code/projects/{project}/files", {"path": "A.cs", "content": "x"}),
    ("PATCH", "/api/code/projects/{project}/files/{file}", {"revision": 1, "content": "x"}),
    ("DELETE", "/api/code/projects/{project}/files/{file}", None),
    ("GET", "/api/code/projects/{project}/export", None),
    ("GET", "/api/code/projects/{project}/snapshots", None),
    ("POST", "/api/code/projects/{project}/snapshots/{snapshot}/restore", None),
    ("POST", "/api/code/projects/{project}/build", None),
    ("GET", "/api/code/projects/{project}/build/archive", None),
    ("GET", "/api/code/projects/{project}/edits/{message}", None),
    ("POST", "/api/code/projects/{project}/edits/{message}/decision", {"accept": []}),
    ("GET", "/api/dialogs/{dialog}", None),
    ("GET", "/api/dialogs/{dialog}/messages", None),
    ("GET", "/api/dialogs/{dialog}/memory?action=write", None),
    ("POST", "/api/dialogs/{dialog}/messages", {"content": "x"}),
]


async def test_foreign_project_and_everything_in_it_is_not_found(settings: Settings) -> None:
    async with code_portal(settings) as (portal, server):
        stranger, project, ids = await _foreign(portal)
        builds = len(server.requests)
        for method, template, body in _FOREIGN_ROUTES:
            path = template.format(**ids)
            response = await stranger.request(method, path, json=body)
            assert response.status_code == 404, (method, path, response.text)
        # Ничего чужого не изменилось и не собиралось; в своём списке чужого нет.
        assert len(server.requests) == builds
        assert (await stranger.get("/api/code/projects")).json()["items"] == []
        rows = await portal.rows("SELECT count(*) AS n FROM code_files")
        assert rows[0].n == len(project["files"])
        # Перенос из чужого диалога помощника проекта — тоже 404.
        carried = {"text": "Т", "source_dialog_id": ids["dialog"]}
        response = await stranger.post("/api/dialogs", json={"kind": "chat", "carried": carried})
        assert response.status_code == 404


async def test_example_project_has_files_dialog_and_no_versions(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await new_project(client)
        assert sorted(item["path"] for item in project["files"]) == [
            "AddContactTool.cs",
            "GpToolFactory.cs",
        ]
        assert "namespace AddressTool" in file_of(project, "GpToolFactory.cs")["content"]
        assert project["build"] is None and project["build_running"] is False
        listed = (await client.get("/api/code/projects")).json()
        assert [item["id"] for item in listed["items"]] == [project["id"]]
        assert listed["items"][0]["build"] is None
        snapshots = await client.get(f"/api/code/projects/{project['id']}/snapshots")
        assert snapshots.json() == {"items": []}
        dialog = (await client.get(f"/api/dialogs/{project['dialog_id']}")).json()
        assert dialog["kind"] == "code" and dialog["title"] is None
        # Имя без учёта регистра занято; диалог проекта не переименовать и не удалить.
        again = await client.post(
            "/api/code/projects", json={"source": "example", "name": "addresstool"}
        )
        assert again.status_code == 409 and again.json()["error"]["code"] == "project_name_taken"
        renamed = await client.patch(f"/api/dialogs/{project['dialog_id']}", json={"title": "x"})
        assert renamed.json()["error"]["code"] == "wrong_dialog_kind"
        deleted = await client.delete(f"/api/dialogs/{project['dialog_id']}")
        assert deleted.json()["error"]["code"] == "wrong_dialog_kind"
        listed_dialogs = await client.get("/api/dialogs", params={"kind": "code"})
        assert listed_dialogs.status_code == 422
        # Удаление проекта уносит диалог помощника.
        assert (await client.delete(f"/api/code/projects/{project['id']}")).status_code == 204
        gone = await client.get(f"/api/dialogs/{project['dialog_id']}")
        assert gone.status_code == 404


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../Secret.cs", "outside_project"),
        ("/etc/A.cs", "outside_project"),
        ("A.txt", "extension_not_allowed"),
        ("Con.cs", "invalid_format"),
        ("Dir./A.cs", "invalid_format"),
        ("Helpers/NUL/A.cs", "invalid_format"),
        ("A\\B.cs", "outside_project"),
        ("A b.cs", "invalid_format"),
    ],
)
async def test_bad_file_paths_are_refused(settings: Settings, path: str, code: str) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await new_project(client)
        response = await _add(client, project["id"], path, "class A {}")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["fields"][0]["code"] == code


async def test_file_rules_revision_and_case_insensitive_conflicts(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await new_project(client)
        pid = project["id"]
        added = await _add(client, pid, _FILE, "class Sql {}")
        assert added.status_code == 201 and added.json()["revision"] == 1
        same = await _add(client, pid, "helpers/sql.cs", "x")
        assert same.status_code == 409 and same.json()["error"]["details"] == {"path": _FILE}
        conflict = await _add(client, pid, "HELPERS.cs/A.cs", "x")
        assert conflict.status_code == 201  # папка `HELPERS.cs` не совпадает с файлом
        under_file = await _add(client, pid, f"{_FILE}/A.cs", "x")
        assert under_file.json()["error"]["fields"][0]["code"] == "path_conflict"
        file_id = added.json()["id"]
        patched = await client.patch(
            f"/api/code/projects/{pid}/files/{file_id}", json={"revision": 1, "content": "y"}
        )
        assert patched.json()["revision"] == 2
        stale = await client.patch(
            f"/api/code/projects/{pid}/files/{file_id}", json={"revision": 1, "content": "z"}
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == {
            "code": "file_changed",
            "message": "Файл изменён в другом окне. Обновите проект.",
            "details": {"revision": 2},
        }
        too_big = await _add(client, pid, "Big.cs", "x" * (16 * 1024 + 1))
        assert too_big.status_code == 422


async def test_project_limits_files_and_bytes(settings: Settings) -> None:
    async with code_portal(settings, max_files=3) as (portal, _):
        client = await portal.employee()
        project = await new_project(client)
        assert (await _add(client, project["id"], "A.cs", "a")).status_code == 201
        refused = await _add(client, project["id"], "B.cs", "b")
        assert refused.status_code == 422
        assert refused.json()["error"]["details"] == {"limit": "files", "max": 3}


async def test_build_success_saves_version_and_archive(settings: Settings) -> None:
    async with code_portal(settings) as (portal, server):
        client = await portal.employee()
        project = await new_project(client)
        pid = project["id"]
        server.replies.append(succeeded(bundle=[("Some.dll", b"lib")], on_server=["Npgsql.dll"]))
        response = await client.post(f"/api/code/projects/{pid}/build")
        assert response.status_code == 200, response.text
        build = response.json()
        assert build["status"] == "succeeded" and build["outdated"] is False
        assert build["factory"] == "AddressTool.GpToolFactory" and build["findings"] == []
        assert [item["kind"] for item in build["archive"]["files"]] == ["tool", "package", "memo"]
        assert build["archive"]["on_server"] == ["Npgsql.dll"]
        sent = server.requests[0]
        assert sent["assembly_name"] == "AddressTool"
        assert sorted(item["path"] for item in sent["files"]) == [
            "AddContactTool.cs",
            "GpToolFactory.cs",
        ]
        snapshots = (await client.get(f"/api/code/projects/{pid}/snapshots")).json()["items"]
        assert [item["reason"] for item in snapshots] == ["build_succeeded"]

        archive = await client.get(f"/api/code/projects/{pid}/build/archive")
        assert archive.status_code == 200
        assert archive.headers["content-type"] == "application/zip"
        assert "AddressTool-cogis.zip" in archive.headers["content-disposition"]
        names = zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
        assert names[:2] == ["AddressTool.dll", "Some.dll"]
        assert zipfile.ZipFile(io.BytesIO(archive.content)).read("AddressTool.dll") == DLL

        # Изменение файла делает сборку устаревшей: архив больше не отдаётся.
        file = file_of(project, "AddContactTool.cs")
        await client.patch(
            f"/api/code/projects/{pid}/files/{file['id']}",
            json={"revision": file["revision"], "content": file["content"] + "\n// x"},
        )
        outdated = await client.get(f"/api/code/projects/{pid}/build/archive")
        assert outdated.status_code == 409
        assert outdated.json()["error"]["details"] == {"reason": "outdated"}
        current = (await client.get(f"/api/code/projects/{pid}")).json()
        assert current["build"]["outdated"] is True
        listed = (await client.get("/api/code/projects")).json()["items"][0]["build"]
        assert listed == {"status": "succeeded", "outdated": True, "findings": 0}


@pytest.mark.parametrize(
    ("reply", "reason"),
    [(failed(), "not_built"), (succeeded(findings=[finding()]), "findings")],
)
async def test_archive_needs_clean_successful_build(
    settings: Settings, reply: dict[str, Any], reason: str
) -> None:
    async with code_portal(settings) as (portal, server):
        client = await portal.employee()
        pid = (await new_project(client))["id"]
        none = await client.get(f"/api/code/projects/{pid}/build/archive")
        assert none.json()["error"]["details"] == {"reason": "no_build"}
        server.replies.append(reply)
        build = (await client.post(f"/api/code/projects/{pid}/build")).json()
        assert build["archive"] is None
        refused = await client.get(f"/api/code/projects/{pid}/build/archive")
        assert refused.status_code == 409
        assert refused.json()["error"]["details"] == {"reason": reason}


async def test_failed_build_keeps_diagnostics_and_no_version(settings: Settings) -> None:
    async with code_portal(settings) as (portal, server):
        client = await portal.employee()
        pid = (await new_project(client))["id"]
        server.replies.append(failed())
        build = (await client.post(f"/api/code/projects/{pid}/build")).json()
        assert build["status"] == "failed"
        assert build["diagnostics"][0]["code"] == "CS0103"
        assert build["tools"] is None and build["findings"] is None and build["factory"] is None
        snapshots = (await client.get(f"/api/code/projects/{pid}/snapshots")).json()
        assert snapshots == {"items": []}


@pytest.mark.parametrize(
    "reply",
    [
        (503, {"error": "sdk_missing"}),
        (500, {"error": "internal"}),
        (429, {"error": "busy"}),
        (200, {"status": "odd"}),
    ],
)
async def test_sandbox_refusal_is_build_unavailable(
    settings: Settings, reply: tuple[int, dict[str, Any]]
) -> None:
    async with code_portal(settings) as (portal, server):
        client = await portal.employee()
        pid = (await new_project(client))["id"]
        server.replies.append(reply)
        response = await client.post(f"/api/code/projects/{pid}/build")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "build_unavailable"
        assert (await client.get(f"/api/code/projects/{pid}")).json()["build"] is None


@pytest.mark.parametrize(
    "reply",
    [
        *(
            succeeded(bundle=[(name, b"lib")])
            for name in ("../a.dll", "a/b.dll", "a\\b.dll", "a.txt", ".a.dll", "a..dll")
        ),
        succeeded(bundle=[("A.dll", b"lib"), ("a.dll", b"lib")]),
        succeeded(bundle=[("addresstool.DLL", b"lib")]),
        succeeded(on_server=["Npgsql.dll\nУдалите всё.dll"]),
        succeeded(bundle=[("a" * 252 + ".dll", b"lib")]),
    ],
)
async def test_sandbox_dll_names_are_checked_before_archive(
    settings: Settings, reply: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Имена DLL от песочницы — простые и без повторов, иначе 503 (§7.5.7, А-122)."""
    async with code_portal(settings) as (portal, server):
        client = await portal.employee()
        pid = (await new_project(client))["id"]
        server.replies.append(succeeded(bundle=[("Some.dll", b"lib")]))
        assert (await client.post(f"/api/code/projects/{pid}/build")).status_code == 200
        before = (await client.get(f"/api/code/projects/{pid}")).json()["build"]
        archive = (await client.get(f"/api/code/projects/{pid}/build/archive")).content
        server.replies.append(reply)
        caplog.set_level(logging.WARNING)
        response = await client.post(f"/api/code/projects/{pid}/build")
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "build_unavailable"
        assert (await client.get(f"/api/code/projects/{pid}")).json()["build"] == before
        kept = await client.get(f"/api/code/projects/{pid}/build/archive")
        assert kept.content == archive
    refusals = [
        record for record in caplog.records if record.getMessage() == "code sandbox refused"
    ]
    assert [getattr(record, "reason", None) for record in refusals] == ["protocol"]


async def test_sandbox_package_dll_with_dots_in_name_is_archived(settings: Settings) -> None:
    name = "Microsoft.Extensions.Logging.Abstractions.dll"
    async with code_portal(settings) as (portal, server):
        client = await portal.employee()
        pid = (await new_project(client))["id"]
        server.replies.append(succeeded(bundle=[(name, b"lib")], on_server=["Npgsql.dll"]))
        build = (await client.post(f"/api/code/projects/{pid}/build")).json()
        assert [item["file_name"] for item in build["archive"]["files"]][1] == name


async def test_second_button_build_is_refused_while_first_runs(settings: Settings) -> None:
    async with code_portal(settings) as (portal, server):
        client = await portal.employee()
        pid = (await new_project(client))["id"]
        gate = asyncio.Event()
        server.replies.append(gate)
        first = asyncio.create_task(client.post(f"/api/code/projects/{pid}/build"))
        while not server.requests:
            await asyncio.sleep(0.01)
        assert (await client.get(f"/api/code/projects/{pid}")).json()["build_running"] is True
        second = await client.post(f"/api/code/projects/{pid}/build")
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "build_in_progress"
        gate.set()
        assert (await first).status_code == 200
        assert (await client.get(f"/api/code/projects/{pid}")).json()["build_running"] is False


async def test_success_snapshot_keeps_entry_files_and_revision(settings: Settings) -> None:
    # Правка файла во время сборки: «Собрано» — с файлами и ревизией входа, ниже по порядку.
    async with code_portal(settings) as (portal, server):
        client = await portal.employee()
        project = await new_project(client)
        pid = project["id"]
        file = file_of(project, "AddContactTool.cs")
        gate = asyncio.Event()
        server.replies.append(gate)
        build = asyncio.create_task(client.post(f"/api/code/projects/{pid}/build"))
        while not server.requests:
            await asyncio.sleep(0.01)
        changed = await client.patch(
            f"/api/code/projects/{pid}/files/{file['id']}",
            json={"revision": file["revision"], "content": "// during build"},
        )
        assert changed.status_code == 200, changed.text
        gate.set()
        response = await build
        assert response.status_code == 200 and response.json()["outdated"] is True
        rows = await portal.rows(
            "SELECT s.reason, s.project_revision, s.files, p.revision FROM code_snapshots s "
            "JOIN code_projects p ON p.id = s.project_id WHERE s.project_id = :id",
            id=pid,
        )
        assert [(row.reason, row.revision - row.project_revision) for row in rows] == [
            ("build_succeeded", 1)
        ]
        assert "// during build" not in json.dumps(rows[0].files, ensure_ascii=False)
        snapshot = (await client.get(f"/api/code/projects/{pid}/snapshots")).json()["items"][0]
        await client.post(f"/api/code/projects/{pid}/snapshots/{snapshot['id']}/restore")
        items = (await client.get(f"/api/code/projects/{pid}/snapshots")).json()["items"]
        assert [item["reason"] for item in items] == [
            "restored",
            "before_restore",
            "build_succeeded",
        ]


async def test_build_waits_for_queue_and_place_then_is_unavailable(settings: Settings) -> None:
    build = settings.code.build.model_copy(update={"max_parallel": 1, "wait_seconds": 0.2})
    async with httpx.AsyncClient() as http:
        sandbox = HttpCodeSandbox(http, build)
        first, second = uuid4(), uuid4()
        async with sandbox.slot(first, button=True):
            with pytest.raises(BuildInProgressError):
                async with sandbox.slot(first, button=True):
                    pass
            # Сборка цикла того же проекта и сборка другого проекта ждут, затем отказ.
            for project, button in ((first, False), (second, True)):
                waiting = asyncio.create_task(_enter(sandbox, project, button=button))
                await asyncio.sleep(0.05)
                assert sandbox.running(project)
                with pytest.raises(BuildUnavailableError):
                    await waiting
            assert not sandbox.running(second)
        assert not sandbox.running(first)
        async with sandbox.slot(second, button=True):
            assert sandbox.running(second)


async def _enter(sandbox: HttpCodeSandbox, project: UUID, *, button: bool) -> None:
    async with sandbox.slot(project, button=button):
        pass


async def test_sandbox_deadline_is_timeout_result(settings: Settings) -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=succeeded())

    build = settings.code.build.model_copy(update={"timeout_seconds": 0.1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
        sandbox = HttpCodeSandbox(http, build)
        async with sandbox.slot(uuid4(), button=True) as slot:
            result = await slot.compile("AddressTool", [CompileFile("A.cs", "class A {}")], [])
    assert result.status == "timeout" and result.dll is None


async def test_build_log_has_numbers_only(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "СЕКРЕТНЫЙ_МАРКЕР_КОДА"
    async with code_portal(settings) as (portal, server):
        client = await portal.employee()
        project = await new_project(client)
        await _add(client, project["id"], f"{marker}.cs", f"// {marker}")
        server.replies.append(failed(message=marker))
        caplog.set_level(logging.DEBUG)
        await client.post(f"/api/code/projects/{project['id']}/build")
        server.replies.append((500, {"error": "internal", "message": marker}))
        await client.post(f"/api/code/projects/{project['id']}/build")
    logged = [json.dumps(record.__dict__, default=str) for record in caplog.records]
    assert logged and not any(marker in line for line in logged)
    assert any(record.getMessage() == "code build" for record in caplog.records)


async def test_versions_restore_and_limit(settings: Settings) -> None:
    async with code_portal(settings, max_snapshots=2) as (portal, _):
        client = await portal.employee()
        project = await new_project(client)
        pid = project["id"]
        file = file_of(project, "AddContactTool.cs")
        original = file["content"]
        await client.post(f"/api/code/projects/{pid}/build")
        await client.patch(
            f"/api/code/projects/{pid}/files/{file['id']}",
            json={"revision": file["revision"], "content": "// changed"},
        )
        first = (await client.get(f"/api/code/projects/{pid}/snapshots")).json()["items"][0]
        restored = await client.post(f"/api/code/projects/{pid}/snapshots/{first['id']}/restore")
        assert restored.status_code == 200, restored.text
        assert file_of(restored.json(), "AddContactTool.cs")["content"] == original
        items = (await client.get(f"/api/code/projects/{pid}/snapshots")).json()["items"]
        # Новые сверху; сверх предела старая версия удалена и больше не находится.
        assert [item["reason"] for item in items] == ["restored", "before_restore"]
        assert items[0]["restored_from"] == first["created_at"]
        gone = await client.post(f"/api/code/projects/{pid}/snapshots/{first['id']}/restore")
        assert gone.status_code == 404


async def test_build_disabled_closes_build_and_archive_only(settings: Settings) -> None:
    async with code_portal(settings, build=False) as (portal, server):
        client = await portal.employee()
        pid = (await new_project(client))["id"]
        for response in (
            await client.post(f"/api/code/projects/{pid}/build"),
            await client.get(f"/api/code/projects/{pid}/build/archive"),
        ):
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "feature_disabled"
        assert (await client.get(f"/api/code/projects/{pid}")).status_code == 200
        assert server.requests == []
        config = (await client.get("/api/config")).json()["code"]
        assert config["enabled"] is True and config["build_enabled"] is False


async def _insert_dialog(portal: Portal, login: str, kind: str) -> str:
    """Диалог прямо в базе: так появляются проект при выключенном разделе и диалог CoGIS."""
    owner = await portal.user_id(login)
    project = uuid4() if kind == "code" else None
    if project is not None:
        await portal.execute(
            "INSERT INTO code_projects (id, owner_id, name, created_at, updated_at) "
            "VALUES (:id, :owner, 'Old', now(), now())",
            id=project,
            owner=owner,
        )
    dialog = uuid4()
    await portal.execute(
        "INSERT INTO dialogs (id, owner_id, kind, project_id, created_at, updated_at) "
        "VALUES (:id, :owner, :kind, :project, now(), now())",
        id=dialog,
        owner=owner,
        kind=kind,
        project=project,
    )
    return str(dialog)


async def test_code_disabled_closes_section_and_project_dialogs(portal: Portal) -> None:
    client = await portal.employee()
    dialog = await _insert_dialog(portal, "ivanov", "code")
    for path in ("/api/code/projects", "/api/code/sdk", f"/api/dialogs/{dialog}/messages"):
        response = await client.get(path)
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "feature_disabled"
    sent = await client.post(f"/api/dialogs/{dialog}/messages", json={"content": "x"})
    assert sent.status_code == 403
    # Прежний помощник CoGIS — как в 0.8.
    assert (await client.post("/api/dialogs", json={"kind": "cogis"})).status_code == 201
    assert (await client.get("/api/config")).json()["code"]["enabled"] is False


async def test_old_cogis_dialogs_are_read_only_when_code_enabled(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        created = await client.post("/api/dialogs", json={"kind": "cogis"})
        assert created.status_code == 422
        assert created.json()["error"]["fields"][0] == {
            "field": "kind",
            "code": "unknown_value",
            "message": created.json()["error"]["fields"][0]["message"],
        }
        dialog = await _insert_dialog(portal, "ivanov", "cogis")
        assert (await client.get(f"/api/dialogs/{dialog}")).status_code == 200
        refusals = [
            await client.post(
                f"/api/dialogs/{dialog}/messages", json={"content": "x", "action": "write"}
            ),
            await client.post(f"/api/dialogs/{dialog}/regenerate"),
            await client.patch(f"/api/dialogs/{dialog}", json={"title": "Новое"}),
            await client.post(f"/api/dialogs/{dialog}/memory/draft", json={"purpose": "compress"}),
            await client.put(
                f"/api/dialogs/{dialog}/memory/summary",
                json={"text": "x", "until_message_id": dialog},
            ),
            await client.delete(f"/api/dialogs/{dialog}/memory/summary"),
        ]
        for response in refusals:
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "dialog_read_only"
        assert (await client.delete(f"/api/dialogs/{dialog}")).status_code == 204


async def test_sdk_reference_is_cached_by_etag_only_here(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        first = await client.get("/api/code/sdk")
        assert first.status_code == 200
        assert first.headers["cache-control"] == "private, no-cache"
        body = first.json()
        assert body["available"] is False and body["types"] == []
        assert all(item["on_server"] is False for item in body["packages"])
        etag = first.headers["etag"]
        again = await client.get("/api/code/sdk", headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["etag"] == etag
        anonymous = portal.client()
        refused = await anonymous.get("/api/code/sdk", headers={"If-None-Match": etag})
        assert refused.status_code == 401
        assert refused.headers["cache-control"] == "no-store"
        project = await new_project(client)
        for path in (f"/api/code/projects/{project['id']}", "/api/config"):
            assert (await client.get(path)).headers["cache-control"] == "no-store"
