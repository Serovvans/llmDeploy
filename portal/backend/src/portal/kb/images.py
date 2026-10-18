"""Картинки документа: извлечение, хранение и описание моделью (docs/portal-api.md §8.6, §12.1).

Сценарий работает через порты: читатель документов, хранилище, модель. Описание — текст
недоверенный, как текст документа; в журнал оно не попадает.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Coroutine, Sequence
from dataclasses import dataclass, replace
from functools import partial
from itertools import batched
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from portal.core.settings import KbImagesSettings, LlmSettings
from portal.files.ports import (
    DocumentImage,
    DocumentReader,
    FileStorage,
    MediaType,
    UnreadableDocumentError,
)
from portal.kb.domain import DESCRIPTION_VERSION, Job, KbDocument, KbImage
from portal.kb.outcomes import JobAbandonedError, PermanentIndexError
from portal.kb.ports import RecognitionFailedError
from portal.kb.retrying import retrying
from portal.kb.slicing import JobSlice
from portal.kb.store import KbUnitOfWorkFactory
from portal.llm.ports import (
    ChatRequest,
    Completion,
    CompletionModel,
    ContextOverflowError,
    ImagePart,
    ModelMessage,
    ModelOverloadedError,
    ModelUnavailableError,
    TextPart,
)

logger = logging.getLogger(__name__)

# Текст запроса зафиксирован контрактом (§12.1): по нему заглушка стенда узнаёт описание.
_INSTRUCTION = "Опиши изображение из документа."
_PDF: MediaType = "application/pdf"


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _all_or_first_failure[T](calls: Sequence[Coroutine[Any, Any, T]]) -> list[T]:
    """Выполнить вызовы одновременно; первый сбой отменяет остальные и пробрасывается."""
    tasks = [asyncio.create_task(call) for call in calls]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in tasks:
        if not task.cancelled() and (error := task.exception()) is not None:
            raise error
    return [task.result() for task in tasks]


@dataclass(frozen=True)
class CollectedImages:
    """Итог шага «Картинки».

    `described` — картинки с непустым действующим описанием по порядку: по каждой
    строятся фрагменты-картинки; `truncated` — картинок в файле больше лимита, `None` —
    файл на картинки не разбирался, признак документа остаётся прежним. Признак
    записывает завершение задания вместе с заменой индекса (§10.6): при сбое
    переобработки документ остаётся в прежнем виде (§11.6).
    """

    described: Sequence[KbImage] = ()
    truncated: bool | None = None


class ImageCollector:
    """Шаг «Картинки» задания индексации (§11.2)."""

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        storage: FileStorage,
        reader: DocumentReader,
        model: CompletionModel,
        llm_slots: asyncio.Semaphore,
        settings: KbImagesSettings,
        llm: LlmSettings,
        portion: int,
    ) -> None:
        """Получить зависимости явно.

        `llm_slots` — общий на воркер предел фоновых обращений к основной модели
        (`kb.worker.background_llm_parallel`); `portion` — сколько картинок описывается
        одновременно, между порциями — сверка задания и точка уступки (§8.6).
        """
        self._uow_factory = uow_factory
        self._storage = storage
        self._reader = reader
        self._model = model
        self._llm_slots = llm_slots
        self._settings = settings
        self._llm = llm
        self._portion = portion

    async def collect(
        self,
        job: Job,
        document: KbDocument,
        path: Path,
        media_type: MediaType,
        turn: JobSlice | None = None,
    ) -> CollectedImages:
        """Картинки документа с действующим описанием по порядку извлечения.

        Картинка с тем же содержимым (по SHA-256) сохраняет строку и идентификатор;
        описание текущей версии повторно не запрашивается. При выключенном
        `kb.images.enabled` файл на картинки не разбирается, уже сохранённые остаются.
        `turn` — квант взятия: после порции описаний задание может уступить место.
        """
        async with self._uow_factory() as uow:
            stored = await uow.documents.images(document.id)
        if not self._settings.enabled:
            return CollectedImages([image for image in stored if image.effective_description])
        current, truncated = await self._extract(document, path, media_type, stored)
        described = await self._describe(job, current, turn)
        return CollectedImages(
            [image for image in described if image.effective_description], truncated
        )

    async def _extract(
        self, document: KbDocument, path: Path, media_type: MediaType, stored: Sequence[KbImage]
    ) -> tuple[list[KbImage], bool]:
        """Картинки файла и признак «их больше лимита»: одинаковые слиты в первую.

        Уже сохранённые читатель находит, но не декодирует (`known`, §8.6).
        """
        known = {image.sha256: image for image in stored}
        current: dict[bytes, KbImage] = {}
        truncated = False
        for page in range(1, (document.page_count or 1) + 1):
            saved = frozenset(known.keys() | current.keys())
            for found in await self._read(path, media_type, page, saved):
                if found.sha256 in current:
                    continue
                if len(current) == self._settings.max_per_document:
                    truncated = True
                    break
                current[found.sha256] = known.get(found.sha256) or await self._store(
                    document.id, page if media_type == _PDF else None, len(current), found
                )
            if truncated:
                break
        return list(current.values()), truncated

    async def _read(
        self, path: Path, media_type: MediaType, page: int, known: frozenset[bytes]
    ) -> list[DocumentImage]:
        settings = self._settings
        try:
            return await asyncio.to_thread(
                self._reader.embedded_images,
                path,
                media_type,
                page,
                min_side_px=settings.min_side_px,
                min_area_px=settings.min_area_px,
                known=known,
            )
        except UnreadableDocumentError:
            raise PermanentIndexError("file_unreadable") from None

    async def _store(
        self, document_id: UUID, page_number: int | None, ordinal: int, found: DocumentImage
    ) -> KbImage:
        """Сохранить файл картинки и её строку; документа уже нет — файл удаляется."""
        saved = await self._storage.save("kb-images", _once(found.data), len(found.data))
        image = KbImage(
            id=uuid4(),
            document_id=document_id,
            page_number=page_number,
            ordinal=ordinal,
            sha256=found.sha256,
            storage_key=saved.key,
            media_type=found.media_type,
            width=found.width,
            height=found.height,
        )
        try:
            async with self._uow_factory() as uow:
                if await uow.documents.lock(document_id) is None:
                    raise JobAbandonedError
                await uow.documents.add_image(image)
        except BaseException:
            await self._storage.delete(saved.key)
            raise
        return image

    async def _describe(
        self, job: Job, images: Sequence[KbImage], turn: JobSlice | None
    ) -> list[KbImage]:
        """Описать порциями картинки без описания текущей версии; каждое — сразу в базу.

        Перед порцией сверяется, что задание не снято: описание занимает GPU основной
        модели (§11.2). После порции — точка уступки места (§11.1).
        """
        result = list(images)
        waiting = [index for index, image in enumerate(images) if image.needs_description]
        for portion in batched(waiting, self._portion):
            async with self._uow_factory() as uow:
                if not await uow.jobs.exists(job.id):
                    raise JobAbandonedError
            if turn is not None:
                turn.start()
            described = await _all_or_first_failure([self._described(images[i]) for i in portion])
            for index, image in zip(portion, described, strict=True):
                result[index] = image
            if turn is not None:
                await turn.check()
        return result

    async def _described(self, image: KbImage) -> KbImage:
        """Картинка с новым описанием, сохранённым сразу по получении (§8.6).

        Впервые — в `description`, взамен прежней версии — в `pending_description`.
        Пустое описание тоже сохраняется: продолжение задания модель о картинке не
        спрашивает, а замена индекса её удалит.
        """
        description = await self._ask(image)
        pending = image.description is not None
        async with self._uow_factory() as uow:
            if await uow.documents.lock(image.document_id) is None:
                raise JobAbandonedError
            await uow.documents.describe_image(image.id, description, pending=pending)
        if pending:
            return replace(image, pending_description=description)
        return replace(image, description=description, description_version=DESCRIPTION_VERSION)

    async def _ask(self, image: KbImage) -> str:
        """Описание по правилам §8.6: повтор при упоре в предел, отбрасывание, обрезка.

        Пустая строка — картинка отбрасывается. Сбой модели после повторов —
        `RecognitionFailedError`.
        """
        settings = self._settings
        data = await asyncio.to_thread(self._storage.path(image.storage_key).read_bytes)
        answer = await self._request(data, image, settings.description_max_tokens)
        if answer.finish_reason == "length":
            # Весь предел мог уйти на размышления: один повтор, попыткой он не считается.
            answer = await self._request(data, image, settings.description_retry_max_tokens)
        # PostgreSQL не хранит нулевой символ в тексте.
        text = answer.text.replace("\x00", "").strip()
        fields = {"document_id": str(image.document_id), "image_id": str(image.id)}
        if not text:
            reason = "empty" if answer.finish_reason == "stop" else "length"
            logger.warning("image dropped", extra={**fields, "reason": reason})
            return ""
        if answer.finish_reason == "length":
            logger.warning("image description truncated", extra={**fields, "reason": "length"})
        if len(text) > settings.description_max_chars:
            logger.warning("image description truncated", extra={**fields, "reason": "max_chars"})
            text = text[: settings.description_max_chars]
        return text

    async def _request(self, data: bytes, image: KbImage, max_tokens: int) -> Completion:
        """Один запрос описания по протоколу §12.1 с повторами при сбое модели."""
        request = ChatRequest(
            messages=(
                ModelMessage("system", (TextPart(self._settings.system_prompt),)),
                ModelMessage("user", (TextPart(_INSTRUCTION), ImagePart(data, image.media_type))),
            ),
            reasoning_effort="low",
            max_tokens=max_tokens,
        )
        try:
            async with self._llm_slots:
                return await retrying(
                    partial(self._model.complete_with_reason, request),
                    attempts=self._llm.recognition_attempts,
                    pause_seconds=self._llm.recognition_retry_pause_seconds,
                    retry_on=(ModelUnavailableError, ModelOverloadedError),
                )
        except ModelOverloadedError as error:
            raise RecognitionFailedError("overloaded") from error
        except (ModelUnavailableError, ContextOverflowError) as error:
            raise RecognitionFailedError("unavailable") from error
