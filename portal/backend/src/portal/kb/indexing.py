"""Конвейер индексации, переобработка и стирание документа (docs/portal-api.md §10.6, §11).

Сценарий работает только через порты: читатель документов, распознавание, эмбеддинги,
векторный индекс, хранилище. Содержимое документов в журнал не попадает.
"""

import asyncio
import logging
from array import array
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import replace
from functools import partial
from itertools import batched
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from portal.core.logging import code_locations
from portal.core.ports import Clock
from portal.core.settings import KbSettings
from portal.files.ports import (
    DocumentReader,
    FileStorage,
    MediaStorage,
    MediaType,
    UnreadableDocumentError,
)
from portal.kb.chunking import split_document
from portal.kb.domain import (
    BULK_CONTEXT_PRIORITY,
    BULK_PRIORITY_FROM,
    Fragment,
    Job,
    KbDocument,
    KbImage,
    PageText,
)
from portal.kb.images import CollectedImages, ImageCollector
from portal.kb.lexical import sparse_vector
from portal.kb.outcomes import JobAbandonedError, PermanentIndexError
from portal.kb.ports import (
    Embedder,
    EmbeddingsUnavailableError,
    FragmentPoint,
    PageRecognizer,
    VectorIndex,
    VectorIndexUnavailableError,
)
from portal.kb.recordings import RecordingProcessor
from portal.kb.retrying import retrying
from portal.kb.search_text import search_text
from portal.kb.store import KbUnitOfWorkFactory
from portal.media.transcription import mark_time

logger = logging.getLogger(__name__)


def _log_failure(message: str, document_id: UUID, error: Exception) -> None:
    """Сбой, который задание не роняет: в журнал — документ, тип ошибки и места в коде."""
    logger.error(
        message,
        extra={
            "document_id": str(document_id),
            "error_type": type(error).__name__,
            "trace": code_locations(error),
        },
    )


async def _uninterrupted(work: Coroutine[Any, Any, None]) -> None:
    """Довести работу до конца, даже если задание отменено (остановка воркера, снятие)."""
    task = asyncio.ensure_future(work)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class Indexer:
    """Выполняет задания `index`, `reprocess` и `delete` над одним документом."""

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        storage: FileStorage,
        media: MediaStorage,
        reader: DocumentReader,
        recognizer: PageRecognizer,
        images: ImageCollector,
        recordings: RecordingProcessor,
        embedder: Embedder,
        index: VectorIndex,
        clock: Clock,
        settings: KbSettings,
        recognition_batch: int,
    ) -> None:
        """Получить зависимости явно.

        `storage` — оригиналы документов и их картинки, `media` — исходники и сжатый звук
        записей. `recognition_batch` — сколько страниц-сканов распознаётся одним вызовом
        порта: столько растров страниц одновременно держится в памяти.
        """
        self._uow_factory = uow_factory
        self._storage = storage
        self._media = media
        self._reader = reader
        self._images = images
        self._recordings = recordings
        self._recognizer = recognizer
        self._embedder = embedder
        self._index = index
        self._clock = clock
        self._settings = settings
        self._recognition_batch = recognition_batch

    async def index(self, job: Job, on_replaced: Callable[[], object] | None = None) -> None:
        """Прочитать страницы, разбить на фрагменты, посчитать эмбеддинги, заменить индекс.

        Идемпотентно: уже сохранённые страницы не читаются и не распознаются заново, а
        прежний индекс действует до фиксации завершения (§10.6). `on_replaced` вызывается
        сразу после неё: задание выполнено и его строки больше нет, остаётся уборка.
        """
        document = await self._start(job.document_id)
        try:
            images = CollectedImages()
            if document.is_recording:
                await self._recordings.transcribe(job, document, self._save_page)
            else:
                path, media_type = self._original(document)
                await self._read_pages(job, document, path, media_type)
                images = await self._images.collect(job, document, path, media_type)
            dropped_images = await self._build(job, document, images)
        except BaseException:
            await _uninterrupted(self.prune(document.id))
            raise
        await self._replaced(document, dropped_images, on_replaced)

    async def reprocess(self, job: Job, on_replaced: Callable[[], object] | None = None) -> None:
        """Построить индекс готового документа заново, не убирая его из поиска (§11.6).

        Текст берётся из сохранённых страниц, из файла — только картинки. Состояние
        документа не меняется; документ не в `ready` обрабатывает задание `index`.
        `on_replaced` — как у `index`.
        """
        async with self._uow_factory() as uow:
            document = await uow.documents.lock(job.document_id)
        if document is None or document.status != "ready":
            raise JobAbandonedError
        try:
            images = CollectedImages()
            if not document.is_recording:
                images = await self._images.collect(job, document, *self._original(document))
            dropped_images = await self._build(job, document, images)
        except BaseException:
            await _uninterrupted(self.prune(document.id))
            raise
        await self._replaced(document, dropped_images, on_replaced)

    async def _replaced(
        self,
        document: KbDocument,
        dropped_images: Sequence[str],
        on_replaced: Callable[[], object] | None,
    ) -> None:
        """Индекс заменён: сообщить об этом и довести уборку до конца, что бы ни случилось."""
        if on_replaced is not None:
            on_replaced()
        await _uninterrupted(self._after_replacement(document, dropped_images))

    async def drop_unindexed_images(self, document_id: UUID) -> None:
        """Убрать картинки, по которым нет фрагмента: их оставила несостоявшаяся переобработка.

        Документ остаётся в прежнем виде (§11.6): с прежним индексом и прежними картинками.
        """
        async with self._uow_factory() as uow:
            if await uow.documents.lock(document_id) is None:
                return
            dropped_images = await uow.documents.drop_unreferenced_images(document_id)
        await self._delete_images(document_id, dropped_images)

    async def prune(self, document_id: UUID) -> None:
        """Уборка лишних точек документа после попытки или после замены индекса (§10.6).

        Сверяется с зафиксированными фрагментами, поэтому верна при любом исходе
        завершения. Сбой уборки сбоем задания не считается: лишние точки в ответ не
        попадают, их уберёт следующая обработка документа.
        """
        try:
            await self._prune(document_id)
        except Exception as error:
            _log_failure("document points not pruned", document_id, error)

    async def _prune(self, document_id: UUID) -> None:
        async with self._uow_factory() as uow:
            current = await uow.documents.fragment_ids(document_id)
        await self._with_retries(
            partial(self._index.prune_document, document_id, current), VectorIndexUnavailableError
        )

    def _original(self, document: KbDocument) -> tuple[Path, MediaType]:
        # У документа, не являющегося записью, оригинал есть всегда (§9.11).
        assert document.storage_key is not None
        return self._storage.path(document.storage_key), cast(MediaType, document.media_type)

    async def _build(self, job: Job, document: KbDocument, images: CollectedImages) -> list[str]:
        """Фрагменты и эмбеддинги по сохранённым страницам, запас и завершение (§10.6).

        Возвращает ключи файлов картинок, строки которых завершение удалило.
        """
        async with self._uow_factory() as uow:
            pages = await uow.documents.pages(document.id)
        if not images.described and not any(page.text.strip() for page in pages):
            raise PermanentIndexError("no_text")
        fragments, texts = self._split(document, pages, images.described)
        vectors = await self._embed(texts)
        points = [
            FragmentPoint(
                fragment_id=fragment.id,
                document_id=document.id,
                scope=document.scope,
                owner_id=document.owner_id,
                is_cogis=document.is_cogis,
                tags=(),  # теги ставит завершение — по базе, под блокировкой строки документа
                dense=vector,
                lexical=sparse_vector(text),
            )
            for fragment, text, vector in zip(fragments, texts, vectors, strict=True)
        ]
        # Запас: поиск его не видит, прежний индекс не тронут.
        await self._prune(document.id)
        await self._with_retries(
            partial(self._index.stage_document, document.id, points), VectorIndexUnavailableError
        )
        return await self._finish(job, document.id, fragments, images.truncated)

    async def _drop_recording_source(self, document_id: UUID) -> None:
        """После успешной обработки исходник записи удаляется: видео не хранится (§8.7).

        Неудалённый исходник уберёт уборка воркера.
        """
        try:
            await self._recordings.drop_source(document_id)
        except Exception as error:
            _log_failure("recording source not deleted", document_id, error)

    async def erase(self, job: Job) -> None:
        """Стереть документ: точки Qdrant → файлы → строка; каждый шаг идемпотентен.

        Файлы — оригинал и картинки документа; у записи — исходник и сжатый звук.
        """
        async with self._uow_factory() as uow:
            document = await uow.documents.lock(job.document_id, include_deleted=True)
            if document is None:
                return
            await self._with_retries(
                partial(self._index.delete_document, document.id), VectorIndexUnavailableError
            )
            source = self._media if document.is_recording else self._storage
            if document.storage_key is not None:
                await source.delete(document.storage_key)
            if document.audio_key is not None:
                await self._media.delete(document.audio_key)
            for image in await uow.documents.images(document.id):
                await self._storage.delete(image.storage_key)
            await uow.documents.delete(document.id)

    async def _start(self, document_id: UUID) -> KbDocument:
        async with self._uow_factory() as uow:
            document = await uow.documents.lock(document_id)
            if document is None:
                raise JobAbandonedError
            await uow.documents.set_status(document_id, "processing")
        return document

    async def _read_pages(
        self, job: Job, document: KbDocument, path: Path, media_type: MediaType
    ) -> None:
        """Сохранить текст каждой ещё не прочитанной страницы; сканы — через распознавание."""
        async with self._uow_factory() as uow:
            stored = await uow.documents.stored_pages(document.id)
        chars = sum(stored.values())
        recognizing = document.recognizing
        scans: list[tuple[int, bytes]] = []
        for number in range(1, (document.page_count or 1) + 1):
            if number in stored:
                continue
            text = await self._read(self._reader.page_text, path, media_type, number)
            if text is not None:
                chars = await self._save_page(document.id, PageText(number, text, False), chars)
                continue
            if not recognizing:
                recognizing = True
                async with self._uow_factory() as uow:
                    await uow.documents.set_recognizing(document.id)
            scans.append(
                (number, await self._read(self._reader.page_image, path, media_type, number))
            )
            if len(scans) == self._recognition_batch:
                chars = await self._recognize(job, scans, chars)
                scans = []
        if scans:
            await self._recognize(job, scans, chars)

    async def _read[T](
        self,
        read: Callable[[Path, MediaType, int], T],
        path: Path,
        media_type: MediaType,
        number: int,
    ) -> T:
        """Вызов читателя документов в пуле потоков.

        Файл или отдельная страница не читаются — постоянный сбой: повторы не помогут.
        Сбой ввода-вывода (файл временно недоступен на диске) остаётся временным (§11.2).
        """
        try:
            return await asyncio.to_thread(read, path, media_type, number)
        except UnreadableDocumentError:
            raise PermanentIndexError("file_unreadable") from None

    async def _recognize(self, job: Job, scans: Sequence[tuple[int, bytes]], chars: int) -> int:
        """Распознать пачку страниц-сканов и сохранить их текст.

        Перед пачкой сверяется, что задание не снято: распознавание занимает GPU основной
        модели, и ждать продления аренды (до трети её срока), чтобы заметить удаление
        документа, нельзя — всё это время стояло бы задание стирания.
        """
        async with self._uow_factory() as uow:
            if not await uow.jobs.exists(job.id):
                raise JobAbandonedError
        texts = await self._recognizer.recognize([image for _, image in scans])
        for (number, _), text in zip(scans, texts, strict=True):
            chars = await self._save_page(job.document_id, PageText(number, text, True), chars)
        return chars

    async def _save_page(self, document_id: UUID, page: PageText, chars: int) -> int:
        """Сохранить страницу; вернуть объём текста документа вместе с ней."""
        # PostgreSQL не хранит нулевой символ в тексте.
        text = page.text.replace("\x00", "")
        chars += len(text)
        if chars > self._settings.indexing.document_max_chars:
            raise PermanentIndexError("document_too_long")
        async with self._uow_factory() as uow:
            if await uow.documents.lock(document_id) is None:
                raise JobAbandonedError
            await uow.documents.add_page(document_id, replace(page, text=text))
        return chars

    def _split(
        self, document: KbDocument, pages: Sequence[PageText], images: Sequence[KbImage]
    ) -> tuple[list[Fragment], list[str]]:
        """Фрагменты документа и их тексты для поиска — пока без пояснений.

        Текст страниц режется по структуре; у фрагмента расшифровки — таймкод: время
        последней отметки не позже его начала (§8.7). Затем — по фрагменту на каждую
        описанную картинку: его текст — описание (§8.6).
        """
        chunking = self._settings.chunking
        by_number = {page.number: page for page in pages}
        spans = split_document(
            [(page.number, page.text) for page in pages],
            chunking.heading_patterns,
            chunking.max_chars,
            chunking.overlap_chars,
            chunking.section_max_chars,
        )
        fragments = [
            Fragment(
                id=uuid4(),
                document_id=document.id,
                page_number=span.page_number,
                ordinal=ordinal,
                start_offset=span.start,
                end_offset=span.end,
                section=span.section,
                start_ms=mark_time(by_number[span.page_number].marks or (), span.start),
            )
            for ordinal, span in enumerate(spans)
        ]
        texts = [
            search_text(
                document.title,
                span.section,
                None,
                by_number[span.page_number].text[span.start : span.end],
                chunking,
            )
            for span in spans
        ]
        for ordinal, image in enumerate(images, len(spans)):
            # У документа без страниц картинка относится к единственной странице текста.
            fragments.append(
                Fragment(
                    uuid4(), document.id, image.page_number or 1, ordinal, 0, 0, image_id=image.id
                )
            )
            texts.append(search_text(document.title, None, None, image.description or "", chunking))
        return fragments, texts

    async def _embed(self, texts: Sequence[str]) -> list[array[float]]:
        """Эмбеддинги пачками; в памяти — плотно упакованные числа одинарной точности."""
        vectors: list[array[float]] = []
        for batch in batched(texts, self._settings.embeddings.batch_size):
            embedded = await self._with_retries(
                partial(self._embedder.embed_documents, batch), EmbeddingsUnavailableError
            )
            vectors.extend(array("f", vector) for vector in embedded)
        return vectors

    async def _with_retries[T](self, call: Callable[[], Awaitable[T]], error: type[Exception]) -> T:
        """Отдельный вызов внешней службы повторяется внутри задания (§11.2)."""
        worker = self._settings.worker
        return await retrying(
            call,
            attempts=worker.call_attempts,
            pause_seconds=worker.call_retry_pause_seconds,
            retry_on=(error,),
        )

    async def _finish(
        self,
        job: Job,
        document_id: UUID,
        fragments: Sequence[Fragment],
        images_truncated: bool | None,
    ) -> list[str]:
        """Завершение: одна транзакция под блокировкой строки документа (§10.6, шаг 2).

        Момент замены индекса — её фиксация: с неё повторная проверка поиска пропускает
        только новые точки. Снятое задание индекс не заменяет. Возвращает ключи файлов
        картинок, строки которых удалены.
        """
        async with self._uow_factory() as uow:
            if await uow.documents.lock(document_id) is None or not await uow.jobs.exists(job.id):
                raise JobAbandonedError
            await uow.documents.replace_fragments(document_id, fragments)
            if images_truncated is not None:
                await uow.documents.set_images_truncated(document_id, images_truncated)
            # Картинки, которых среди извлечённых больше нет (§8.6): строки — здесь, файлы —
            # после фиксации.
            dropped_images = await uow.documents.drop_unreferenced_images(document_id)
            await uow.documents.mark_indexed(document_id)
            if job.kind == "index":
                await uow.documents.set_status(document_id, "ready")
            if self._settings.context.enabled:
                # Пояснения дописывает следом отдельное задание: документ уже ищется (§11.5).
                bulk = job.kind == "reprocess" and job.priority >= BULK_PRIORITY_FROM
                await uow.jobs.add_if_absent(
                    Job.new(
                        document_id,
                        "context",
                        self._clock.now(),
                        BULK_CONTEXT_PRIORITY if bulk else None,
                    )
                )
            await uow.jobs.finish(job.id)
            # Правка тегов идёт под той же блокировкой (§8.5): запас не введётся в действие
            # со старыми тегами.
            tags = await uow.documents.tag_keys(document_id)
            await self._with_retries(
                partial(self._index.publish_document, document_id, tags),
                VectorIndexUnavailableError,
            )
        return dropped_images

    async def _after_replacement(self, document: KbDocument, dropped_images: Sequence[str]) -> None:
        """Индекс уже заменён: убрать прежние точки, файлы прежних картинок и исходник записи.

        Задание выполнено (§10.6, шаг 2.4), поэтому сбой здесь его сбоем не считается.
        """
        await self.prune(document.id)
        await self._delete_images(document.id, dropped_images)
        if document.is_recording:
            await self._drop_recording_source(document.id)

    async def _delete_images(self, document_id: UUID, keys: Sequence[str]) -> None:
        """Удалить файлы картинок, строк которых уже нет; сбой — только в журнал."""
        try:
            for key in keys:
                await self._storage.delete(key)
        except Exception as error:
            _log_failure("dropped images not deleted", document_id, error)
