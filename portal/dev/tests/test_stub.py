"""Заглушка модели: каждое правило docs/portal-api.md §12.2."""

import base64
import hashlib
import json
import math
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from llm_stub.app import create_app

pytestmark = pytest.mark.anyio

CHAT = "/v1/chat/completions"
EMBEDDINGS = "/v1/embeddings"
RERANK = "/v1/rerank"
PNG = b"\x89PNG\r\n\x1a\n" + b"page-one"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(chunk_delay_ms=0))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://bifrost:8080", headers={"Authorization": "Bearer key"}
    ) as http:
        yield http


def _image(data: bytes = PNG) -> dict[str, Any]:
    url = "data:image/png;base64," + base64.b64encode(data).decode()
    return {"type": "image_url", "image_url": {"url": url}}


def _chat(text: str, *images: dict[str, Any], **extra: Any) -> dict[str, Any]:
    content: Any = [{"type": "text", "text": text}, *images] if images else text
    return {"model": "default", "messages": [{"role": "user", "content": content}], **extra}


async def _answer(client: httpx.AsyncClient, body: dict[str, Any]) -> str:
    response = await client.post(CHAT, json=body)
    assert response.status_code == 200, response.text
    content: str = response.json()["choices"][0]["message"]["content"]
    return content


async def _events(client: httpx.AsyncClient, body: dict[str, Any]) -> list[str]:
    response = await client.post(CHAT, json={**body, "stream": True})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]


async def test_health_needs_no_key_and_api_requires_bearer(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health", headers={"Authorization": ""})).status_code == 200
    for value in ("", "Bearer", "Bearer   ", "Basic abc"):
        for path, body in (
            (CHAT, _chat("привет")),
            (EMBEDDINGS, {"model": "embeddings", "input": "x"}),
        ):
            response = await client.post(path, json=body, headers={"Authorization": value})
            assert response.status_code == 401, value


async def test_default_reply_quotes_question_and_counts_images(client: httpx.AsyncClient) -> None:
    assert await _answer(client, _chat("Сколько лет аренды?")) == (
        "Заглушка. Вопрос: «Сколько лет аренды?». Изображений: 0."
    )
    long_question = "я" * 300
    assert f"«{'я' * 200}»" in await _answer(client, _chat(long_question))
    body = {
        "model": "default",
        "messages": [
            {"role": "system", "content": "правила"},
            {"role": "user", "content": [{"type": "text", "text": "раньше"}, _image()]},
            {"role": "assistant", "content": "ответ"},
            {"role": "user", "content": [{"type": "text", "text": "что на фото?"}, _image(b"2")]},
        ],
    }
    assert await _answer(client, body) == "Заглушка. Вопрос: «что на фото?». Изображений: 2."


async def test_reply_is_deterministic_openai_shape(client: httpx.AsyncClient) -> None:
    first = await client.post(CHAT, json=_chat("привет", reasoning_effort="low", max_tokens=10))
    second = await client.post(CHAT, json=_chat("привет", reasoning_effort="low", max_tokens=10))
    assert first.json() == second.json()
    choice = first.json()["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["reasoning"] == "Размышление заглушки."
    assert first.json()["object"] == "chat.completion"


async def test_footnote_is_added_when_question_carries_sources(client: httpx.AsyncClient) -> None:
    with_sources = "[1] Договор.pdf, стр. 2\nсроком на 49 лет\n\nКакой срок?"
    assert (await _answer(client, _chat(with_sources))).endswith(" Изображений: 0. [1]")
    assert not (await _answer(client, _chat("массив coords[1] тут"))).endswith("[1]")


async def test_unknown_field_model_effort_and_images_are_refused(client: httpx.AsyncClient) -> None:
    cases = [
        (_chat("x", chat_template_kwargs={"enable_thinking": False}), 400),
        (_chat("x", temperature=0.1), 400),
        ({**_chat("x"), "model": "qwen"}, 404),
        (_chat("x", reasoning_effort="minimal"), 400),
        (_chat("x", *[_image(bytes([n])) for n in range(9)]), 400),
        ({"model": "default", "messages": [{"role": "system", "content": "x"}]}, 400),
        ({"model": "default", "messages": "x"}, 400),
    ]
    for body, status in cases:
        response = await client.post(CHAT, json=body)
        assert response.status_code == status, body
        assert "error" in response.json()
    for effort in ("low", "medium"):
        assert (
            await client.post(CHAT, json=_chat("x", reasoning_effort=effort))
        ).status_code == 200
    eight = _chat("x", *[_image(bytes([n])) for n in range(8)])
    assert (await _answer(client, eight)).endswith("Изображений: 8.")
    assert (await client.post(CHAT, content=b"{oops")).status_code == 400


async def test_high_efforts_are_refused_like_template_behind_gateway(
    client: httpx.AsyncClient,
) -> None:
    """Шлюз переписывает `xhigh` в `high`, шаблон отвечает 400; без поля — размышления."""
    for effort in ("high", "xhigh"):
        response = await client.post(CHAT, json=_chat("x", reasoning_effort=effort))
        assert response.status_code == 400, effort
        assert response.json()["error"]["message"] == (
            "Unexpected reasoning effort high. "
            "Supported types are xhigh (default), medium, and low."
        )
    default = await client.post(CHAT, json=_chat("x"))
    assert default.status_code == 200
    assert default.json()["choices"][0]["message"]["reasoning"] == "Размышление заглушки."


async def test_reply_marks_win_over_json_schema(client: httpx.AsyncClient) -> None:
    schema = {"type": "json_schema", "json_schema": {"name": "t", "schema": {"type": "object"}}}
    text = 'Документ. [[stub:reply]]{"cadastral_number": ""}[[/stub:reply]] хвост'
    assert await _answer(client, _chat(text, response_format=schema)) == '{"cadastral_number": ""}'
    block = "[[stub:reply]]```sql\nDELETE FROM parcels;\n```[[/stub:reply]]"
    assert await _answer(client, _chat(block)) == "```sql\nDELETE FROM parcels;\n```"
    assert await _answer(client, _chat("[[stub:reply]][[/stub:reply]]")) == ""


async def test_page_recognition_depends_on_image_bytes(client: httpx.AsyncClient) -> None:
    prompt = "Распознай текст страницы."
    first = await _answer(client, _chat(prompt, _image(PNG)))
    assert first == f"Текст страницы-заглушки {hashlib.sha256(PNG).hexdigest()[:8]}."
    assert await _answer(client, _chat(prompt, _image(PNG))) == first
    assert await _answer(client, _chat(prompt, _image(b"other page"))) != first
    # Не протокол распознавания: другой текст или не одно изображение.
    assert (await _answer(client, _chat(prompt))).startswith("Заглушка. Вопрос")
    assert (await _answer(client, _chat(prompt + " ", _image()))).startswith("Заглушка. Вопрос")
    assert (await _answer(client, _chat(prompt, _image(), _image(b"2")))).startswith("Заглушка.")


async def test_json_schema_reply_follows_schema(client: httpx.AsyncClient) -> None:
    schema = {
        "type": "object",
        "properties": {
            "cadastral_number": {"type": "string", "description": "Номер"},
            "area": {"type": "number"},
            "count": {"type": "integer"},
            "is_land": {"type": "boolean"},
            "kind": {"enum": ["land", "building"]},
            "note": {"type": ["string", "null"]},
            "owner": {"type": "object", "properties": {"name": {"type": "string"}}},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "value": {"type": "string"}},
                },
            },
            "tags": {"type": "array", "minItems": 3, "items": {"type": "string"}},
        },
    }
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "t", "schema": schema, "strict": True},
    }
    answer = json.loads(await _answer(client, _chat("документ", response_format=response_format)))
    assert answer == {
        "cadastral_number": "заглушка: cadastral_number",
        "area": 0,
        "count": 0,
        "is_land": False,
        "kind": "land",
        "note": "заглушка: note",
        "owner": {"name": "заглушка: name"},
        "items": [{"title": "заглушка: title", "value": "заглушка: value"}],
        "tags": ["заглушка: tags"] * 3,
    }


async def test_refusal_marks(client: httpx.AsyncClient) -> None:
    assert (await client.post(CHAT, json=_chat("a [[stub:error]]"))).status_code == 503
    assert (await client.post(CHAT, json=_chat("a [[stub:overloaded]]"))).status_code == 429
    overflow = await client.post(CHAT, json=_chat("a [[stub:context-overflow]]", stream=True))
    assert overflow.status_code == 400
    assert overflow.json() == {
        "error": {
            "message": "This model's maximum context length is 65536 tokens.",
            "type": "BadRequestError",
            "code": 400,
        }
    }
    # Пометка действует только в последнем сообщении пользователя.
    history = {
        "model": "default",
        "messages": [
            {"role": "user", "content": "[[stub:error]]"},
            {"role": "assistant", "content": "…"},
            {"role": "user", "content": "дальше"},
        ],
    }
    assert (await client.post(CHAT, json=history)).status_code == 200
    # Отказ проверяется раньше выбора текста ответа.
    both = _chat("[[stub:reply]]ok[[/stub:reply]] [[stub:error]]")
    assert (await client.post(CHAT, json=both)).status_code == 503


async def test_stream_sends_reasoning_then_text_then_done(client: httpx.AsyncClient) -> None:
    events = await _events(client, _chat("Первый второй третий четвёртый пятый"))
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    assert deltas[0]["reasoning"] == "Размышление заглушки."
    assert "content" not in deltas[0]
    text_deltas = [delta["content"] for delta in deltas[1:-1]]
    assert len(text_deltas) > 1
    assert all(1 <= len(part.split()) <= 3 for part in text_deltas)
    assert "".join(text_deltas) == await _answer(
        client, _chat("Первый второй третий четвёртый пятый")
    )
    assert [chunk["choices"][0]["finish_reason"] for chunk in chunks[:-1]] == [None] * (
        len(chunks) - 1
    )
    assert chunks[-1]["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop"}


async def test_stream_keeps_whitespace_of_marked_reply(client: httpx.AsyncClient) -> None:
    reply = "  Заголовок\n\n```sql\nSELECT 1;\n```\n| a | b |\n"
    events = await _events(client, _chat(f"[[stub:reply]]{reply}[[/stub:reply]]"))
    parts = [json.loads(event)["choices"][0]["delta"].get("content", "") for event in events[:-1]]
    assert "".join(parts) == reply


async def test_length_and_break_marks(client: httpx.AsyncClient) -> None:
    plain = await client.post(CHAT, json=_chat("вопрос [[stub:length]]"))
    assert plain.json()["choices"][0]["finish_reason"] == "length"
    events = await _events(client, _chat("вопрос [[stub:length]]"))
    assert json.loads(events[-2])["choices"][0]["finish_reason"] == "length"

    broken = await _events(client, _chat("один два три четыре пять шесть [[stub:break]]"))
    assert "[DONE]" not in broken
    chunks = [json.loads(event)["choices"][0] for event in broken]
    assert len([chunk for chunk in chunks if "content" in chunk["delta"]]) == 1
    assert all(chunk["finish_reason"] is None for chunk in chunks)


async def test_usage_chunk_follows_finish_reason_when_asked(client: httpx.AsyncClient) -> None:
    body = {
        "model": "default",
        "messages": [
            {"role": "system", "content": "Правила."},
            {"role": "user", "content": [{"type": "text", "text": "Сколько?"}, _image()]},
        ],
        "stream_options": {"include_usage": True},
    }
    events = await _events(client, body)
    assert events[-1] == "[DONE]"
    *_, finish, usage = [json.loads(event) for event in events[:-1]]
    assert finish["choices"][0]["finish_reason"] == "stop"
    answer = "Заглушка. Вопрос: «Сколько?». Изображений: 1."
    prompt = math.ceil(len("Правила.Сколько?") / 3) + 1500
    completion = math.ceil(len(answer) / 3)
    assert usage == {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "default",
        "choices": [],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


async def test_usage_is_absent_unless_asked_and_on_marks(client: httpx.AsyncClient) -> None:
    def has_usage(events: list[str]) -> bool:
        return any('"usage"' in event for event in events)

    assert not has_usage(await _events(client, _chat("вопрос")))
    off = _chat("вопрос", stream_options={"include_usage": False})
    assert not has_usage(await _events(client, off))
    for mark in ("[[stub:no-usage]]", "один два три четыре [[stub:break]]"):
        events = await _events(client, _chat(mark, stream_options={"include_usage": True}))
        assert not has_usage(events), mark
    refused = [
        _chat("x", stream_options={"include_usage": True}),  # без stream
        {**_chat("x", stream_options={"include_usage": "yes"}), "stream": True},
        {**_chat("x", stream_options={"include_usage": True, "x": 1}), "stream": True},
        {**_chat("x", stream_options=[]), "stream": True},
    ]
    for body in refused:
        assert (await client.post(CHAT, json=body)).status_code == 400, body


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Паузы потока записываются, а не выдерживаются: тест не зависит от времени."""
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr("llm_stub.app.asyncio.sleep", record)
    return recorded


async def test_slow_mark_gives_hundred_parts_with_pause(
    client: httpx.AsyncClient, pauses: list[float]
) -> None:
    plain = await _answer(client, _chat("вопрос [[stub:slow]]"))
    events = await _events(client, _chat("вопрос [[stub:slow]]"))
    parts = [json.loads(event)["choices"][0]["delta"].get("content") for event in events[:-1]]
    text_parts = [part for part in parts if part is not None]
    assert len(text_parts) == 100
    assert "".join(text_parts) == plain
    assert pauses == [0.2] * 100


async def test_chunk_delay_is_configurable(pauses: list[float]) -> None:
    transport = httpx.ASGITransport(app=create_app(chunk_delay_ms=50))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://bifrost:8080", headers={"Authorization": "Bearer key"}
    ) as http:
        response = await http.post(CHAT, json=_chat("раз два три четыре пять шесть", stream=True))
    assert response.text.count('"content"') == len(pauses) >= 3
    assert set(pauses) == {0.05}


async def test_embeddings_are_normalized_and_reflect_shared_words(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        EMBEDDINGS,
        json={
            "model": "embeddings",
            "input": [
                "Договор аренды земельного участка",
                "договор АРЕНДЫ помещения",
                "кадастровый номер объекта",
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["index"] for item in body["data"]] == [0, 1, 2]
    vectors = [item["embedding"] for item in body["data"]]
    assert all(len(vector) == 1024 for vector in vectors)
    assert all(math.isclose(sum(v * v for v in vector), 1.0, rel_tol=1e-9) for vector in vectors)

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(vectors[0], vectors[1]) > 0.4
    assert cosine(vectors[0], vectors[2]) < cosine(vectors[0], vectors[1])

    single = await client.post(
        EMBEDDINGS, json={"model": "embeddings", "input": "Договор аренды земельного участка"}
    )
    assert single.json()["data"][0]["embedding"] == vectors[0]


async def test_embeddings_refusals(client: httpx.AsyncClient) -> None:
    assert (
        await client.post(EMBEDDINGS, json={"model": "default", "input": "x"})
    ).status_code == 404
    assert (
        await client.post(EMBEDDINGS, json={"model": "embeddings", "input": 5})
    ).status_code == 400
    assert (
        await client.post(EMBEDDINGS, json={"model": "embeddings", "input": [1]})
    ).status_code == 400
    limit = " ".join(["слово"] * 8192)
    assert (
        await client.post(EMBEDDINGS, json={"model": "embeddings", "input": limit})
    ).status_code == 200
    too_long = {"model": "embeddings", "input": ["коротко", limit + " ещё"]}
    assert (await client.post(EMBEDDINGS, json=too_long)).status_code == 400


async def test_query_rewriting_joins_last_question_with_the_follow_up(
    client: httpx.AsyncClient,
) -> None:
    text = (
        "Перепиши вопрос для поиска.\n<диалог>\n"
        "<пользователь>Первый вопрос</пользователь>\n<помощник>Ответ</помощник>\n"
        "<пользователь> Договор аренды участка </пользователь>\n<помощник>49 лет [1]</помощник>\n"
        "</диалог>\n<вопрос>\nА кто арендатор? [[stub:reply]]подмена[[/stub:reply]]\n</вопрос>"
    )
    # Пометка reply из вопроса теста ответ переписывания не подменяет.
    expected = "Договор аренды участка А кто арендатор? [[stub:reply]]подмена[[/stub:reply]]"
    assert await _answer(client, _chat(text, reasoning_effort="low")) == expected
    # Первое сообщение диалога: истории нет — остаётся сам вопрос.
    alone = "Перепиши вопрос для поиска.\n<диалог>\n</диалог>\n<вопрос>\nСрок аренды?\n</вопрос>"
    assert await _answer(client, _chat(alone)) == "Срок аренды?"


async def test_picture_description_and_fragment_context_depend_on_content(
    client: httpx.AsyncClient,
) -> None:
    prompt = "Опиши изображение из документа."
    digest = hashlib.sha256(PNG).hexdigest()[:8]
    described = await _answer(client, _chat(prompt, _image()))
    assert described == f"Описание изображения-заглушки {digest}."
    assert await _answer(client, _chat(prompt, _image(PNG + b"2"))) != described
    # Без изображения это обычный вопрос.
    assert (await _answer(client, _chat(prompt))).startswith("Заглушка. Вопрос:")

    # 701–703 — только у PNG с такой шириной; у прочих ширин и JPEG — обычный ответ.
    assert await _answer(client, _chat(prompt, _image(_png_of_width(700)))) == (
        f"Описание изображения-заглушки {hashlib.sha256(_png_of_width(700)).hexdigest()[:8]}."
    )

    context = (
        "Поясни место фрагмента в документе.\n<документ>Договор</документ>\n"
        "<соседи>\nсоседний текст\n</соседи>\n<фрагмент>\n текст фрагмента \n</фрагмент>"
    )
    digest = hashlib.sha256("текст фрагмента".encode()).hexdigest()[:8]
    assert await _answer(client, _chat(context)) == f"Контекстная справка {digest}."


def _png_of_width(width: int) -> bytes:
    """Начало PNG с заголовком `IHDR` нужной ширины — заглушке точки не нужны."""
    ihdr = b"IHDR" + width.to_bytes(4, "big") + (60).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return PNG[:8] + (13).to_bytes(4, "big") + ihdr + b"\x00\x00\x00\x00"


async def _described(
    client: httpx.AsyncClient, width: int, max_tokens: int | None = 2048
) -> tuple[str, str]:
    extra = {} if max_tokens is None else {"max_tokens": max_tokens}
    body = _chat("Опиши изображение из документа.", _image(_png_of_width(width)), **extra)
    response = await client.post(CHAT, json=body)
    assert response.status_code == 200, response.text
    choice = response.json()["choices"][0]
    return choice["message"]["content"], choice["finish_reason"]


async def test_picture_widths_check_description_rules(client: httpx.AsyncClient) -> None:
    def digest(width: int) -> str:
        return hashlib.sha256(_png_of_width(width)).hexdigest()[:8]

    # 701 — модель сочла картинку бессодержательной.
    assert await _described(client, 701) == ("", "stop")
    # 702 — длинное описание: строки по порядку, всего больше 3000 символов.
    text, finish = await _described(client, 702)
    lines = text.split("\n")
    assert finish == "stop"
    assert len(text) > 3000
    assert len("\n".join(lines[:-1])) <= 3000
    assert lines[0] == f"Описание изображения-заглушки {digest(702)}, строка 1."
    assert lines[-1] == f"Описание изображения-заглушки {digest(702)}, строка {len(lines)}."
    # 703 — упор в предел до 4096 токенов, полный ответ с 4096; без предела — полный.
    cut = (f"Начало описания-заглушки {digest(703)}.", "length")
    full = (f"Описание изображения-заглушки {digest(703)}.", "stop")
    assert await _described(client, 703, 2048) == cut
    assert await _described(client, 703, 4095) == cut
    assert await _described(client, 703, 4096) == full
    assert await _described(client, 703, None) == full
    # Та же ширина в обычном вопросе ничего не меняет.
    plain = await _answer(client, _chat("Вопрос", _image(_png_of_width(701))))
    assert plain.startswith("Заглушка. Вопрос:")


async def test_sql_test_data_and_fix_protocols(client: httpx.AsyncClient) -> None:
    schema = {"type": "json_schema", "json_schema": {"name": "data", "schema": {"type": "object"}}}
    plain = (
        "Подготовь тестовые данные.\n<схема>\nCREATE TABLE t (id int);\n</схема>\n"
        "<запрос>\nSELECT 1\n</запрос>"
    )
    assert await _answer(client, _chat(plain, response_format=schema)) == '{"statements": []}'
    data = '{"statements": ["CREATE TABLE t (id int)"]}'
    marked = plain.replace(
        "CREATE TABLE", f"-- [[stub:sql-data]]{data}[[/stub:sql-data]]\nCREATE TABLE"
    )
    assert await _answer(client, _chat(marked, response_format=schema)) == data

    fix = (
        "Исправь запрос.\n<схема>\n-- s\n</схема>\n<запрос>\nSELECT * FROM t\n</запрос>\n"
        "<ошибка>\nrelation\n</ошибка>"
    )
    assert await _answer(client, _chat(fix)) == "```sql\nSELECT * FROM t\n```"
    fixed = fix.replace("-- s", "-- [[stub:sql-fix]]```sql\nSELECT 1\n```[[/stub:sql-fix]]")
    assert await _answer(client, _chat(fixed)) == "```sql\nSELECT 1\n```"


async def test_rerank_scores_by_share_of_query_words(client: httpx.AsyncClient) -> None:
    documents = ["Договор аренды", "Срок аренды участка — 49 лет", "совсем другое", "АРЕНДЫ срок"]
    body = {"model": "rerank", "query": "срок аренды участка", "documents": documents}
    response = await client.post(RERANK, json=body)
    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["index"] for item in results] == [1, 3, 0, 2]
    assert [item["relevance_score"] for item in results] == pytest.approx([1, 2 / 3, 1 / 3, 0])
    empty = await client.post(RERANK, json={**body, "query": "?!"})
    assert [item["relevance_score"] for item in empty.json()["results"]] == [0, 0, 0, 0]
    assert [item["index"] for item in empty.json()["results"]] == [0, 1, 2, 3]


async def test_rerank_refusals(client: httpx.AsyncClient) -> None:
    body = {"model": "rerank", "query": "срок", "documents": ["срок"]}
    assert (await client.post(RERANK, json={**body, "model": "default"})).status_code == 404
    assert (await client.post(RERANK, json={**body, "documents": "срок"})).status_code == 400
    assert (await client.post(RERANK, json={**body, "query": ["срок"]})).status_code == 400
    assert (await client.post(RERANK, json=body, headers={"Authorization": ""})).status_code == 401
    failing = {**body, "query": "срок [[stub:rerank-error]]"}
    assert (await client.post(RERANK, json=failing)).status_code == 503
    # Общая пометка отказа на реранкер не действует, а своя — не действует на ответ модели.
    assert (await client.post(RERANK, json={**body, "query": "[[stub:error]]"})).status_code == 200
    assert (await client.post(CHAT, json=_chat("[[stub:rerank-error]]"))).status_code == 200
