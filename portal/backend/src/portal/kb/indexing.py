"""Конвейер индексации и стирание документа (docs/portal-api.md §11.2, §11.3).

Сценарий работает только через порты: читатель документов, распознавание, эмбеддинги,
векторный индекс, хранилище. Содержимое документов в журнал не попадает.
"""

import asyncio
from array import array
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from itertools import batched
from pathlib import Path
from uuid import UUID, uuid4

from portal.core.settings import KbSettings
from portal.files.ports import (
    DocumentReader,
    FileStorage,
    MediaType,
    UnreadableDocumentError,
)
from portal.kb.chunking import split_page
from portal.kb.domain import ErrorCode, Fragment, Job, KbDocument, PageText
from portal.kb.lexical import sparse_vector
from portal.kb.ports import (
    Embedder,
    EmbeddingsUnavailableError,
    FragmentPoint,
    PageRecognizer,
    VectorIndex,
    VectorIndexUnavailableError,
)
from portal.kb.retrying import retrying
from portal.kb.store import KbUnitOfWorkFactory


class PermanentIndexError(Exception):
    """Причина в самом файле: повтор задания не поможет."""

    def __init__(self, code: ErrorCode) -> None:
        """Запомнить код ошибки документа."""
        super().__init__(code)
        self.code: ErrorCode = code


class JobAbandonedError(Exception):
    """Документа больше нет или он помечен удалённым: работу надо прекратить."""


class Indexer:
    """Выполняет задания `index` и `delete` над одним документом."""

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        storage: FileStorage,
        reader: DocumentReader,
        recognizer: PageRecognizer,
        embedder: Embedder,
        index: VectorIndex,
        settings: KbSettings,
        recognition_batch: int,
    ) -> None:
        """Получить зависимости явно.

        `recognition_batch` — сколько страниц-сканов распознаётся одним вызовом порта:
        столько растров страниц одновременно держится в памяти.
        """
        self._uow_factory = uow_factory
        self._storage = storage
        self._reader = reader
        self._recognizer = recognizer
        self._embedder = embedder
        self._index = index
        self._settings = settings
        self._recognition_batch = recognition_batch

    async def index(self, job: Job) -> None:
        """Прочитать страницы, разбить на фрагменты, посчитать эмбеддинги, записать индекс.

        Идемпотентно: уже сохранённые страницы не читаются и не распознаются заново, а
        завершение начинается с удаления прежних фрагментов и точек.
        """
        document = await self._start(job.document_id)
        await self._read_pages(job, document)
        async with self._uow_factory() as uow:
            pages = await uow.documents.pages(document.id)
        if not any(page.text.strip() for page in pages):
            raise PermanentIndexError("no_text")
        fragments, texts = self._split(document.id, pages)
        vectors = await self._embed(texts)
        points = [
            FragmentPoint(
                fragment_id=fragment.id,
                document_id=document.id,
                scope=document.scope,
                owner_id=document.owner_id,
                is_cogis=document.is_cogis,
                dense=vector,
                lexical=sparse_vector(text),
            )
            for fragment, text, vector in zip(fragments, texts, vectors, strict=True)
        ]
        await self._finish(job, document.id, fragments, points)

    async def erase(self, job: Job) -> None:
        """Стереть документ: точки Qdrant → файл → строка; каждый шаг идемпотентен."""
        async with self._uow_factory() as uow:
            document = await uow.documents.lock(job.document_id, include_deleted=True)
            if document is None:
                return
            await self._with_retries(
                partial(self._index.delete_document, document.id), VectorIndexUnavailableError
            )
            await self._storage.delete(document.storage_key)
            await uow.documents.delete(document.id)

    async def _start(self, document_id: UUID) -> KbDocument:
        async with self._uow_factory() as uow:
            document = await uow.documents.lock(document_id)
            if document is None:
                raise JobAbandonedError
            await uow.documents.set_status(document_id, "processing")
        return document

    async def _read_pages(self, job: Job, document: KbDocument) -> None:
        """Сохранить текст каждой ещё не прочитанной страницы; сканы — через распознавание."""
        path = self._storage.path(document.storage_key)
        async with self._uow_factory() as uow:
            stored = await uow.documents.stored_pages(document.id)
        chars = sum(stored.values())
        recognizing = document.recognizing
        scans: list[tuple[int, bytes]] = []
        for number in range(1, (document.page_count or 1) + 1):
            if number in stored:
                continue
            text = await self._read(self._reader.page_text, path, document, number)
            if text is not None:
                chars = await self._save_page(document.id, PageText(number, text, False), chars)
                continue
            if not recognizing:
                recognizing = True
                async with self._uow_factory() as uow:
                    await uow.documents.set_recognizing(document.id)
            scans.append(
                (number, await self._read(self._reader.page_image, path, document, number))
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
        document: KbDocument,
        number: int,
    ) -> T:
        """Вызов читателя документов в пуле потоков.

        Файл или отдельная страница не читаются — постоянный сбой: повторы не помогут.
        Сбой ввода-вывода (файл временно недоступен на диске) остаётся временным (§11.2).
        """
        try:
            return await asyncio.to_thread(read, path, document.media_type, number)
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
            await uow.documents.add_page(document_id, PageText(page.number, text, page.recognized))
        return chars

    def _split(
        self, document_id: UUID, pages: Sequence[PageText]
    ) -> tuple[list[Fragment], list[str]]:
        """Фрагменты всех страниц по порядку и их тексты."""
        chunking = self._settings.chunking
        fragments: list[Fragment] = []
        texts: list[str] = []
        for page in pages:
            for start, end in split_page(page.text, chunking.max_chars, chunking.overlap_chars):
                fragments.append(
                    Fragment(uuid4(), document_id, page.number, len(fragments), start, end)
                )
                texts.append(page.text[start:end])
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
        points: Sequence[FragmentPoint],
    ) -> None:
        """Завершение одной транзакцией под блокировкой строки документа (§11.2)."""
        async with self._uow_factory() as uow:
            if await uow.documents.lock(document_id) is None:
                raise JobAbandonedError
            await uow.documents.replace_fragments(document_id, fragments)
            await self._with_retries(
                partial(self._index.replace_document, document_id, points),
                VectorIndexUnavailableError,
            )
            await uow.documents.set_status(document_id, "ready")
            await uow.jobs.finish(job.id)
