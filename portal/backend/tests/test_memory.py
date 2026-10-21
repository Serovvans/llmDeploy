"""Память диалога 0.7: оценка, режимы пар, сборка запроса, расход (docs/portal-api.md §5.10)."""

import asyncio
import json
import logging
import math
from typing import Any

import httpx
import pytest
from llm_stub.app import create_app as create_stub

from portal.core.logging import JsonFormatter
from portal.core.settings import Settings
from portal.kb.ports import Retrieval
from portal.llm.ports import (
    ChatRequest,
    ContentDelta,
    Finished,
    ModelUnavailableError,
    TextPart,
)
from tests import samples
from tests.conftest import close_portal, make_portal
from tests.support import Portal, ask, live_server, new_dialog, parse_events, post_events, upload
from tests.test_chat_stream import _len_is, _messages, _until
from tests.test_sql_run import DATA, FakeSandbox, _dialog, _enabled, _sql

pytestmark = pytest.mark.anyio

RULES = "Правила базы знаний."
CONTEXT = "<база_знаний>\n[1] Договор.pdf\nсроком на 49 лет\n</база_знаний>"
FAST = {"mode": "fast", "search": "false"}


def _settings(settings: Settings, *, max_model_len: int = 65536, memory: bool = True) -> Settings:
    flag = settings.memory.model_copy(update={"enabled": memory})
    return settings.model_copy(update={"max_model_len": max_model_len, "memory": flag})


async def _memory(client: httpx.AsyncClient, dialog_id: str, **params: str) -> dict[str, Any]:
    response = await client.get(f"/api/dialogs/{dialog_id}/memory", params=params or FAST)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def _set_mode(
    client: httpx.AsyncClient, dialog_id: str, message_id: str, mode: str
) -> httpx.Response:
    return await client.patch(
        f"/api/dialogs/{dialog_id}/messages/{message_id}", json={"memory_mode": mode}
    )


async def _pair(
    portal: Portal,
    client: httpx.AsyncClient,
    dialog_id: str,
    question: str,
    answer: str,
    **extra: Any,
) -> tuple[str, str]:
    """Задать вопрос с заданным ответом модели; вернуть идентификаторы вопроса и ответа."""
    portal.model.scripts.append([ContentDelta(answer), Finished("stop")])
    events = await ask(client, dialog_id, question, **extra)
    assert events[-1][0] == "done", events
    start = dict(events)["start"]
    return start["user_message_id"], start["assistant_message_id"]


def _text(request: ChatRequest) -> str:
    return "\n".join(
        part.text
        for message in request.messages
        for part in message.parts
        if isinstance(part, TextPart)
    )


async def _images(client: httpx.AsyncClient, dialog_id: str, count: int) -> list[str]:
    return [
        (await upload(client, dialog_id, f"{n}.png", samples.png())).json()["id"]
        for n in range(count)
    ]


# --- одна оценка (CLAUDE.md, правило 1) ---


@pytest.mark.parametrize("delta", [-1, 1])
async def test_overflow_level_matches_dropping_at_the_budget_edge(
    settings: Settings, delta: int
) -> None:
    """«Переполнена» ⇔ следующий запрос отбросит начало; граница — ±1 токен.

    Вопрос «x» занимает ровно один токен, поэтому запросу под историю остаётся
    `limit_tokens − 1`: при `used = limit − 1` всё помещается, при `limit + 1` — нет.
    """
    portal = await make_portal(_settings(settings, max_model_len=20000))
    try:
        client = await portal.employee()
        dialog_id = await new_dialog(client)
        await _pair(portal, client, dialog_id, "яя", "яя")
        before = await _memory(client, dialog_id)
        assert (before["used_tokens"], before["level"]) == (2, "free")
        limit = before["limit_tokens"]
        await _pair(portal, client, dialog_id, "яя", "я" * 2 * (limit + delta - 3))

        memory = await _memory(client, dialog_id)
        assert memory["used_tokens"] == limit + delta and memory["limit_tokens"] == limit
        overflow = delta > 0
        assert memory["level"] == ("overflow" if overflow else "almost_full")
        assert [pair["dropped"] for pair in memory["pairs"]] == [overflow, False]
        assert memory["percent"] == (limit + delta) * 100 // limit

        events = await ask(client, dialog_id, "x")
        truncated = dict(events).get("context_truncated")
        assert truncated == ({"dropped_messages": 2} if overflow else None)
    finally:
        await close_portal(portal)


async def test_image_limit_text_only_and_answer_settings_change_the_estimate(
    portal: Portal, settings: Settings
) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    first, _ = await _pair(
        portal,
        client,
        dialog_id,
        "Первый",
        "Ответ",
        attachment_ids=await _images(client, dialog_id, 4),
    )
    await _pair(
        portal,
        client,
        dialog_id,
        "Второй",
        "Ответ",
        attachment_ids=await _images(client, dialog_id, 4),
    )
    # Восемь картинок в истории и вопрос без картинок — предел 8 не превышен.
    assert (await _memory(client, dialog_id))["level"] != "overflow"
    await _pair(
        portal,
        client,
        dialog_id,
        "Третий",
        "Ответ",
        attachment_ids=await _images(client, dialog_id, 1),
    )
    memory = await _memory(client, dialog_id)
    assert memory["level"] == "overflow"
    assert [(p["attachments"], p["dropped"]) for p in memory["pairs"]] == [
        (4, True), (4, False), (1, False),
    ]  # fmt: skip

    # «Без вложений» у первой пары снимает её картинки и освобождает предел.
    changed = await _set_mode(client, dialog_id, first, "text_only")
    assert changed.status_code == 200 and changed.json()["memory_mode"] == "text_only"
    after = await _memory(client, dialog_id)
    assert after["level"] != "overflow" and not any(p["dropped"] for p in after["pairs"])
    image = settings.llm.tokens_per_image
    assert memory["pairs"][0]["tokens"] - after["pairs"][0]["tokens"] >= 4 * image
    events = await ask(client, dialog_id, "Четвёртый")
    assert "context_truncated" not in dict(events)
    sent = portal.model.requests[-1]
    assert "Первый" in _text(sent)
    assert sum(len(m.parts) - 1 for m in sent.messages if m.role == "user" and m.parts) == 5

    # Режим ответа и выбор базы знаний меняют объём под разговор (ПД §5.7).
    fast = (await _memory(client, dialog_id))["limit_tokens"]
    thorough = (await _memory(client, dialog_id, mode="thorough", search="false"))["limit_tokens"]
    searched = (await _memory(client, dialog_id, mode="fast", search="true"))["limit_tokens"]
    tokens = settings.chat.max_output_tokens
    assert fast - thorough == tokens.thorough - tokens.fast
    assert fast - searched == settings.kb.context_max_tokens


# --- учёт пар в запросе и в переписывании поискового запроса ---


async def test_pair_modes_shape_the_request_search_history_and_regeneration(
    portal: Portal,
) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    contract = (await upload(client, dialog_id, "договор.txt", b"tekst-vlozheniya")).json()["id"]
    first, _ = await _pair(
        portal, client, dialog_id, "Вопрос про договор", "Ответ про договор",
        attachment_ids=[contract],
    )  # fmt: skip
    extra, _ = await _pair(portal, client, dialog_id, "Лишний вопрос", "Лишний ответ")
    assert (await _set_mode(client, dialog_id, first, "text_only")).status_code == 200
    assert (await _set_mode(client, dialog_id, extra, "excluded")).status_code == 200

    portal.knowledge.result = Retrieval((), RULES, "")
    appendix = (await upload(client, dialog_id, "приложение.txt", b"dannye-tretyego")).json()["id"]
    third, _ = await _pair(
        portal, client, dialog_id, "Третий вопрос", "Третий ответ",
        attachment_ids=[appendix], collections=["shared"],
    )  # fmt: skip
    text = _text(portal.model.requests[-1])
    assert "Вопрос про договор" in text and "Ответ про договор" in text
    assert "tekst-vlozheniya" not in text and "договор.txt" not in text
    assert "Лишний" not in text
    assert [turn.text for turn in portal.knowledge.histories[-1]] == [
        "Вопрос про договор", "Ответ про договор",
    ]  # fmt: skip
    modes = [message["memory_mode"] for message in await _messages(client, dialog_id)]
    assert modes == ["text_only", "text_only", "excluded", "excluded", "full", "full"]

    # Повторная генерация следует текущему выбору, а сам вопрос уходит целиком.
    assert (await _set_mode(client, dialog_id, first, "full")).status_code == 200
    assert (await _set_mode(client, dialog_id, third, "text_only")).status_code == 200
    regenerated = parse_events((await client.post(f"/api/dialogs/{dialog_id}/regenerate")).text)
    assert regenerated[-1] == ("done", {"status": "complete"})
    request = portal.model.requests[-1]
    assert "tekst-vlozheniya" in _text(request) and "Лишний" not in _text(request)
    last = request.messages[-1].parts[0]
    assert isinstance(last, TextPart)
    assert "dannye-tretyego" in last.text and last.text.endswith("Третий вопрос")
    assert [turn.text for turn in portal.knowledge.histories[-1]] == [
        "Вопрос про договор", "Ответ про договор",
    ]  # fmt: skip


# --- флаг (CLAUDE.md, правило 2) ---


async def _long_dialog_answer(portal: Portal) -> list[tuple[str, dict[str, Any]]]:
    """Диалог у границы бюджета: длинная пара и исключённая пара, затем вопрос с поиском."""
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    await _pair(portal, client, dialog_id, "Длинный вопрос", "я" * 12000)
    excluded, _ = await _pair(portal, client, dialog_id, "Исключённый вопрос", "Исключённый ответ")
    await portal.execute(
        "UPDATE messages SET memory_mode = 'excluded' WHERE id = CAST(:id AS uuid)", id=excluded
    )
    portal.knowledge.result = Retrieval((), RULES, CONTEXT)
    return await ask(client, dialog_id, "Третий", collections=["shared"])


async def test_flag_off_builds_request_as_before_but_keeps_saved_modes(
    settings: Settings,
) -> None:
    small = 20000  # бюджет 11 808: длинная пара (~6000) помещается без резерва и не с ним
    off = await make_portal(_settings(settings, max_model_len=small, memory=False))
    try:
        events = await _long_dialog_answer(off)
        assert "context_truncated" not in dict(events)
        request = off.model.requests[-1]
        assert request.include_usage is False
        assert "Исключённый" not in _text(request)
        system = request.messages[0].parts[0]
        assert isinstance(system, TextPart) and system.text.endswith(RULES)
        assert "сроком на 49 лет" in _text(request)
        client = await off.employee("petrova")
        own = await new_dialog(client)
        question, _ = await _pair(off, client, own, "Вопрос", "Ответ")
        for response in (
            await client.get(f"/api/dialogs/{own}/memory", params=FAST),
            await _set_mode(client, own, question, "excluded"),
        ):
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "feature_disabled"
        assert (await _messages(client, own))[0]["memory_mode"] == "full"
    finally:
        await close_portal(off)

    on = await make_portal(_settings(settings, max_model_len=small))
    try:
        events = await _long_dialog_answer(on)
        # С флагом под найденное — весь резерв kb.context_max_tokens, длинная пара не влезла.
        assert dict(events)["context_truncated"] == {"dropped_messages": 2}
        request = on.model.requests[-1]
        assert request.include_usage is True and "Исключённый" not in _text(request)
    finally:
        await close_portal(on)


# --- занятость (CLAUDE.md, правило 5) ---


async def test_mode_is_not_changed_while_an_answer_is_forming(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    first, _ = await _pair(portal, client, dialog_id, "Первый", "Ответ")
    gate = asyncio.Event()
    portal.model.scripts.append([ContentDelta("Идёт"), gate, Finished("stop")])
    forming = asyncio.create_task(ask(client, dialog_id, "Второй"))
    await _until(lambda: _len_is(portal.model.requests, 2))

    busy = await _set_mode(client, dialog_id, first, "excluded")
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "generation_in_progress")
    # Оценка во время ответа отвечает: идущий ответ пока пуст и в историю не входит.
    memory = await _memory(client, dialog_id)
    assert [pair["question_preview"] for pair in memory["pairs"]] == ["Первый", "Второй"]
    gate.set()
    assert (await forming)[-1] == ("done", {"status": "complete"})
    assert (await _messages(client, dialog_id))[0]["memory_mode"] == "full"

    # Зависший `streaming` без живой задачи не держит диалог: он прерывается.
    await portal.execute("UPDATE messages SET status = 'streaming' WHERE position = 3")
    assert (await _set_mode(client, dialog_id, first, "excluded")).status_code == 200
    answer = (await _messages(client, dialog_id))[-1]
    assert (answer["status"], answer["error_code"]) == ("error", "interrupted")


async def test_mode_is_not_changed_while_sql_is_checked(settings: Settings) -> None:
    sandbox = FakeSandbox()
    checked = await make_portal(_enabled(settings), sql_sandbox=sandbox)
    checked.model.sql_data = DATA
    try:
        client = await checked.employee()
        dialog_id, body = await _dialog(client)
        path = f"/api/dialogs/{dialog_id}/messages"
        checked.model.scripts.append([ContentDelta(_sql("SELECT 1")), Finished("stop")])
        start = dict(await post_events(client, path, {**body, "test_run": False}))["start"]
        sandbox.script = [gate := asyncio.Event()]
        checked.model.scripts.append([ContentDelta(_sql("SELECT 2")), Finished("stop")])
        task = asyncio.create_task(post_events(client, path, body))
        await asyncio.wait_for(sandbox.started.wait(), timeout=5)

        busy = await _set_mode(client, dialog_id, start["user_message_id"], "excluded")
        assert (busy.status_code, busy.json()["error"]["code"]) == (409, "generation_in_progress")
        gate.set()
        assert (await asyncio.wait_for(task, timeout=5))[-1][0] == "done"
        done = await _set_mode(client, dialog_id, start["user_message_id"], "excluded")
        assert done.status_code == 200
    finally:
        await close_portal(checked)


# --- изоляция и проверки (CLAUDE.md, правило 4) ---


async def test_foreign_dialog_answer_and_bad_input_are_refused(portal: Portal) -> None:
    owner = await portal.employee("ivanov")
    dialog_id = await new_dialog(owner)
    question, answer = await _pair(portal, owner, dialog_id, "Секретный вопрос", "Секретный ответ")
    stranger = await portal.employee("petrova")
    admin = portal.client()
    await portal.onboard(admin, "boss", role="admin")
    for client in (stranger, admin):
        own = await new_dialog(client)
        for response in (
            await client.get(f"/api/dialogs/{dialog_id}/memory", params=FAST),
            await _set_mode(client, dialog_id, question, "excluded"),
            # Чужое сообщение не меняется и через свой диалог.
            await _set_mode(client, own, question, "excluded"),
        ):
            assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")
            assert "Секрет" not in response.text

    for target in (answer, "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
        response = await _set_mode(owner, dialog_id, target, "excluded")
        assert response.status_code == 404, target

    def fields(response: httpx.Response) -> list[tuple[str, str]]:
        assert response.status_code == 422, response.text
        return [(item["field"], item["code"]) for item in response.json()["error"]["fields"]]

    assert fields(await _set_mode(owner, dialog_id, question, "text_only")) == [
        ("memory_mode", "invalid_format")
    ]
    assert fields(await _set_mode(owner, dialog_id, question, "pinned")) == [
        ("memory_mode", "unknown_value")
    ]
    memory_path = f"/api/dialogs/{dialog_id}/memory"
    assert fields(await owner.get(memory_path, params={"search": "false"})) == [
        ("mode", "required")
    ]
    assert fields(await owner.get(memory_path, params={"mode": "fast", "search": "yes"})) == [
        ("search", "unknown_value")
    ]
    sql = (await owner.post("/api/dialogs", json={"kind": "sql"})).json()["id"]
    missing = await owner.get(
        f"/api/dialogs/{sql}/memory",
        params={"action": "write", "dialect": "postgres", "schema_id": str(question)},
    )
    assert (missing.status_code, missing.json()["error"]["code"]) == (422, "schema_not_found")
    assert (await _messages(owner, dialog_id))[0]["memory_mode"] == "full"


# --- расход запроса (CLAUDE.md, правило 7) ---


async def test_usage_is_logged_as_numbers_only_and_its_absence_is_harmless(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    async with live_server(create_stub(chunk_delay_ms=0)) as url:
        llm = settings.llm.model_copy(update={"base_url": f"{url}/v1"})
        portal = await make_portal(settings.model_copy(update={"llm": llm}), scripted_model=False)
        try:
            client = await portal.employee()
            dialog_id = await new_dialog(client)
            caplog.set_level(logging.INFO)
            question = "Какой срок аренды участка?"
            events = await ask(client, dialog_id, question)
            assert events[-1] == ("done", {"status": "complete"})
            records = [r for r in caplog.records if r.getMessage() == "prompt usage"]
            assert len(records) == 1
            line = JsonFormatter().format(records[0])
            entry = json.loads(line)
            numbers = {
                key: entry[key] for key in set(entry) - {"time", "level", "logger", "message"}
            }
            assert set(numbers) == {
                "answer_id", "estimated_tokens", "prompt_tokens", "text_chars", "images",
            }  # fmt: skip
            assert numbers["answer_id"] == dict(events)["start"]["assistant_message_id"]
            system = settings.chat.system_prompt
            assert numbers["text_chars"] == len(system) + len(question)
            assert numbers["images"] == 0
            assert numbers["estimated_tokens"] == math.ceil(len(system) / 2) + math.ceil(
                len(question) / 2
            )
            # Заглушка считает по три символа на токен — в журнале видны оба числа.
            assert numbers["prompt_tokens"] == math.ceil(numbers["text_chars"] / 3)
            assert "аренды" not in line and "Ты — помощник" not in line

            caplog.clear()
            events = await ask(client, await new_dialog(client), "Вопрос [[stub:no-usage]]")
            assert events[-1] == ("done", {"status": "complete"})
            assert not [r for r in caplog.records if r.getMessage() == "prompt usage"]
        finally:
            await close_portal(portal)


async def test_title_request_skips_excluded_first_question(portal: Portal) -> None:
    """Исключённая пара не уходит модели и в отдельном запросе названия (критерий 30)."""
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.title = ModelUnavailableError()
    first, _ = await _pair(portal, client, dialog_id, "Секретный первый вопрос", "Ответ")
    assert (await client.get(f"/api/dialogs/{dialog_id}")).json()["title"] is None
    assert (await _set_mode(client, dialog_id, first, "excluded")).status_code == 200

    portal.model.title = "Название"
    await _pair(portal, client, dialog_id, "Второй вопрос", "Ответ")
    sent = _text(portal.model.title_requests[-1])
    assert "Второй вопрос" in sent and "Секретный" not in sent
