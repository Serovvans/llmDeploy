"""«Исправить с помощником» по ошибкам предпросмотра (docs/portal-api.md §7.5.16, А-148).

Текст вопроса собирает сервер из `preview_fix`. При слое из образца в истории и в запросе к
модели — только заголовок `header_sample`, тип ошибки из закрытого списка, путь файла
проекта, строка и директива: метка в любом другом поле запрос не проходит.
"""

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from portal.code.preview_fix import DIRECTIVES
from portal.core.settings import Settings
from portal.kb.ports import Retrieval
from portal.llm.ports import ChatRequest, ContentDelta, Finished, TextPart
from tests.code_support import code_portal, new_project
from tests.support import Portal

pytestmark = pytest.mark.anyio

CADDYFILE = Path(__file__).resolve().parents[3] / "deploy" / "caddy" / "Caddyfile"
MARKER = "MarkerRow5b7e"
HEADER = "Исправить ошибки, которые появились в предпросмотре:"
HEADER_SAMPLE = (
    "Исправить ошибки, которые появились в предпросмотре. Слой был заполнен образцом из базы,"
    " поэтому текст ошибок не передан — только тип ошибки, файл и строка:"
)


def _body(preview_fix: Any, **fields: Any) -> dict[str, Any]:
    return {
        "action": "write",
        "file_ids": None,
        "target": None,
        "preview_fix": preview_fix,
        **fields,
    }


def _sample(*problems: dict[str, Any]) -> dict[str, Any]:
    return {"layer": "sample", "problems": list(problems)}


ERROR = {"kind": "error", "path": "plugin.js", "line": 2, "type": "TypeError"}
NETWORK = {"kind": "network", "path": None, "line": None, "directive": "connect-src"}


async def _project(portal: Portal, client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/code/projects", json={"source": "template", "kind": "js_plugin", "name": "Плагин"}
    )
    assert response.status_code == 201, response.text
    portal.knowledge.result = Retrieval((), "", "")
    project: dict[str, Any] = response.json()["project"]
    return project


async def _ask(
    portal: Portal, client: httpx.AsyncClient, project: dict[str, Any], body: dict[str, Any]
) -> httpx.Response:
    portal.model.scripts.append([ContentDelta("Готово."), Finished("stop")])
    return await client.post(f"/api/dialogs/{project['dialog_id']}/messages", json=body)


def _user_texts(request: ChatRequest) -> list[str]:
    return [
        part.text
        for message in request.messages
        if message.role == "user"
        for part in message.parts
        if isinstance(part, TextPart)
    ]


async def _question(client: httpx.AsyncClient, project: dict[str, Any]) -> str:
    items = (await client.get(f"/api/dialogs/{project['dialog_id']}/messages")).json()["items"]
    content: str = next(item for item in items if item["role"] == "user")["content"]
    return content


async def test_sample_layer_question_has_only_type_file_line_and_directive(
    settings: Settings,
) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await _project(portal, client)
        no_line = {"kind": "error", "path": "plugin.js", "line": None, "type": "Error"}
        response = await _ask(portal, client, project, _body(_sample(ERROR, NETWORK, no_line)))
        assert response.status_code == 200, response.text
        expected = "\n".join(
            [
                HEADER_SAMPLE,
                "plugin.js, строка 2: TypeError",
                "место не определено: код обратился к сети (connect-src) — в предпросмотре"
                " сеть закрыта",
                "plugin.js: Error",
            ]
        )
        assert await _question(client, project) == expected
        user = _user_texts(portal.model.requests[-1])
        assert any(expected in text for text in user)
        assert all(expected not in part for part in _system(portal.model.requests[-1]))


def _system(request: ChatRequest) -> list[str]:
    return [
        part.text
        for message in request.messages
        if message.role == "system"
        for part in message.parts
        if isinstance(part, TextPart)
    ]


@pytest.mark.parametrize(
    ("problem", "code"),
    [
        ({**ERROR, "text": MARKER}, "invalid_format"),
        ({**ERROR, "type": MARKER}, "unknown_value"),
        ({**ERROR, "path": MARKER}, "unknown_value"),
        ({**ERROR, "line": 10_000}, "invalid_format"),
        ({**ERROR, "path": None}, "invalid_format"),
        ({**NETWORK, "directive": f"connect-src {MARKER}"}, "unknown_value"),
        ({**NETWORK, "directive": "connect-srcx"}, "unknown_value"),
        ({**NETWORK, "directive": "abc"}, "unknown_value"),
        ({**NETWORK, "directive": "frame-ancestors"}, "unknown_value"),
        ({**NETWORK, "type": "TypeError"}, "invalid_format"),
        ({**ERROR, "message": MARKER}, "invalid_format"),
        ({"kind": "warning", "path": None, "line": None}, "invalid_format"),
    ],
)
async def test_sample_layer_refuses_any_other_field(
    settings: Settings, problem: dict[str, Any], code: str
) -> None:
    """Метка в запрещённом поле — `validation_error`: ни в историю, ни модели она не идёт."""
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await _project(portal, client)
        response = await _ask(portal, client, project, _body(_sample(problem)))
        assert response.status_code == 422
        (field,) = response.json()["error"]["fields"]
        assert (field["field"], field["code"]) == ("preview_fix", code)
        assert portal.model.requests == []
        assert MARKER not in str(await portal.rows("SELECT content, params::text FROM messages"))


async def test_invented_layer_passes_texts_as_is(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await _project(portal, client)
        problems = [
            {**ERROR, "text": "Uncaught TypeError: x is {path} undefined"},
            {**NETWORK, "text": "https://example.org/data"},
        ] + [{**ERROR, "text": f"ошибка {n}"} for n in range(10)]
        response = await _ask(
            portal, client, project, _body({"layer": "invented", "problems": problems})
        )
        assert response.status_code == 200, response.text
        lines = (await _question(client, project)).split("\n")
        assert lines[0] == HEADER
        assert lines[1] == "plugin.js, строка 2: Uncaught TypeError: x is {path} undefined"
        assert lines[2] == (
            "место не определено: код обратился к сети (https://example.org/data)"
            " — в предпросмотре сеть закрыта"
        )
        assert len(lines) == 12 and lines[-1] == "и ещё 2"
        missing = await _ask(
            portal,
            client,
            project,
            _body({"layer": "invented", "problems": [ERROR]}),
        )
        assert missing.json()["error"]["fields"][0]["code"] == "required"


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        (_body(_sample(ERROR), content="Исправь"), "content", "invalid_format"),
        (_body(_sample(ERROR), action="explain"), "action", "invalid_format"),
        (_body({"layer": "sample", "problems": []}), "preview_fix", "required"),
        (_body({"layer": "cached", "problems": [ERROR]}), "preview_fix", "invalid_format"),
        (_body({**_sample(ERROR), "extra": 1}), "preview_fix", "invalid_format"),
        (_body(_sample(*[ERROR] * 101)), "preview_fix", "too_many"),
    ],
)
async def test_preview_fix_form(
    settings: Settings, body: dict[str, Any], field: str, code: str
) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        project = await _project(portal, client)
        response = await _ask(portal, client, project, body)
        assert response.status_code == 422
        (error,) = response.json()["error"]["fields"]
        assert (error["field"], error["code"]) == (field, code)


async def test_preview_fix_is_only_for_web_projects(settings: Settings) -> None:
    async with code_portal(settings) as (portal, _):
        client = await portal.employee()
        gptool = await new_project(client)
        response = await _ask(portal, client, gptool, _body(_sample(ERROR)))
        (error,) = response.json()["error"]["fields"]
        assert (error["field"], error["code"]) == ("preview_fix", "invalid_format")
        foreign = await portal.employee("petrov")
        assert (await _ask(portal, foreign, gptool, _body(_sample(ERROR)))).status_code == 404


def test_directives_match_preview_page_header() -> None:
    """Список директив — из заголовка `/code-preview/*` в Caddyfile (§7.5.17, «Директивы»).

    Без `frame-ancestors` и `sandbox` (событий внутри фрейма не дают), с уточняющими
    `-elem`/`-attr` у `script-src` и `style-src`.
    """
    caddyfile = CADDYFILE.read_text(encoding="utf-8")
    found = re.search(
        r'Content-Security-Policy "([^"]*)"', caddyfile[caddyfile.index("/code-preview/*") :]
    )
    assert found is not None
    named = {part.split()[0] for part in found.group(1).split(";") if part.strip()}
    named -= {"frame-ancestors", "sandbox"}
    refined = {
        f"{name}-{part}" for name in ("script-src", "style-src") for part in ("elem", "attr")
    }
    assert named | refined == DIRECTIVES
