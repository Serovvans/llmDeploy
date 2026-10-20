"""Клиент модели через Bifrost — против заглушки стенда `portal/dev` (§12.1, §12.2)."""

import io
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from llm_stub.app import create_app as create_stub
from PIL import Image

from portal.core.settings import LlmSettings, Settings
from portal.llm.bifrost import BifrostChatModel, shrink_image
from portal.llm.ports import (
    ChatRequest,
    Completion,
    ContentDelta,
    ContextOverflowError,
    Finished,
    ImagePart,
    ModelMessage,
    ModelOverloadedError,
    ModelUnavailableError,
    ReasoningDelta,
    ReasoningEffort,
    TextPart,
    Usage,
)
from tests import samples
from tests.conftest import close_portal, make_portal
from tests.support import ask, live_server, new_dialog, upload

pytestmark = pytest.mark.anyio


def _request(text: str, *images: ImagePart, schema: dict[str, Any] | None = None) -> ChatRequest:
    return ChatRequest(
        [
            ModelMessage("system", [TextPart("Правила.")]),
            ModelMessage("user", [TextPart(text), *images]),
        ],
        "low",
        100,
        schema,
    )


@pytest.fixture
async def model(settings: Settings) -> AsyncIterator[BifrostChatModel]:
    """Клиент к заглушке, поднятой настоящим сервером: поток читается как на стенде."""
    async with live_server(create_stub(chunk_delay_ms=0)) as url, httpx.AsyncClient() as client:
        llm = settings.llm.model_copy(update={"base_url": f"{url}/v1"})
        yield BifrostChatModel(client, llm, "sk-bf-test")


async def _collect(model: BifrostChatModel, request: ChatRequest) -> list[Any]:
    return [event async for event in model.stream(request)]


async def test_stream_yields_reasoning_text_and_finish(model: BifrostChatModel) -> None:
    events = await _collect(model, _request("Какой срок аренды?"))
    assert events[0] == ReasoningDelta("Размышление заглушки.")
    assert events[-1] == Finished("stop")
    text = "".join(event.text for event in events if isinstance(event, ContentDelta))
    assert text == "Заглушка. Вопрос: «Какой срок аренды?». Изображений: 0."
    assert len([event for event in events if isinstance(event, ContentDelta)]) > 1


async def test_only_standard_fields_and_images_are_sent(model: BifrostChatModel) -> None:
    """Заглушка отвечает 400 на любое нестандартное поле — успешный ответ это исключает."""
    image = ImagePart(samples.png(), "image/png")
    events = await _collect(
        model, _request("Что на фото?", image, ImagePart(samples.jpeg(), "image/jpeg"))
    )
    text = "".join(event.text for event in events if isinstance(event, ContentDelta))
    assert text.endswith("Изображений: 2.")

    schema = {"type": "object", "properties": {"number": {"type": "string"}}}
    assert (
        await model.complete(_request("Документ", schema=schema))
        == '{"number": "заглушка: number"}'
    )
    assert (await model.complete(_request("Название?"))).startswith("Заглушка. Вопрос: «Название?»")


async def test_length_finish_reason(model: BifrostChatModel) -> None:
    events = await _collect(model, _request("вопрос [[stub:length]]"))
    assert events[-1] == Finished("length")


async def test_stream_closed_without_finish_reason_is_a_break(model: BifrostChatModel) -> None:
    received = []
    with pytest.raises(ModelUnavailableError):
        async for event in model.stream(_request("один два три четыре пять шесть [[stub:break]]")):
            received.append(event)
    assert [type(event) for event in received] == [ReasoningDelta, ContentDelta]


@pytest.mark.parametrize(
    ("mark", "error"),
    [
        ("[[stub:error]]", ModelUnavailableError),
        ("[[stub:overloaded]]", ModelOverloadedError),
        ("[[stub:context-overflow]]", ContextOverflowError),
    ],
)
async def test_refusals_map_to_port_errors(
    model: BifrostChatModel, mark: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await _collect(model, _request(f"вопрос {mark}"))
    with pytest.raises(error):
        await model.complete(_request(f"вопрос {mark}"))


def _mock_model(settings: LlmSettings, handler: Any) -> BifrostChatModel:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BifrostChatModel(client, settings, "sk-bf-secret")


async def test_key_goes_in_authorization_header(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"choices": [{"message": {"content": "ок"}, "finish_reason": "stop"}]}
        return httpx.Response(200, json=body)

    model = _mock_model(settings.llm, handler)
    assert await model.complete(_request("вопрос")) == "ок"
    assert str(seen[0].url) == "http://bifrost:8080/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-bf-secret"
    payload = json.loads(seen[0].content)
    assert set(payload) == {"model", "messages", "stream", "reasoning_effort", "max_tokens"}
    assert (payload["model"], payload["stream"], payload["reasoning_effort"]) == (
        "default", False, "low",
    )  # fmt: skip
    assert payload["messages"][0] == {"role": "system", "content": "Правила."}


@pytest.mark.parametrize(
    ("effort", "sent"), [("low", "low"), ("medium", "medium"), ("xhigh", None)]
)
async def test_xhigh_effort_is_left_to_model_default(
    settings: Settings, effort: ReasoningEffort, sent: str | None
) -> None:
    """Bifrost переписывает `xhigh` в `high` (модель отвечает 400): поле не передаётся."""
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if payloads[-1]["stream"]:
            line = 'data: {"choices":[{"delta":{"content":"ок"},"finish_reason":"stop"}]}\n\n'
            return httpx.Response(200, text=line)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}]})

    model = _mock_model(settings.llm, handler)
    request = replace(_request("вопрос"), reasoning_effort=effort)
    await _collect(model, request)
    await model.complete(request)
    assert [payload["stream"] for payload in payloads] == [True, False]
    for payload in payloads:
        if sent is None:
            assert "reasoning_effort" not in payload
        else:
            assert payload["reasoning_effort"] == sent


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ({"message": {"content": "ок"}, "finish_reason": "stop"}, Completion("ок", "stop")),
        ({"message": {"content": "нача"}, "finish_reason": "length"}, Completion("нача", "length")),
        ({"message": {"content": None}, "finish_reason": "stop"}, Completion("", "stop")),
        ({"message": {}}, Completion("", "stop")),
        (
            {"message": {"content": "ок"}, "finish_reason": "content_filter"},
            Completion("ок", "stop"),
        ),
    ],
)
async def test_completion_carries_finish_reason(
    settings: Settings, choice: dict[str, Any], expected: Completion
) -> None:
    """§12.1: `length` — только при `finish_reason = "length"`; нет текста — пустая строка."""
    model = _mock_model(settings.llm, lambda _: httpx.Response(200, json={"choices": [choice]}))
    assert await model.complete_with_reason(_request("вопрос")) == expected
    assert await model.complete(_request("вопрос")) == expected.text


async def test_timeout_defaults_to_first_token_and_can_be_overridden(settings: Settings) -> None:
    """Срок воркера передаётся конструктором; без него — срок `portal-api` (§12.1)."""
    seen: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await BifrostChatModel(client, settings.llm, "k").complete(_request("вопрос"))
    await BifrostChatModel(client, settings.llm, "k", timeout_seconds=600).complete(
        _request("вопрос")
    )
    assert seen == [settings.llm.first_token_timeout_seconds, 600]


async def test_connection_failures_and_timeouts(settings: Settings) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no first token")

    with pytest.raises(ModelUnavailableError):
        await _collect(_mock_model(settings.llm, refuse), _request("вопрос"))
    # Модель не начала отвечать за llm.first_token_timeout_seconds — перегрузка.
    with pytest.raises(ModelOverloadedError):
        await _collect(_mock_model(settings.llm, stall), _request("вопрос"))
    with pytest.raises(ModelUnavailableError):
        await _mock_model(settings.llm, refuse).complete(_request("вопрос"))


async def test_both_reasoning_field_names_are_accepted(settings: Settings) -> None:
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"раз"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"reasoning":"два"},"finish_reason":null}]}',
        ": comment",
        "data: not json",
        'data: {"choices":[{"delta":{"content":"текст"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="\n\n".join(lines) + "\n\n")

    events = await _collect(_mock_model(settings.llm, handler), _request("вопрос"))
    assert events == [
        ReasoningDelta("раз"), ReasoningDelta("два"), ContentDelta("текст"), Finished("stop"),
    ]  # fmt: skip


def test_large_image_is_downscaled_before_sending() -> None:
    big = ImagePart(samples.png(3000, 1500), "image/png")
    small = shrink_image(big, 1568)
    with Image.open(io.BytesIO(small.data)) as image:
        assert image.size == (1568, 784) and image.format == "PNG"
    untouched = ImagePart(samples.jpeg(100, 50), "image/jpeg")
    assert shrink_image(untouched, 1568) is untouched
    with Image.open(
        io.BytesIO(shrink_image(ImagePart(samples.jpeg(2000, 2000), "image/jpeg"), 500).data)
    ) as image:
        assert image.size == (500, 500) and image.format == "JPEG"


async def test_chat_through_real_client_and_stub(settings: Settings) -> None:
    """Сквозной путь: маршрут → сценарий → клиент Bifrost → заглушка стенда."""
    async with live_server(create_stub(chunk_delay_ms=0)) as url:
        llm = settings.llm.model_copy(update={"base_url": f"{url}/v1"})
        portal = await make_portal(settings.model_copy(update={"llm": llm}), scripted_model=False)
        try:
            client = await portal.employee()
            dialog_id = await new_dialog(client)
            image = (await upload(client, dialog_id, "фото.png", samples.png())).json()["id"]
            events = await ask(client, dialog_id, "Что на фото?", attachment_ids=[image])
            names = [name for name, _ in events]
            assert names[0] == "start" and names[-1] == "done" and "reasoning_delta" in names
            answer = "".join(data["text"] for name, data in events if name == "delta")
            assert answer.startswith("Заглушка. Вопрос: «<вложение имя=") and answer.endswith(
                "Изображений: 1."
            )
            assert dict(events)["title"]["title"].startswith("Заглушка. Вопрос: «")
            assert len(dict(events)["title"]["title"]) <= 80

            cases = [
                ("[[stub:break]] один два три четыре", "error", "model_unavailable"),
                ("[[stub:error]]", "error", "model_unavailable"),
                ("[[stub:overloaded]]", "error", "model_overloaded"),
                ("[[stub:context-overflow]]", "error", "message_too_long"),
                ("[[stub:length]]", "done", None),
            ]
            for content, last, code in cases:
                other = await new_dialog(client)
                events = await ask(client, other, content)
                assert events[-1][0] == last, content
                if code:
                    assert events[-1][1]["code"] == code
                    # Название могло прийти раньше сбоя, но после `error` событий нет.
                    assert [name for name, _ in events].index("error") == len(events) - 1
                else:
                    assert events[-1][1] == {"status": "length_limit"}
            # Обрыв посреди потока: полученная часть ответа сохранена.
            partial = await portal.rows(
                "SELECT content FROM messages WHERE status = 'error' AND content <> ''"
            )
            assert len(partial) == 1 and partial[0].content.startswith("Заглушка.")
        finally:
            await close_portal(portal)


# --- фактический расход запроса (0.7, §5.10.10, §12.1) ---

_TEXT = 'data: {"choices":[{"delta":{"content":"текст"},"finish_reason":null}]}'
_FINISH = 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
_USAGE = 'data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":3}}'


class _Body(httpx.AsyncByteStream):
    """Тело потока по строкам; `failure` поднимается после последней строки — обрыв хвоста."""

    def __init__(self, lines: list[str], failure: Exception | None = None) -> None:
        self._lines = lines
        self._failure = failure

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for line in self._lines:
            yield f"{line}\n\n".encode()
        if self._failure is not None:
            raise self._failure


def _streaming(lines: list[str], failure: Exception | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Body(lines, failure))

    return handler


def _with_usage(include: bool = True) -> ChatRequest:
    return replace(_request("вопрос"), include_usage=include)


async def test_stream_options_are_sent_only_when_usage_is_asked(settings: Settings) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if sent[-1]["stream"]:
            return httpx.Response(200, text="\n\n".join([_TEXT, _FINISH, "data: [DONE]"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}]})

    model = _mock_model(settings.llm, handler)
    await _collect(model, _with_usage())
    await _collect(model, _with_usage(False))
    # Без потока поля нет даже при include_usage: оно стандартно только для потока.
    await model.complete(_with_usage())
    assert sent[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in sent[1] and "stream_options" not in sent[2]


async def test_usage_chunk_without_choices_gives_usage_before_finish(settings: Settings) -> None:
    separate = [_TEXT, _FINISH, _USAGE, "data: [DONE]"]
    events = await _collect(_mock_model(settings.llm, _streaming(separate)), _with_usage())
    assert events == [ContentDelta("текст"), Usage(42), Finished("stop")]
    # Шлюз может положить usage и в сам фрагмент с finish_reason; тело без [DONE] — тоже конец.
    inline = [_TEXT, _FINISH.replace("]}", '],"usage":{"prompt_tokens":7}}')]
    events = await _collect(_mock_model(settings.llm, _streaming(inline)), _with_usage())
    assert events == [ContentDelta("текст"), Usage(7), Finished("stop")]
    # Без include_usage хвост не читается и Usage нет, даже если шлюз его прислал.
    events = await _collect(_mock_model(settings.llm, _streaming(separate)), _with_usage(False))
    assert events == [ContentDelta("текст"), Finished("stop")]


@pytest.mark.parametrize(
    ("tail", "failure"),
    [
        ([], None),  # usage не пришёл
        (["data: not json", 'data: {"usage":{"prompt_tokens":"много"}}'], None),
        ([_USAGE], httpx.ReadTimeout("tail stalled")),
        ([_USAGE], httpx.RemoteProtocolError("tail broken")),
    ],
)
async def test_tail_after_finish_never_spoils_the_answer(
    settings: Settings, tail: list[str], failure: Exception | None
) -> None:
    handler = _streaming([_TEXT, _FINISH, *tail], failure)
    events = await _collect(_mock_model(settings.llm, handler), _with_usage())
    assert events == [ContentDelta("текст"), Finished("stop")]
