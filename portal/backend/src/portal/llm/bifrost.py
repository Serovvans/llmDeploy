"""Клиент модели через Bifrost: только стандартные поля OpenAI API (docs/portal-api.md §12.1)."""

import asyncio
import base64
import io
import json
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
from PIL import Image

from portal.core.settings import LlmSettings
from portal.llm.ports import (
    ChatRequest,
    ContentDelta,
    ContextOverflowError,
    Finished,
    ImagePart,
    ModelMessage,
    ModelOverloadedError,
    ModelUnavailableError,
    ReasoningDelta,
)

_CONNECT_TIMEOUT_SECONDS = 10.0
_DATA_PREFIX = "data:"
_DONE = "[DONE]"
_RASTER_LOCK = threading.Lock()


def shrink_image(part: ImagePart, max_side: int) -> ImagePart:
    """Уменьшить изображение до `max_side` по длинной стороне; меньшее вернуть как есть.

    Блокировка одна на процесс и держится, пока растр существует: сколько бы ответов ни
    формировалось одновременно, раскрыт только один. JPEG читается сразу уменьшенным.
    """
    with _RASTER_LOCK:
        image = Image.open(io.BytesIO(part.data))
        try:
            if max(image.size) <= max_side:
                return part
            image.draft("RGB", (max_side, max_side))
            image.thumbnail((max_side, max_side))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG" if part.media_type == "image/png" else "JPEG")
        finally:
            image.close()  # растр освобождается до снятия блокировки
    return ImagePart(buffer.getvalue(), part.media_type)


def _encode_message(message: ModelMessage, max_side: int) -> dict[str, Any]:
    if all(not isinstance(part, ImagePart) for part in message.parts):
        text = "".join(part.text for part in message.parts if not isinstance(part, ImagePart))
        return {"role": message.role, "content": text}
    content: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, ImagePart):
            image = shrink_image(part, max_side)
            encoded = base64.b64encode(image.data).decode()
            url = f"data:{image.media_type};base64,{encoded}"
            content.append({"type": "image_url", "image_url": {"url": url}})
        else:
            content.append({"type": "text", "text": part.text})
    return {"role": message.role, "content": content}


class BifrostChatModel:
    """Реализация порта `ChatModel` поверх `POST /v1/chat/completions`."""

    def __init__(self, client: httpx.AsyncClient, settings: LlmSettings, api_key: str) -> None:
        """Получить HTTP-клиент, параметры и ключ портала."""
        self._client = client
        self._settings = settings
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = httpx.Timeout(
            settings.first_token_timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS
        )

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[ReasoningDelta | ContentDelta | Finished]:
        """Потоковый ответ; поток без фрагмента с `finish_reason` — обрыв."""
        payload = await self._payload(request, stream=True)
        received = False
        try:
            async with self._client.stream(
                "POST", self._url, json=payload, headers=self._headers, timeout=self._timeout
            ) as response:
                if response.status_code != httpx.codes.OK:
                    await response.aread()
                    raise self._refusal(response)
                async for line in response.aiter_lines():
                    if not line.startswith(_DATA_PREFIX):
                        continue
                    data = line[len(_DATA_PREFIX) :].strip()
                    if data == _DONE:
                        break
                    received = True
                    for event in _events(data):
                        yield event
                        if isinstance(event, Finished):
                            return
        except httpx.TimeoutException as error:
            # Модель не начала отвечать вовремя — перегрузка; замолчала посреди ответа — сбой.
            if received:
                raise ModelUnavailableError from error
            raise ModelOverloadedError from error
        except httpx.HTTPError as error:
            raise ModelUnavailableError from error
        raise ModelUnavailableError

    async def complete(self, request: ChatRequest) -> str:
        """Ответ целиком: `choices[0].message.content`."""
        payload = await self._payload(request, stream=False)
        try:
            response = await self._client.post(
                self._url, json=payload, headers=self._headers, timeout=self._timeout
            )
        except httpx.TimeoutException as error:
            raise ModelOverloadedError from error
        except httpx.HTTPError as error:
            raise ModelUnavailableError from error
        if response.status_code != httpx.codes.OK:
            raise self._refusal(response)
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, LookupError, TypeError) as error:
            raise ModelUnavailableError from error
        return content if isinstance(content, str) else ""

    @property
    def _url(self) -> str:
        return f"{self._settings.base_url.rstrip('/')}/chat/completions"

    async def _payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        max_side = self._settings.image_max_side_px
        # Перекодирование изображений — работа процессора: в пуле потоков.
        messages = await asyncio.to_thread(
            lambda: [_encode_message(message, max_side) for message in request.messages]
        )
        payload: dict[str, Any] = {
            "model": self._settings.chat_model,
            "messages": messages,
            "stream": stream,
            "reasoning_effort": request.reasoning_effort,
            "max_tokens": request.max_tokens,
        }
        if request.json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": request.json_schema, "strict": True},
            }
        return payload

    def _refusal(self, response: httpx.Response) -> Exception:
        """Перевести отказ в исключение порта; текст ответа никуда не передаётся."""
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return ModelOverloadedError()
        overflow = self._settings.context_overflow_marker in response.text
        if response.status_code == httpx.codes.BAD_REQUEST and overflow:
            return ContextOverflowError()
        return ModelUnavailableError()


def _events(data: str) -> list[ReasoningDelta | ContentDelta | Finished]:
    """События одного фрагмента потока; размышления принимаются под обоими именами."""
    try:
        choice = json.loads(data)["choices"][0]
    except (ValueError, LookupError, TypeError):
        return []
    delta = choice.get("delta") or {}
    events: list[ReasoningDelta | ContentDelta | Finished] = []
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if reasoning:
        events.append(ReasoningDelta(str(reasoning)))
    if delta.get("content"):
        events.append(ContentDelta(str(delta["content"])))
    finish = choice.get("finish_reason")
    if finish:
        events.append(Finished("length" if finish == "length" else "stop"))
    return events
