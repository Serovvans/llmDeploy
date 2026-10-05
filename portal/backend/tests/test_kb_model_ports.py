"""Эмбеддинги и распознавание страниц — против заглушки стенда `portal/dev` (§12.1, §12.2)."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from llm_stub import replies
from llm_stub.app import create_app as create_stub

from portal.core.settings import Settings
from portal.kb.embedder import BifrostEmbedder
from portal.kb.ports import EmbeddingsUnavailableError, RecognitionFailedError
from portal.kb.recognizer import ModelPageRecognizer
from portal.llm.bifrost import BifrostChatModel
from portal.llm.ports import (
    ChatRequest,
    ContextOverflowError,
    ImagePart,
    ModelOverloadedError,
    ModelUnavailableError,
    TextPart,
)
from tests import samples
from tests.support import live_server

pytestmark = pytest.mark.anyio


@pytest.fixture
async def stub() -> AsyncIterator[tuple[str, httpx.AsyncClient]]:
    async with live_server(create_stub(chunk_delay_ms=0)) as url, httpx.AsyncClient() as client:
        yield f"{url}/v1", client


def _embedder(
    stub: tuple[str, httpx.AsyncClient],
    settings: Settings,
    model: str = "embeddings",
    **update: Any,
) -> BifrostEmbedder:
    url, client = stub
    embeddings = settings.kb.embeddings.model_copy(update=update)
    return BifrostEmbedder(client, url, model, "sk-bf-test", embeddings)


# --- эмбеддинги ---


async def test_documents_are_embedded_in_order_by_batches(
    stub: tuple[str, httpx.AsyncClient], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = stub[1]
    sent: list[dict[str, Any]] = []
    original = client.post

    async def spy(*args: Any, **kwargs: Any) -> httpx.Response:
        sent.append(kwargs["json"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(client, "post", spy)
    texts = [f"фрагмент номер {index}" for index in range(70)]
    vectors = await _embedder(stub, settings).embed_documents(texts)
    assert [len(request["input"]) for request in sent] == [32, 32, 6]
    assert all(set(request) == {"model", "input"} for request in sent)
    assert {request["model"] for request in sent} == {"embeddings"}
    assert vectors == [replies.embedding(text) for text in texts]
    assert all(len(vector) == 1024 for vector in vectors)


async def test_query_gets_instruction_prefix_and_documents_do_not(
    stub: tuple[str, httpx.AsyncClient], settings: Settings
) -> None:
    instruction = settings.kb.embeddings.query_instruction
    assert instruction.startswith("Instruct: ") and instruction.endswith("\nQuery: ")
    embedder = _embedder(stub, settings)
    assert await embedder.embed_query("срок аренды") == replies.embedding(
        instruction + "срок аренды"
    )
    assert await embedder.embed_documents(["срок аренды"]) == [replies.embedding("срок аренды")]
    plain = _embedder(stub, settings, query_instruction="")  # bge-m3
    assert await plain.embed_query("срок аренды") == replies.embedding("срок аренды")


@pytest.mark.parametrize("case", ["model", "dimension", "too_long", "connection"])
async def test_embedding_failures_become_port_error(
    stub: tuple[str, httpx.AsyncClient], settings: Settings, case: str
) -> None:
    embedder = _embedder(stub, settings)
    text = "текст"
    if case == "model":
        embedder = _embedder(stub, settings, model="unknown")
    elif case == "dimension":
        embedder = _embedder(stub, settings, dimension=768)
    elif case == "too_long":
        text = "слово " * 9000  # заглушка имитирует лимит vllm-embed
    else:
        embedder = BifrostEmbedder(
            stub[1], "http://127.0.0.1:9/v1", "embeddings", "key", settings.kb.embeddings
        )
    with pytest.raises(EmbeddingsUnavailableError):
        await embedder.embed_documents([text])


# --- распознавание страниц ---


async def test_page_is_recognized_through_real_client_and_stub(
    stub: tuple[str, httpx.AsyncClient], settings: Settings
) -> None:
    url, client = stub
    llm = settings.llm.model_copy(update={"base_url": url})
    recognizer = ModelPageRecognizer(BifrostChatModel(client, llm, "sk-bf-test"), llm)
    first, second = samples.png(60, 40), samples.png(40, 60, "black")
    texts = await recognizer.recognize([first, second])
    assert texts == [
        f"Текст страницы-заглушки {hashlib.sha256(image).hexdigest()[:8]}."
        for image in (first, second)
    ]
    assert await recognizer.recognize([]) == []


class _VisionModel:
    """Подмена `ChatModel`: запоминает запросы, отвечает и сбоит по сценарию."""

    def __init__(self, *outcomes: str | Exception) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[ChatRequest] = []
        self.running = self.peak = 0

    async def complete(self, request: ChatRequest) -> str:
        self.requests.append(request)
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(0.01)
        self.running -= 1
        outcome = self.outcomes.pop(0) if self.outcomes else "  текст страницы \n"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stream(self, request: ChatRequest) -> Any:
        raise NotImplementedError


def _recognizer(model: _VisionModel, settings: Settings, **update: Any) -> ModelPageRecognizer:
    llm = settings.llm.model_copy(update={"recognition_retry_pause_seconds": 0, **update})
    return ModelPageRecognizer(model, llm)


async def test_recognition_request_follows_protocol(settings: Settings) -> None:
    model = _VisionModel()
    image = samples.png()
    assert await _recognizer(model, settings).recognize([image]) == ["текст страницы"]
    request = model.requests[0]
    assert (request.reasoning_effort, request.max_tokens, request.json_schema) == (
        "low",
        settings.llm.recognition_max_tokens,
        None,
    )
    system, user = request.messages
    assert system.role == "system"
    assert system.parts == (TextPart(settings.llm.recognition_system_prompt),)
    assert user.parts == (TextPart("Распознай текст страницы."), ImagePart(image, "image/png"))


async def test_one_request_per_page_with_limited_parallelism(settings: Settings) -> None:
    model = _VisionModel()
    images = [samples.png(40 + index, 30) for index in range(8)]
    texts = await _recognizer(model, settings).recognize(images)
    assert len(texts) == len(model.requests) == 8
    assert all(len(request.messages[1].parts) == 2 for request in model.requests)
    assert model.peak == settings.llm.recognition_parallel_requests == 2
    with pytest.raises(ValueError, match="не больше 8"):
        await _recognizer(model, settings).recognize([*images, samples.png()])


async def test_failed_page_is_retried_then_fails_whole_call(settings: Settings) -> None:
    flaky = _VisionModel(ModelUnavailableError(), ModelOverloadedError(), "со второго повтора")
    assert await _recognizer(flaky, settings).recognize([samples.png()]) == ["со второго повтора"]
    assert len(flaky.requests) == 3

    dead = _VisionModel(*[ModelUnavailableError() for _ in range(10)])
    with pytest.raises(RecognitionFailedError):
        await _recognizer(dead, settings).recognize([samples.png()])
    assert len(dead.requests) == settings.llm.recognition_attempts

    overflow = _VisionModel(ContextOverflowError())
    with pytest.raises(RecognitionFailedError):
        await _recognizer(overflow, settings).recognize([samples.png()])
    assert len(overflow.requests) == 1  # повтор того же запроса не поможет


async def test_empty_answer_means_page_without_text(settings: Settings) -> None:
    assert await _recognizer(_VisionModel("  \n "), settings).recognize([samples.png()]) == [""]
