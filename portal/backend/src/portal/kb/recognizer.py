"""Распознавание страниц-сканов основной моделью (docs/portal-api.md §12.1).

Один запрос — одна страница: соответствие «страница → текст» не зависит от того, как
модель разделила бы несколько страниц. Работает через порт `ChatModel`.
"""

import asyncio
from collections.abc import Sequence
from functools import partial

from portal.core.settings import LlmSettings
from portal.kb.ports import RecognitionFailedError
from portal.kb.retrying import retrying
from portal.llm.ports import (
    MAX_IMAGES_PER_REQUEST,
    ChatModel,
    ChatRequest,
    ContextOverflowError,
    ImagePart,
    ModelMessage,
    ModelOverloadedError,
    ModelUnavailableError,
    TextPart,
)

# Текст запроса зафиксирован контрактом: по нему заглушка стенда узнаёт распознавание.
_INSTRUCTION = "Распознай текст страницы."


class ModelPageRecognizer:
    """Реализация порта `PageRecognizer`."""

    def __init__(self, model: ChatModel, settings: LlmSettings) -> None:
        """Получить модель и параметры распознавания."""
        self._model = model
        self._settings = settings
        # Один на процесс: предел одновременных запросов общий для всех вызовов.
        self._slots = asyncio.Semaphore(settings.recognition_parallel_requests)

    async def recognize(self, images: Sequence[bytes]) -> list[str]:
        """Тексты страниц в порядке изображений; сбой любой страницы — сбой вызова."""
        if len(images) > MAX_IMAGES_PER_REQUEST:
            raise ValueError("за один вызов распознаётся не больше 8 страниц")
        tasks = [asyncio.create_task(self._page(image)) for image in images]
        try:
            return list(await asyncio.gather(*tasks))
        except (ModelUnavailableError, ModelOverloadedError, ContextOverflowError) as error:
            raise RecognitionFailedError from error
        finally:
            # Сбой одной страницы прекращает запросы остальных: их итог уже не нужен.
            for task in tasks:
                task.cancel()

    async def _page(self, image: bytes) -> str:
        request = ChatRequest(
            messages=(
                ModelMessage("system", (TextPart(self._settings.recognition_system_prompt),)),
                ModelMessage("user", (TextPart(_INSTRUCTION), ImagePart(image, "image/png"))),
            ),
            reasoning_effort="low",
            max_tokens=self._settings.recognition_max_tokens,
        )
        async with self._slots:
            text = await retrying(
                partial(self._model.complete, request),
                attempts=self._settings.recognition_attempts,
                pause_seconds=self._settings.recognition_retry_pause_seconds,
                retry_on=(ModelUnavailableError, ModelOverloadedError),
            )
        return text.strip()
