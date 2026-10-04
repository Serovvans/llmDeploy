"""HTTP-слой заглушки: проверки запроса, поток и ответы в формате OpenAI.

Запуск: `uvicorn llm_stub.app:app --host 0.0.0.0 --port 8080`.
"""

import asyncio
import binascii
import json
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from llm_stub import replies

_CHAT_FIELDS = frozenset(
    {"model", "messages", "stream", "reasoning_effort", "max_tokens", "response_format"}
)
_REASONING_EFFORTS = frozenset({"low", "medium", "xhigh"})
_SLOW_DELAY_SECONDS = 0.2
_CONTEXT_OVERFLOW = {
    "error": {
        "message": "This model's maximum context length is 65536 tokens.",
        "type": "BadRequestError",
        "code": 400,
    }
}
# Пометки-отказы: проверяются раньше выбора текста ответа.
_REFUSALS: Mapping[str, tuple[int, dict[str, Any]]] = {
    "[[stub:error]]": (503, {"error": {"message": "stub error", "type": "stub", "code": 503}}),
    "[[stub:overloaded]]": (
        429,
        {"error": {"message": "stub overloaded", "type": "stub", "code": 429}},
    ),
    "[[stub:context-overflow]]": (400, _CONTEXT_OVERFLOW),
}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "stub", "code": status}}, status_code=status
    )


class BearerAuth:
    """Запрос к `/v1/*` без `Authorization: Bearer <непустое>` получает 401."""

    def __init__(self, app: ASGIApp) -> None:
        """Обернуть приложение."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Проверить заголовок и передать запрос дальше."""
        if scope["type"] == "http" and scope["path"].startswith("/v1/"):
            scheme, _, token = Request(scope).headers.get("authorization", "").partition(" ")
            if scheme != "Bearer" or not token.strip():
                await _error(401, "missing api key")(scope, receive, send)
                return
        await self._app(scope, receive, send)


async def _body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "default",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _stream(
    parts: list[str], finish_reason: str, delay: float, *, broken: bool
) -> AsyncIterator[str]:
    yield _chunk({"role": "assistant", "reasoning": replies.REASONING})
    for part in parts:
        await asyncio.sleep(delay)
        yield _chunk({"content": part})
        if broken:
            return
    yield _chunk({}, finish_reason)
    yield "data: [DONE]\n\n"


def _chat_refusal(body: dict[str, Any]) -> Response | None:
    """Отказы до выбора ответа: поля, модель, уровень размышлений, изображения."""
    if unknown := sorted(set(body) - _CHAT_FIELDS):
        return _error(400, f"unsupported fields: {', '.join(unknown)}")
    if body.get("model") != "default":
        return _error(404, "model not found")
    effort = body.get("reasoning_effort")
    if effort == "high":
        return _error(500, "reasoning_effort high is not supported")
    if effort is not None and effort not in _REASONING_EFFORTS:
        return _error(400, "invalid reasoning_effort")
    messages = body.get("messages")
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        return _error(400, "messages must be a list of objects")
    return None


def create_app(chunk_delay_ms: int) -> Starlette:
    """Собрать приложение; `chunk_delay_ms` — пауза между частями потока."""

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def chat_completions(request: Request) -> Response:
        body = await _body(request)
        if body is None:
            return _error(400, "invalid json")
        if (refusal := _chat_refusal(body)) is not None:
            return refusal
        messages: list[dict[str, Any]] = body["messages"]
        try:
            images = sum(len(replies.message_images(message)) for message in messages)
            turn = replies.last_user_turn(messages)
        except (binascii.Error, AttributeError):
            return _error(400, "invalid message content")
        if images > replies.MAX_IMAGES:
            return _error(400, "too many images")
        if turn is None:
            return _error(400, "no user message")
        for mark, (status, payload) in _REFUSALS.items():
            if mark in turn.text:
                return JSONResponse(payload, status_code=status)

        slow = "[[stub:slow]]" in turn.text
        text = replies.reply_text(turn, body.get("response_format"), images)
        parts = replies.text_parts(text, slow=slow)
        finish_reason = "length" if "[[stub:length]]" in turn.text else "stop"
        if body.get("stream"):
            delay = _SLOW_DELAY_SECONDS if slow else chunk_delay_ms / 1000
            broken = "[[stub:break]]" in turn.text
            return StreamingResponse(
                _stream(parts, finish_reason, delay, broken=broken),
                media_type="text/event-stream",
            )
        return JSONResponse(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": 0,
                "model": "default",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "".join(parts),
                            "reasoning": replies.REASONING,
                        },
                        "finish_reason": finish_reason,
                    }
                ],
            }
        )

    async def embeddings(request: Request) -> Response:
        body = await _body(request)
        if body is None:
            return _error(400, "invalid json")
        if body.get("model") != "embeddings":
            return _error(404, "model not found")
        texts = body.get("input")
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
            return _error(400, "input must be a string or a list of strings")
        vectors = [replies.embedding(text) for text in texts]
        if any(vector is None for vector in vectors):
            return _error(400, "input is longer than 8192 words")
        return JSONResponse(
            {
                "object": "list",
                "model": "embeddings",
                "data": [
                    {"object": "embedding", "index": index, "embedding": vector}
                    for index, vector in enumerate(vectors)
                ],
            }
        )

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/v1/embeddings", embeddings, methods=["POST"]),
        ],
        middleware=[Middleware(BearerAuth)],
    )


app = create_app(int(os.environ.get("STUB_CHUNK_DELAY_MS", "30")))
