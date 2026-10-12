"""Эмбеддинги и распознавание страниц — против заглушки стенда `portal/dev` (§12.1, §12.2)."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from llm_stub import replies
from llm_stub.app import create_app as create_stub

from portal.core.settings import Settings
from portal.kb.embedder import BifrostEmbedder
from portal.kb.ports import (
    EmbeddingsUnavailableError,
    RecognitionFailedError,
    RerankUnavailableError,
)
from portal.kb.recognizer import ModelPageRecognizer
from portal.kb.reranker import BifrostReranker
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


# --- реранкер ---


def _reranker(
    settings: Settings, url: str, client: httpx.AsyncClient, **update: Any
) -> BifrostReranker:
    rerank = settings.kb.rerank.model_copy(update={"url": f"{url}/rerank", **update})
    return BifrostReranker(client, rerank, "sk-bf-test")


async def test_scores_come_in_document_order_through_real_client_and_stub(
    stub: tuple[str, httpx.AsyncClient], settings: Settings
) -> None:
    """Заглушка отдаёт записи по убыванию оценки — порт возвращает их по порядку документов."""
    reranker = _reranker(settings, *stub)
    documents = ["арендная плата", "срок аренды 49 лет", "срок сервитута"]
    assert await reranker.scores("срок аренды", documents) == [0.0, 1.0, 0.5]


@pytest.mark.parametrize(
    "failure", ["status", "model", "refused", "partial", "garbage", "huge", "slow"]
)
async def test_every_reranker_failure_is_one_port_error(
    stub: tuple[str, httpx.AsyncClient], settings: Settings, failure: str
) -> None:
    """Статус не 200, отказ соединения, неполный ответ и превышение срока — «недоступен»."""
    url, client = stub
    query = "срок аренды"

    async def answer(request: httpx.Request) -> httpx.Response:
        if failure == "slow":
            await asyncio.sleep(5)
        body: Any = {"results": [{"index": 0, "relevance_score": 0.9}]}
        if failure == "garbage":
            body = "не то"
        elif failure == "huge":
            # Целое, которое не помещается в число с плавающей точкой.
            body = {"results": [{"index": index, "relevance_score": 10**400} for index in (0, 1)]}
        return httpx.Response(200, json=body)

    if failure == "status":
        reranker = _reranker(settings, url, client)
        query += " [[stub:rerank-error]]"
    elif failure == "model":
        reranker = _reranker(settings, url, client, model="unknown")
    elif failure == "refused":
        reranker = _reranker(settings, "http://127.0.0.1:1/v1", client)
    else:
        mock = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        reranker = _reranker(settings, url, mock, timeout_seconds=0.05)
    with pytest.raises(RerankUnavailableError):
        await reranker.scores(query, ["первый документ", "второй документ"])


async def test_rerank_request_follows_protocol_and_cuts_long_texts(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    async def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    reranker = _reranker(settings, "http://bifrost:8080/v1", client, document_max_chars=10)
    assert await reranker.scores("з" * 50, ["д" * 50]) == [1.0]
    (request,) = seen
    assert str(request.url) == "http://bifrost:8080/v1/rerank"
    assert request.headers["Authorization"] == "Bearer sk-bf-test"
    assert json.loads(request.content) == {
        "model": "rerank",
        "query": "з" * 10,
        "documents": ["д" * 10],
    }
