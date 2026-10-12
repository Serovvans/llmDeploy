"""Картинки документа: извлечение, хранение и описание моделью (docs/portal-api.md §8.6, §12.1).

Сценарий работает через порты: читатель документов, хранилище, модель. Описание — текст
недоверенный, как текст документа; в журнал оно не попадает.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

from portal.core.settings import KbImagesSettings, LlmSettings
from portal.files.ports import (
    DocumentImage,
    DocumentReader,
    FileStorage,
    MediaType,
    UnreadableDocumentError,
)
from portal.kb.domain import Job, KbDocument, KbImage
from portal.kb.outcomes import JobAbandonedError, PermanentIndexError
from portal.kb.ports import RecognitionFailedError
from portal.kb.retrying import retrying
from portal.kb.store import KbUnitOfWorkFactory
from portal.llm.ports import (
    ChatModel,
    ChatRequest,
    ContextOverflowError,
    ImagePart,
    ModelMessage,
    ModelOverloadedError,
    ModelUnavailableError,
    TextPart,
)

# Текст запроса зафиксирован контрактом (§12.1): по нему заглушка стенда узнаёт описание.
_INSTRUCTION = "Опиши изображение из документа."
_PDF: MediaType = "application/pdf"


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


@dataclass(frozen=True)
class CollectedImages:
    """Итог шага «Картинки».

    `described` — описанные картинки по порядку; `truncated` — картинок в файле больше
    лимита, `None` — файл на картинки не разбирался, признак документа остаётся прежним.
    Признак записывает завершение задания вместе с заменой индекса (§10.6): при сбое
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
        model: ChatModel,
        llm_slots: asyncio.Semaphore,
        settings: KbImagesSettings,
        llm: LlmSettings,
    ) -> None:
        """Получить зависимости явно.

        `llm_slots` — общий на воркер предел фоновых обращений к основной модели
        (`kb.worker.background_llm_parallel`).
        """
        self._uow_factory = uow_factory
        self._storage = storage
        self._reader = reader
        self._model = model
        self._llm_slots = llm_slots
        self._settings = settings
        self._llm = llm

    async def collect(
        self, job: Job, document: KbDocument, path: Path, media_type: MediaType
    ) -> CollectedImages:
        """Описанные картинки документа по порядку: по каждой строится фрагмент-картинка.

        Картинка с тем же содержимым (по SHA-256) сохраняет строку, идентификатор и
        описание — повторно не описывается. При выключенном `kb.images.enabled` файл на
        картинки не разбирается, уже сохранённые остаются.
        """
        async with self._uow_factory() as uow:
            stored = await uow.documents.images(document.id)
        if not self._settings.enabled:
            return CollectedImages([image for image in stored if image.description])
        current, truncated = await self._extract(document, path, media_type, stored)
        described = [
            described
            for image in current
            if (described := await self._described(job, image)) is not None
        ]
        return CollectedImages(described, truncated)

    async def _extract(
        self, document: KbDocument, path: Path, media_type: MediaType, stored: Sequence[KbImage]
    ) -> tuple[list[KbImage], bool]:
        """Картинки файла и признак «их больше лимита»: одинаковые слиты в первую."""
        known = {image.sha256: image for image in stored}
        current: dict[bytes, KbImage] = {}
        truncated = False
        for page in range(1, (document.page_count or 1) + 1):
            for found in await self._read(path, media_type, page):
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

    async def _read(self, path: Path, media_type: MediaType, page: int) -> list[DocumentImage]:
        settings = self._settings
        try:
            return await asyncio.to_thread(
                self._reader.embedded_images,
                path,
                media_type,
                page,
                min_side_px=settings.min_side_px,
                min_area_px=settings.min_area_px,
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

    async def _described(self, job: Job, image: KbImage) -> KbImage | None:
        """Картинка с описанием; `None` — описание пустое, картинка не сохраняется (§8.6)."""
        if image.description is not None:
            return image
        async with self._uow_factory() as uow:
            # Описание занимает GPU основной модели: снятое задание его не тратит (§11.2).
            if not await uow.jobs.exists(job.id):
                raise JobAbandonedError
        description = await self._ask(image)
        async with self._uow_factory() as uow:
            if await uow.documents.lock(image.document_id) is None:
                raise JobAbandonedError
            if description:
                await uow.documents.describe_image(image.id, description)
            else:
                await uow.documents.delete_image(image.id)
        if not description:
            await self._storage.delete(image.storage_key)
            return None
        return replace(image, description=description)

    async def _ask(self, image: KbImage) -> str:
        """Запрос описания по протоколу §12.1; сбой после повторов — `RecognitionFailedError`."""
        data = await asyncio.to_thread(self._storage.path(image.storage_key).read_bytes)
        request = ChatRequest(
            messages=(
                ModelMessage("system", (TextPart(self._settings.system_prompt),)),
                ModelMessage("user", (TextPart(_INSTRUCTION), ImagePart(data, image.media_type))),
            ),
            reasoning_effort="low",
            max_tokens=self._settings.description_max_tokens,
        )
        try:
            async with self._llm_slots:
                text = await retrying(
                    partial(self._model.complete, request),
                    attempts=self._llm.recognition_attempts,
                    pause_seconds=self._llm.recognition_retry_pause_seconds,
                    retry_on=(ModelUnavailableError, ModelOverloadedError),
                )
        except ModelOverloadedError as error:
            raise RecognitionFailedError("overloaded") from error
        except (ModelUnavailableError, ContextOverflowError) as error:
            raise RecognitionFailedError("unavailable") from error
        # PostgreSQL не хранит нулевой символ в тексте.
        return text.strip().replace("\x00", "")
