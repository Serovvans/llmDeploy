"""Поток ответа: порядок событий, сохранение, ошибки, остановка, название (§5.5–5.7, §6)."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from portal.core.settings import Settings
from portal.llm.ports import (
    ContentDelta,
    ContextOverflowError,
    Finished,
    ImagePart,
    ModelOverloadedError,
    ModelUnavailableError,
    ReasoningDelta,
    TextPart,
)
from tests import samples
from tests.conftest import close_portal, make_portal
from tests.support import CHAT_BODY, Portal, ask, live_server, new_dialog, parse_events, upload

pytestmark = pytest.mark.anyio


async def _messages(client: httpx.AsyncClient, dialog_id: str) -> list[dict[str, Any]]:
    """Сообщения диалога по порядку, от старых к новым."""
    items = (await client.get(f"/api/dialogs/{dialog_id}/messages")).json()["items"]
    return list(reversed(items))


async def _answer_status(portal: Portal) -> str:
    rows = await portal.rows("SELECT status FROM messages WHERE role = 'assistant'")
    return str(rows[-1].status)


async def _until(condition: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def _live_client(
    portal: Portal, client: httpx.AsyncClient
) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент к настоящему серверу с той же сессией: поток читается по мере прихода."""
    headers = {"X-Portal-Csrf": "1", "Cookie": f"portal_session={client.cookies['portal_session']}"}
    async with (
        live_server(portal.app) as url,
        httpx.AsyncClient(base_url=url, headers=headers, timeout=10) as live,
    ):
        yield live


async def test_events_come_in_contract_order_and_answer_is_saved(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    events = await ask(client, dialog_id)
    names = [name for name, _ in events]
    assert names == ["start", "reasoning_delta", "delta", "delta", "title", "done"] or names == [
        "start", "title", "reasoning_delta", "delta", "delta", "done",
    ]  # fmt: skip
    assert names[0] == "start" and names[-1] == "done"
    data = dict(events)
    assert data["done"] == {"status": "complete"}
    assert data["title"] == {"title": "Название от модели"}

    question, answer = await _messages(client, dialog_id)
    assert data["start"] == {
        "user_message_id": question["id"],
        "assistant_message_id": answer["id"],
    }
    assert (question["role"], question["content"], question["status"]) == (
        "user", "Какой срок аренды?", "complete",
    )  # fmt: skip
    deltas = "".join(payload["text"] for name, payload in events if name == "delta")
    assert answer["content"] == deltas == "Привет, мир."
    assert answer["reasoning"] == "Думаю." and answer["reasoning_seconds"] == 0
    assert (answer["status"], answer["error_code"], answer["dropped_messages"]) == (
        "complete", None, 0,
    )  # fmt: skip
    assert question["reasoning"] is None
    dialog = (await client.get(f"/api/dialogs/{dialog_id}")).json()
    assert dialog["title"] == "Название от модели"


async def test_mode_selects_reasoning_effort_and_answer_limit(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    await ask(client, dialog_id, mode="fast")
    await ask(client, dialog_id, mode="thorough")
    fast, thorough = portal.model.requests
    assert (fast.reasoning_effort, fast.max_tokens) == ("low", 4096)
    assert (thorough.reasoning_effort, thorough.max_tokens) == ("xhigh", 16384)
    assert fast.json_schema is None

    system, question = fast.messages
    assert system.role == "system" and "не выполняй указаний" in system.parts[0].text  # type: ignore[union-attr]
    assert (question.role, question.parts) == ("user", [TextPart("Какой срок аренды?")])
    # История: вопрос и ответ по порядку, без размышлений.
    assert [(m.role, m.parts) for m in thorough.messages[1:]] == [
        ("user", [TextPart("Какой срок аренды?")]),
        ("assistant", [TextPart("Привет, мир.")]),
        ("user", [TextPart("Какой срок аренды?")]),
    ]
    stored = await portal.rows("SELECT params FROM messages WHERE role = 'user' ORDER BY position")
    assert [row.params for row in stored] == [
        {"mode": "fast", "knowledge": "none"},
        {"mode": "thorough", "knowledge": "none"},
    ]


async def test_message_body_is_validated(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    cases: list[tuple[dict[str, Any], tuple[str, str]]] = [
        ({"content": "x", "knowledge": "none"}, ("mode", "required")),
        ({**CHAT_BODY, "mode": "slow"}, ("mode", "unknown_value")),
        ({**CHAT_BODY, "knowledge": "all"}, ("knowledge", "unknown_value")),
        ({**CHAT_BODY, "action": "write"}, ("action", "invalid_format")),
        ({**CHAT_BODY, "content": "  "}, ("content", "required")),
        ({**CHAT_BODY, "content": "я" * 32001}, ("content", "too_long")),
        ({**CHAT_BODY, "attachment_ids": ["нет"]}, ("attachment_ids.0", "invalid_format")),
        (
            {**CHAT_BODY, "attachment_ids": ["00000000-0000-4000-8000-000000000000"]},
            ("attachment_ids", "invalid_format"),
        ),
    ]
    for body, expected in cases:
        response = await client.post(f"/api/dialogs/{dialog_id}/messages", json=body)
        assert response.status_code == 422, body
        fields = response.json()["error"]["fields"]
        assert [(item["field"], item["code"]) for item in fields] == [expected]
    not_object = await client.post(f"/api/dialogs/{dialog_id}/messages", json=["x"])
    assert not_object.status_code == 400
    assert await portal.rows("SELECT 1 FROM messages") == []
    assert portal.model.requests == []


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ModelUnavailableError(), "model_unavailable"),
        (ModelOverloadedError(), "model_overloaded"),
        (RuntimeError("подробности сбоя"), "internal_error"),
    ],
)
async def test_model_failure_mid_stream_keeps_partial_answer(
    portal: Portal, failure: Exception, code: str
) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.scripts.append([ContentDelta("Начало ответа"), failure])
    events = await ask(client, dialog_id)
    names = [name for name, _ in events]
    assert names[-1] == "error" and "done" not in names
    error = events[-1][1]
    assert error["code"] == code and error["message"] and "подробности" not in error["message"]
    answer = (await _messages(client, dialog_id))[1]
    assert (answer["status"], answer["error_code"], answer["content"]) == (
        "error", code, "Начало ответа",
    )  # fmt: skip


async def test_stream_without_finish_is_a_break(portal: Portal) -> None:
    """Поток, закончившийся без `finish_reason`, полным ответом не считается никогда."""
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.scripts.append([ContentDelta("Оборванный текст")])
    events = await ask(client, dialog_id)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "model_unavailable"
    answer = (await _messages(client, dialog_id))[1]
    assert (answer["status"], answer["content"]) == ("error", "Оборванный текст")


async def test_length_limit_is_reported_in_done(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.scripts.append([ContentDelta("Длинный ответ"), Finished("length")])
    events = await ask(client, dialog_id)
    assert events[-1] == ("done", {"status": "length_limit"})
    assert (await _messages(client, dialog_id))[1]["status"] == "length_limit"


async def test_knowledge_base_is_honestly_unavailable_until_stage_four(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    for knowledge in ("shared", "shared_and_personal"):
        events = await ask(client, dialog_id, knowledge=knowledge)
        assert [name for name, _ in events if name != "title"] == [
            "start", "search_started", "error",
        ]  # fmt: skip
        assert events[-1][1]["code"] == "knowledge_unavailable"
    assert portal.model.requests == []
    answers = [m for m in await _messages(client, dialog_id) if m["role"] == "assistant"]
    assert {(m["status"], m["error_code"], m["content"]) for m in answers} == {
        ("error", "knowledge_unavailable", "")
    }


async def test_empty_answers_are_skipped_in_history(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.scripts.append([ModelUnavailableError()])
    await ask(client, dialog_id, "Первый")
    await ask(client, dialog_id, "Второй")
    assert [(m.role, m.parts) for m in portal.model.requests[1].messages[1:]] == [
        ("user", [TextPart("Первый")]),
        ("user", [TextPart("Второй")]),
    ]


# --- название ---


async def test_title_is_cleaned_and_never_overwrites_user_title(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.title = '\n  "Очень длинное название ' + "а" * 100 + '"\nвторая строка'
    events = await ask(client, dialog_id, "Первый вопрос " + "я" * 3000)
    title = dict(events)["title"]["title"]
    assert len(title) == 80 and title.startswith("Очень длинное название")
    request = portal.model.title_requests[0]
    assert (request.reasoning_effort, request.max_tokens) == ("low", 256)
    assert len(request.messages[1].parts[0].text) == 2000  # type: ignore[union-attr]

    # Название уже есть: модель больше не спрашивают.
    events = await ask(client, dialog_id, "Второй вопрос")
    assert "title" not in dict(events) and len(portal.model.title_requests) == 1

    other = await new_dialog(client)
    gate = asyncio.Event()
    portal.model.title = gate
    portal.model.scripts.append([ContentDelta("Ответ"), gate, Finished("stop")])
    task = asyncio.create_task(ask(client, other))
    await _until(lambda: _len_is(portal.model.title_requests, 2))
    await client.patch(f"/api/dialogs/{other}", json={"title": "Моё название"})
    gate.set()
    events = await task
    assert "title" not in dict(events)
    assert (await client.get(f"/api/dialogs/{other}")).json()["title"] == "Моё название"


async def _len_is(items: list[Any], expected: int) -> bool:
    return len(items) >= expected


async def test_title_failure_and_slowness_do_not_break_the_answer(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.title = ModelUnavailableError()
    events = await ask(client, dialog_id)
    assert [name for name, _ in events][-1] == "done" and "title" not in dict(events)

    # Название не успело за dialogs.title_wait_seconds: done уходит без него.
    portal.model.title = asyncio.Event()
    events = await ask(client, dialog_id)
    assert events[-1] == ("done", {"status": "complete"}) and "title" not in dict(events)
    assert (await client.get(f"/api/dialogs/{dialog_id}")).json()["title"] is None

    # Попытка повторяется при следующем ответе.
    portal.model.title = "Наконец-то"
    events = await ask(client, dialog_id)
    assert dict(events)["title"] == {"title": "Наконец-то"}
    assert len(portal.model.title_requests) == 3
    # В запрос названия идёт первый вопрос диалога, а не текущий.
    assert portal.model.title_requests[2].messages[1].parts == [TextPart("Какой срок аренды?")]


async def test_no_title_event_after_error(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.title = asyncio.Event()
    portal.model.scripts.append([ModelUnavailableError()])
    events = await ask(client, dialog_id)
    assert [name for name, _ in events] == ["start", "error"]


# --- повторная генерация ---


async def test_regenerate_replaces_last_answer(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    await ask(client, dialog_id, "Первый", mode="thorough")
    question, old_answer = await _messages(client, dialog_id)
    portal.model.scripts.append([ContentDelta("Другой ответ"), Finished("stop")])
    portal.clock.advance(minutes=1)
    response = await client.post(f"/api/dialogs/{dialog_id}/regenerate")
    assert response.status_code == 200
    events = parse_events(response.text)
    start = dict(events)["start"]
    assert start["user_message_id"] == question["id"]
    assert start["assistant_message_id"] != old_answer["id"]
    assert events[-1] == ("done", {"status": "complete"})

    messages = await _messages(client, dialog_id)
    assert [(m["id"], m["content"]) for m in messages] == [
        (question["id"], "Первый"),
        (start["assistant_message_id"], "Другой ответ"),
    ]
    # Те же сохранённые параметры вопроса.
    assert portal.model.requests[1].reasoning_effort == "xhigh"
    assert [m.role for m in portal.model.requests[1].messages] == ["system", "user"]
    dialog = (await client.get(f"/api/dialogs/{dialog_id}")).json()
    assert dialog["updated_at"] == "2026-10-05T09:01:00Z"


async def test_regenerate_answers_a_question_left_without_answer(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    empty = await client.post(f"/api/dialogs/{dialog_id}/regenerate")
    assert empty.status_code == 409
    assert empty.json()["error"]["code"] == "nothing_to_regenerate"

    await ask(client, dialog_id, "Вопрос")
    await portal.execute("DELETE FROM messages WHERE role = 'assistant'")
    response = await client.post(f"/api/dialogs/{dialog_id}/regenerate")
    assert parse_events(response.text)[-1] == ("done", {"status": "complete"})
    assert [m["role"] for m in await _messages(client, dialog_id)] == ["user", "assistant"]


# --- длина запроса ---


def _small_context(settings: Settings, max_model_len: int) -> Settings:
    return settings.model_copy(update={"max_model_len": max_model_len})


async def test_history_is_truncated_to_fit_the_context(settings: Settings) -> None:
    # Бюджет: 9000 − 4096 (ответ) − 4096 (запас) = 808 токенов; системное сообщение ~330.
    portal = await make_portal(_small_context(settings, 9000))
    try:
        client = await portal.employee()
        dialog_id = await new_dialog(client)
        for number in range(3):
            portal.model.scripts.append([ContentDelta("о" * 200), Finished("stop")])
            events = await ask(client, dialog_id, f"Вопрос {number} " + "я" * 200)
            assert "context_truncated" not in dict(events) or number > 0
        events = await ask(client, dialog_id, "Последний вопрос")
        names = [name for name, _ in events]
        dropped = dict(events)["context_truncated"]["dropped_messages"]
        assert dropped > 0 and dropped % 2 == 0
        assert names.index("context_truncated") < names.index("delta")
        assert names.count("context_truncated") == 1

        request = portal.model.requests[-1]
        assert [m.role for m in request.messages][:2] in (["system", "user"],)
        assert len(request.messages) == 1 + (6 - dropped) + 1
        assert (await _messages(client, dialog_id))[-1]["dropped_messages"] == dropped
    finally:
        await close_portal(portal)


async def test_question_that_cannot_fit_is_refused_before_the_stream(settings: Settings) -> None:
    portal = await make_portal(_small_context(settings, 9000))
    try:
        client = await portal.employee()
        dialog_id = await new_dialog(client)
        response = await client.post(
            f"/api/dialogs/{dialog_id}/messages", json={**CHAT_BODY, "content": "я" * 2000}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "message_too_long"
        assert await portal.rows("SELECT 1 FROM messages") == []
    finally:
        await close_portal(portal)


async def test_history_is_truncated_to_eight_images(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    for batch in (5, 4):
        ids = [
            (await upload(client, dialog_id, f"{n}.png", samples.png(color="red"))).json()["id"]
            for n in range(batch)
        ]
        events = await ask(client, dialog_id, "Что на фото?", attachment_ids=ids)
    assert dict(events)["context_truncated"] == {"dropped_messages": 2}
    request = portal.model.requests[-1]
    images = [p for m in request.messages for p in m.parts if isinstance(p, ImagePart)]
    assert len(images) == 4 and len(request.messages) == 2


async def test_context_overflow_is_retried_once_with_less_history(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    for number in range(3):
        await ask(client, dialog_id, f"Вопрос {number}")
    portal.model.scripts.append([ContextOverflowError()])
    events = await ask(client, dialog_id, "Четвёртый")
    assert events[-1] == ("done", {"status": "complete"})
    assert "context_truncated" not in dict(events)  # повторно событие не шлётся
    first, second = portal.model.requests[-2:]
    assert len(first.messages) == 8 and len(second.messages) == 4
    assert (await _messages(client, dialog_id))[-1]["dropped_messages"] == 4

    portal.model.scripts.extend([[ContextOverflowError()], [ContextOverflowError()]])
    events = await ask(client, dialog_id, "Пятый")
    assert events[-1][0] == "error" and events[-1][1]["code"] == "message_too_long"
    answer = (await _messages(client, dialog_id))[-1]
    assert (answer["status"], answer["error_code"]) == ("error", "message_too_long")


# --- параллельность, остановка, сессия, сроки ---


async def test_parallel_sends_to_one_dialog_do_not_both_pass(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    gate = asyncio.Event()
    portal.model.scripts.append([ContentDelta("Первый ответ"), gate, Finished("stop")])
    first = asyncio.create_task(ask(client, dialog_id, "Первый"))
    await _until(lambda: _len_is(portal.model.requests, 1))

    # Ответ формируется: второй запрос ждёт dialogs.stop_grace_seconds и получает отказ.
    second, third = await asyncio.gather(
        client.post(f"/api/dialogs/{dialog_id}/messages", json=CHAT_BODY),
        client.post(f"/api/dialogs/{dialog_id}/regenerate"),
    )
    for response in (second, third):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "generation_in_progress"
    gate.set()
    assert (await first)[-1] == ("done", {"status": "complete"})
    positions = await portal.rows("SELECT position FROM messages ORDER BY position")
    assert [row.position for row in positions] == [0, 1]


async def test_simultaneous_first_sends_are_serialized(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    results = await asyncio.gather(
        *(client.post(f"/api/dialogs/{dialog_id}/messages", json=CHAT_BODY) for _ in range(4))
    )
    assert all(response.status_code in (200, 409) for response in results)
    assert sum(response.status_code == 200 for response in results) >= 1
    positions = [row.position for row in await portal.rows("SELECT position FROM messages")]
    assert sorted(positions) == list(range(len(positions))) and len(positions) % 2 == 0


async def test_send_right_after_stop_waits_for_the_previous_answer(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    gate = asyncio.Event()
    portal.model.scripts.append([ContentDelta("Первый ответ"), gate, Finished("stop")])
    first = asyncio.create_task(ask(client, dialog_id, "Первый"))
    await _until(lambda: _len_is(portal.model.requests, 1))
    second = asyncio.create_task(ask(client, dialog_id, "Второй"))
    await asyncio.sleep(0.3)
    gate.set()  # прежний ответ сохраняется, пока второй запрос ждёт
    assert (await first)[-1][0] == "done" and (await second)[-1][0] == "done"
    assert len(await portal.rows("SELECT 1 FROM messages")) == 4


async def test_closing_the_connection_stops_and_saves_partial_answer(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    gate = asyncio.Event()
    portal.model.scripts.append(
        [ReasoningDelta("Думаю."), ContentDelta("Начало "), gate, ContentDelta("конец")]
    )
    async with _live_client(portal, client) as live:
        async with live.stream(
            "POST", f"/api/dialogs/{dialog_id}/messages", json=CHAT_BODY
        ) as response:
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            received = ""
            async for chunk in response.aiter_text():
                received += chunk
                # Закрытие обнаруживается и в паузе без текста — по строке keep-alive.
                if "Начало" in received and ": keep-alive" in received:
                    break
        await _until(lambda: _status_is(portal, "stopped"))

    names = [name for name, _ in parse_events(received)]
    assert "start" in names and "done" not in names and "error" not in names
    answer = (await _messages(client, dialog_id))[1]
    assert (answer["status"], answer["error_code"]) == ("stopped", None)
    assert (answer["content"], answer["reasoning"]) == ("Начало ", "Думаю.")
    assert portal.model.closed == 1  # запрос к модели отменён

    # После остановки ответ можно сформировать заново.
    response = await client.post(f"/api/dialogs/{dialog_id}/regenerate")
    assert parse_events(response.text)[-1] == ("done", {"status": "complete"})


async def _status_is(portal: Portal, status: str) -> bool:
    return await _answer_status(portal) == status


async def test_events_are_delivered_as_they_happen(portal: Portal) -> None:
    """Событие приходит клиенту до того, как модель продолжит: ответ не копится в буфере."""
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    gate = asyncio.Event()
    portal.model.scripts.append([ContentDelta("Первая часть"), gate, Finished("stop")])
    async with (
        _live_client(portal, client) as live,
        live.stream("POST", f"/api/dialogs/{dialog_id}/messages", json=CHAT_BODY) as response,
    ):
        received = ""
        async for chunk in response.aiter_text():
            received += chunk
            if "Первая часть" in received and not gate.is_set():
                assert "done" not in received
                gate.set()
    assert parse_events(received)[-1] == ("done", {"status": "complete"})


@pytest.mark.parametrize("cause", ["logout", "block", "reset_password"])
async def test_deleted_session_ends_the_stream(portal: Portal, cause: str) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.scripts.append([ContentDelta("Часть ответа"), asyncio.Event()])
    task = asyncio.create_task(ask(client, dialog_id))
    await _until(lambda: _len_is(portal.model.requests, 1))

    if cause == "logout":
        async with portal.client() as same_session:
            same_session.cookies.set(
                "portal_session",
                client.cookies["portal_session"],
                domain="portal.test",
                path="/api",
            )
            assert (await same_session.post("/api/auth/logout")).status_code == 204
    else:
        actor = await portal.actor()
        target = await portal.user_id("ivanov")
        if cause == "block":
            await portal.container.admin.set_blocked(actor, target, blocked=True)
        else:
            await portal.container.admin.reset_password(actor, target)

    events = await asyncio.wait_for(task, timeout=5)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "session_ended"
    row = (await portal.rows("SELECT * FROM messages WHERE role = 'assistant'"))[0]
    assert (row.status, row.error_code, row.content) == ("error", "session_ended", "Часть ответа")
    assert portal.model.closed == 1
    assert (await client.get(f"/api/dialogs/{dialog_id}")).status_code == 401


async def test_generation_timeout(settings: Settings) -> None:
    dialogs = settings.dialogs.model_copy(update={"generation_timeout_seconds": 1})
    portal = await make_portal(settings.model_copy(update={"dialogs": dialogs}))
    try:
        client = await portal.employee()
        dialog_id = await new_dialog(client)
        portal.model.scripts.append([ContentDelta("Долгий ответ"), asyncio.Event()])
        events = await asyncio.wait_for(ask(client, dialog_id), timeout=5)
        assert events[-1][0] == "error" and events[-1][1]["code"] == "generation_timeout"
        answer = (await _messages(client, dialog_id))[1]
        assert (answer["status"], answer["error_code"], answer["content"]) == (
            "error", "generation_timeout", "Долгий ответ",
        )  # fmt: skip
    finally:
        await close_portal(portal)


async def test_stuck_answers_become_interrupted_at_startup(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    await ask(client, dialog_id)
    await portal.execute("UPDATE messages SET status = 'streaming' WHERE role = 'assistant'")
    await portal.container.dialogs.reset_interrupted()
    answer = (await _messages(client, dialog_id))[1]
    assert (answer["status"], answer["error_code"]) == ("error", "interrupted")
    assert (await ask(client, dialog_id))[-1][0] == "done"


async def test_generation_leaves_no_message_text_in_logs(
    portal: Portal, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from portal.core.logging import JsonFormatter

    caplog.set_level(logging.DEBUG)
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.scripts.append([ContentDelta("Тайный ответ"), RuntimeError("Тайная ошибка")])
    await ask(client, dialog_id, "Тайный вопрос")
    output = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert "generation failed" in output and "Тайн" not in output


# --- сохранение ответа, удаление диалога, момент принятия вопроса ---


def _break_saving(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[int]:
    """Первые `failures` сохранений ответа падают, как при недоступной базе."""
    from portal.dialogs import generation
    from portal.dialogs.repositories import SqlDialogRepository

    calls: list[int] = []
    original = SqlDialogRepository.finish_answer

    async def flaky(self: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) <= failures:
            raise ConnectionRefusedError("portal-db")
        await original(self, *args, **kwargs)

    monkeypatch.setattr(SqlDialogRepository, "finish_answer", flaky)
    monkeypatch.setattr(generation, "_SAVE_RETRY_SECONDS", 0.01)
    return calls


async def test_saving_the_answer_is_retried(
    portal: Portal, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    calls = _break_saving(monkeypatch, failures=2)
    events = await ask(client, dialog_id)
    assert events[-1] == ("done", {"status": "complete"}) and len(calls) == 3
    answer = (await _messages(client, dialog_id))[1]
    assert (answer["status"], answer["content"]) == ("complete", "Привет, мир.")


async def test_failed_save_ends_the_stream_and_does_not_lock_the_dialog(
    portal: Portal, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    calls = _break_saving(monkeypatch, failures=4)
    events = await asyncio.wait_for(ask(client, dialog_id), timeout=5)
    # Поток закрыт, клиент узнаёт о сбое, а не ждёт бесконечно.
    assert events[-1][0] == "error" and events[-1][1]["code"] == "internal_error"
    assert len(calls) == 4 and await _answer_status(portal) == "streaming"
    record = next(r for r in caplog.records if r.getMessage() == "answer not saved")
    assert record.__dict__["error_type"] == "ConnectionRefusedError"
    assert any("generation.py" in place for place in record.__dict__["trace"])
    assert "portal-db" not in str(record.__dict__)

    # Задачи нет — зависший ответ объявляется прерванным уже при чтении сообщений:
    # интерфейс в состоянии «формируется» только перечитывает их.
    stuck = (await _messages(client, dialog_id))[1]
    assert (stuck["status"], stuck["error_code"]) == ("error", "interrupted")
    assert await _answer_status(portal) == "error"
    events = await asyncio.wait_for(ask(client, dialog_id, "Ещё раз"), timeout=5)
    assert events[-1] == ("done", {"status": "complete"})
    statuses = [(m["status"], m["error_code"]) for m in await _messages(client, dialog_id)]
    assert statuses == [
        ("complete", None), ("error", "interrupted"), ("complete", None), ("complete", None),
    ]  # fmt: skip


async def test_generation_failure_is_logged_with_code_locations(
    portal: Portal, caplog: pytest.LogCaptureFixture
) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.scripts.append([RuntimeError("подробности сбоя")])
    await ask(client, dialog_id)
    record = next(r for r in caplog.records if r.getMessage() == "generation failed")
    assert record.__dict__["error_type"] == "RuntimeError"
    assert any("support.py" in place for place in record.__dict__["trace"])
    assert "подробности" not in str(record.__dict__) and record.exc_info is None


async def test_deleting_the_dialog_ends_its_answer(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    attachment = (await upload(client, dialog_id, "a.txt", b"text")).json()["id"]
    portal.model.scripts.append([ContentDelta("Часть ответа"), asyncio.Event()])
    task = asyncio.create_task(ask(client, dialog_id, attachment_ids=[attachment]))
    await _until(lambda: _len_is(portal.model.requests, 1))

    assert (await client.delete(f"/api/dialogs/{dialog_id}")).status_code == 204
    events = await asyncio.wait_for(task, timeout=5)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "dialog_deleted"
    assert portal.model.closed == 1  # запрос к модели отменён
    for table in ("dialogs", "messages", "attachments"):
        assert await portal.rows(f"SELECT 1 FROM {table}") == []
    files = portal.container.settings.files.root / "attachments"
    assert list(files.iterdir()) == []


async def test_status_200_is_sent_only_after_the_question_is_committed(
    portal: Portal, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.model.scripts.append([asyncio.Event()])
    async with (
        _live_client(portal, client) as live,
        live.stream("POST", f"/api/dialogs/{dialog_id}/messages", json=CHAT_BODY) as response,
    ):
        assert response.status_code == 200
        # Заголовки пришли: вопрос и пустой ответ уже видны другому соединению с базой.
        rows = await portal.rows("SELECT role, status FROM messages ORDER BY position")
        assert [tuple(row) for row in rows] == [("user", "complete"), ("assistant", "streaming")]
    await _until(lambda: _status_is(portal, "stopped"))

    # Сбой фиксации — обычная ошибка до открытия потока, а не статус 200.
    from portal.dialogs.repositories import SqlDialogRepository

    async def fail(self: Any, *args: Any, **kwargs: Any) -> None:
        raise ConnectionRefusedError("portal-db")

    monkeypatch.setattr(SqlDialogRepository, "touch_dialog", fail)
    refused = await client.post(f"/api/dialogs/{dialog_id}/messages", json=CHAT_BODY)
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "service_unavailable"
    assert len(await portal.rows("SELECT 1 FROM messages")) == 2
    assert len(portal.model.requests) == 1


async def test_reading_messages_does_not_interrupt_a_live_answer(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    gate = asyncio.Event()
    portal.model.scripts.append([ContentDelta("Ответ"), gate, Finished("stop")])
    task = asyncio.create_task(ask(client, dialog_id))
    await _until(lambda: _len_is(portal.model.requests, 1))
    assert (await _messages(client, dialog_id))[1]["status"] == "streaming"
    gate.set()
    assert (await task)[-1] == ("done", {"status": "complete"})
    assert (await _messages(client, dialog_id))[1]["status"] == "complete"


async def test_stuck_answer_is_interrupted_by_sending_without_reading(
    portal: Portal, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    _break_saving(monkeypatch, failures=4)
    await ask(client, dialog_id)
    assert await _answer_status(portal) == "streaming"
    events = await asyncio.wait_for(ask(client, dialog_id, "Ещё раз"), timeout=5)
    assert events[-1] == ("done", {"status": "complete"})
