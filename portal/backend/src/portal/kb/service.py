"""Сценарии маршрутов базы знаний: список, добавление, теги, удаление, повтор, просмотр.

Документ вне правила видимости (docs/portal-api.md §8.1) — всегда `not_found`, в том
числе для администратора: условие стоит в каждом запросе хранилища.
"""

import asyncio
from collections.abc import AsyncIterator, Collection, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from portal.core.errors import (
    AppError,
    field_error,
    file_too_large,
    file_unreadable,
    forbidden,
    not_found,
    service_unavailable,
    too_many_pages,
    unauthenticated,
    unsupported_file_type,
    validation_error,
)
from portal.core.pagination import Page, PageQuery
from portal.core.ports import Clock, CurrentUser, SessionAuthenticator
from portal.core.settings import KbSettings
from portal.files.names import display_file_name
from portal.files.ports import (
    DocumentReader,
    FileStorage,
    FileTooLargeError,
    MediaStorage,
    MediaType,
    StoredRecording,
    UnreadableDocumentError,
)
from portal.kb import errors
from portal.kb.domain import (
    JOB_PRIORITY,
    DocumentView,
    Fragment,
    Job,
    KbDocument,
    KbImage,
    PageText,
    Scope,
    SortField,
    can_add,
    can_delete,
)
from portal.kb.ports import COLLECTIONS, VectorIndex, VectorIndexUnavailableError
from portal.kb.store import DocumentRepository, KbUnitOfWorkFactory
from portal.kb.tags import Tag, TagsError, TagsErrorCode, normalize_tags, tag_key

_SORT_FIELDS = ("created_at", "title")
_TAGS_MESSAGES: dict[TagsErrorCode, str] = {
    "too_many": "Слишком много тегов.",
    "too_long": "Слишком длинный тег.",
    "invalid_format": "Тег пуст или содержит недопустимые символы.",
}


def _tags_error(error: TagsError) -> AppError:
    """Отказ по полю `tags` (§8.5); сами значения тегов в ответ не попадают."""
    return validation_error([field_error("tags", error.code, _TAGS_MESSAGES[error.code])])


class DuplicateDocumentError(Exception):
    """Такой файл уже есть в коллекции, куда его добавляют."""

    def __init__(self, existing: DocumentView) -> None:
        """Запомнить существующий документ: отказ называет его автора и состояние."""
        super().__init__("duplicate document")
        self.existing = existing


@dataclass(frozen=True)
class Segment:
    """Отрезок текста страницы; `highlight` — это цитата.

    `start_seconds` — у расшифровки записи: время начала отрезка речи, который начинается
    с этого элемента; у продолжения отрезка и у документов — `None` (§8.3).
    """

    text: str
    highlight: bool
    start_seconds: int | None = None


@dataclass(frozen=True)
class RecordingTarget:
    """Проверенные до приёма тела параметры загрузки записи (§1.6)."""

    scope: Scope
    title: str
    tags: tuple[Tag, ...]


@dataclass(frozen=True)
class DocumentText:
    """Текст одной страницы документа с положением цитаты (§8.3).

    У записи страница — часть расшифровки, `timecode_seconds` — с какой секунды начать
    проигрывание. `image` — картинка источника-картинки, когда отдаётся её страница.
    """

    page: int | None
    page_count: int | None
    recognized: bool
    segments: list[Segment]
    timecode_seconds: int | None = None
    image: KbImage | None = None


def split_segments(text: str, quote: tuple[int, int] | None) -> list[Segment]:
    """Текст страницы отрезками; цитата — отрезок с `highlight`, пустые отрезки опущены."""
    if quote is None:
        parts = [(text, False)]
    else:
        start, end = quote
        parts = [(text[:start], False), (text[start:end], True), (text[end:], False)]
    return [Segment(part, highlight) for part, highlight in parts if part]


def split_transcript(
    text: str, marks: Sequence[Sequence[int]], quote: tuple[int, int] | None
) -> list[Segment]:
    """Текст части расшифровки, разрезанный по отрезкам речи и по границам цитаты (§8.3).

    Элемент, с которого отрезок речи начинается, несёт его время в целых секундах;
    продолжение отрезка после границы цитаты — `None`.
    """
    quote_start, quote_end = quote or (0, 0)
    segments: list[Segment] = []
    ends = [*(offset for offset, _ in marks[1:]), len(text)]
    for (start, time_ms), end in zip(marks, ends, strict=True):
        cuts = sorted({start, end, *(cut for cut in (quote_start, quote_end) if start < cut < end)})
        for position, (piece_start, piece_end) in enumerate(pairwise(cuts)):
            segments.append(
                Segment(
                    text[piece_start:piece_end],
                    highlight=quote_start <= piece_start < quote_end,
                    start_seconds=time_ms // 1000 if position == 0 else None,
                )
            )
    return segments


async def _lock_manageable(
    documents: DocumentRepository, user: CurrentUser, document_id: UUID
) -> KbDocument:
    """Документ под блокировкой строки, которым пользователь вправе распоряжаться.

    Невидимый — `not_found`; видимый без права по текущей роли — `forbidden` (§8.1).
    """
    view = await documents.get(user.id, document_id, lock=True)
    if view is None:
        raise not_found()
    if not can_delete(user, view.document):
        raise forbidden()
    return view.document


def _audit(user: CurrentUser, document: KbDocument) -> dict[str, object]:
    """Подробности записи аудита: без названия и содержимого документа (§9.5)."""
    return {
        "document_id": str(document.id),
        "scope": document.scope,
        "by_admin": document.owner_id != user.id,
    }


class KbService:
    """Документы базы знаний глазами пользователя."""

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        storage: FileStorage,
        media: MediaStorage,
        reader: DocumentReader,
        index: VectorIndex,
        sessions: SessionAuthenticator,
        document_executor: Executor,
        clock: Clock,
        settings: KbSettings,
    ) -> None:
        """Получить зависимости явно.

        `document_executor` — потоки чтения документов, отдельные от общего пула.
        `index` нужен только правке тегов: других записей в Qdrant у API нет (§10).
        `media` — файлы записей; `sessions` — проверка, что сессия долгой загрузки записи
        ещё существует (§1.6).
        """
        self._uow_factory = uow_factory
        self._storage = storage
        self._media = media
        self._sessions = sessions
        self._index = index
        self._reader = reader
        self._document_executor = document_executor
        self._clock = clock
        self._settings = settings

    def _selected_tag_keys(self, tags: Sequence[str]) -> list[str]:
        """Ключи тегов отбора; проверяется только формат: несуществующий тег — не ошибка."""
        limits = self._settings.tags
        try:
            selected = normalize_tags(
                tags, max_count=limits.max_selected, max_length=limits.max_length
            )
        except TagsError as error:
            raise _tags_error(error) from None
        return [tag.key for tag in selected]

    def _document_tags(self, tags: Sequence[str]) -> list[Tag]:
        """Теги документа по правилам §8.5."""
        limits = self._settings.tags
        try:
            return normalize_tags(
                tags, max_count=limits.max_per_document, max_length=limits.max_length
            )
        except TagsError as error:
            raise _tags_error(error) from None

    async def list_documents(
        self, user: CurrentUser, scope: Scope, query: PageQuery, tags: Sequence[str] = ()
    ) -> Page[DocumentView]:
        """Страница документов коллекции; `tags` — только документы с одним из тегов."""
        if query.sort not in (None, *_SORT_FIELDS):
            raise validation_error([field_error("sort", "unknown_value")])
        tag_keys = self._selected_tag_keys(tags)
        sort: SortField = "title" if query.sort == "title" else "created_at"
        order = query.order or ("asc" if sort == "title" else "desc")
        async with self._uow_factory() as uow:
            items, total = await uow.documents.list_documents(
                user.id,
                scope,
                q=query.q,
                tags=tag_keys,
                sort=sort,
                order=order,
                offset=query.offset,
                limit=query.page_size,
            )
        return Page(items, query.page, query.page_size, total)

    async def get(self, user: CurrentUser, document_id: UUID) -> DocumentView:
        """Документ, видимый пользователю."""
        async with self._uow_factory() as uow:
            view = await uow.documents.get(user.id, document_id)
        if view is None:
            raise not_found()
        return view

    async def add(
        self,
        user: CurrentUser,
        file_name: str,
        chunks: AsyncIterator[bytes],
        scope: Scope,
        is_cogis: bool,
        tags: Sequence[str] = (),
    ) -> DocumentView:
        """Принять файл и поставить его в очередь индексации.

        Документ, его теги, задание и запись аудита создаются одной транзакцией. Повтор
        файла в той же коллекции — `DuplicateDocumentError`; принятый файл при любом
        отказе удаляется. В «Министерства» добавляет только администратор.
        """
        if not can_add(user, scope):
            raise forbidden()
        if is_cogis and scope != "shared":
            raise validation_error([field_error("is_cogis", "invalid_format")])
        document_tags = self._document_tags(tags)
        limit = self._settings.document_max_bytes
        try:
            stored = await self._storage.save("kb", chunks, limit)
        except FileTooLargeError:
            raise file_too_large(limit) from None
        saved = False
        try:
            path = self._storage.path(stored.key)
            media_type, page_count = await asyncio.get_running_loop().run_in_executor(
                self._document_executor, self._inspect, path, file_name
            )
            now = self._clock.now()
            document = KbDocument(
                id=uuid4(),
                owner_id=user.id,
                scope=scope,
                is_cogis=is_cogis,
                title=display_file_name(file_name),
                media_type=media_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                storage_key=stored.key,
                page_count=page_count,
                status="queued",
                error_code=None,
                pages_done=0,
                recognizing=False,
                created_at=now,
                deleted_at=None,
            )
            async with self._uow_factory() as uow:
                if not await uow.documents.add(document):
                    existing = await uow.documents.find_duplicate(user.id, scope, stored.sha256)
                    if existing is None:  # повтор удалили между двумя запросами
                        raise service_unavailable()
                    raise DuplicateDocumentError(existing)
                await uow.documents.set_tags(document.id, document_tags)
                await uow.jobs.add(Job.new(document.id, "index", now))
                await uow.audit.record(
                    "document_uploaded",
                    actor_id=user.id,
                    ip=user.ip,
                    details={"document_id": str(document.id), "scope": scope},
                )
                await uow.commit()
                saved = True
        finally:
            if not saved:
                await self._storage.delete(stored.key)
        return self._added(user, document, document_tags)

    @staticmethod
    def _added(user: CurrentUser, document: KbDocument, tags: Sequence[Tag]) -> DocumentView:
        ordered = sorted(tags, key=lambda tag: tag.key)
        return DocumentView(document, user.full_name, tuple(tag.text for tag in ordered))

    def recording_target(
        self, user: CurrentUser, scope: str | None, file_name: str | None, tags: Sequence[str]
    ) -> RecordingTarget:
        """Проверки загрузки записи до чтения тела (§1.6, шаги 3–4): право, затем параметры."""
        if scope == "ministries" and not can_add(user, "ministries"):
            raise forbidden()
        if scope is None:
            raise validation_error([field_error("scope", "required")])
        if scope not in COLLECTIONS:
            raise validation_error([field_error("scope", "unknown_value")])
        if not file_name:
            raise validation_error([field_error("file_name", "required")])
        return RecordingTarget(
            scope, display_file_name(file_name), tuple(self._document_tags(tags))
        )

    async def add_recording(
        self,
        user: CurrentUser,
        session_id: UUID,
        target: RecordingTarget,
        recording: StoredRecording,
    ) -> DocumentView:
        """Поставить принятую запись в очередь расшифровки (§8.1).

        Тело записи принималось долго, поэтому сессия проверяется заново перед записью
        строки: блокировка и смена роли удаляют сессии, так что вместе с ней проверено и
        право на коллекцию (§1.6). Формат, длительность и наличие звука выясняет воркер.
        Любой отказ удаляет принятый файл.
        """
        now = self._clock.now()
        document = KbDocument(
            id=uuid4(),
            owner_id=user.id,
            scope=target.scope,
            is_cogis=False,
            title=target.title,
            media_type=recording.media_type,
            size_bytes=recording.size_bytes,
            sha256=recording.sha256,
            storage_key=recording.key,
            page_count=None,
            status="queued",
            error_code=None,
            pages_done=0,
            recognizing=False,
            created_at=now,
            deleted_at=None,
        )
        saved = False
        try:
            async with self._uow_factory() as uow:
                if not await self._sessions.session_exists(session_id):
                    raise unauthenticated()
                if not await uow.documents.add(document):
                    existing = await uow.documents.find_duplicate(
                        user.id, target.scope, recording.sha256
                    )
                    if existing is None:  # повтор удалили между двумя запросами
                        raise service_unavailable()
                    raise DuplicateDocumentError(existing)
                await uow.documents.set_tags(document.id, target.tags)
                await uow.jobs.add(Job.new(document.id, "index", now))
                await uow.audit.record(
                    "document_uploaded",
                    actor_id=user.id,
                    ip=user.ip,
                    details={"document_id": str(document.id), "scope": target.scope},
                )
                await uow.commit()
                saved = True
        finally:
            if not saved:
                await self._media.delete(recording.key)
        return self._added(user, document, target.tags)

    def _inspect(self, path: Path, file_name: str) -> tuple[MediaType, int | None]:
        """Тип по содержимому, читаемость и число страниц; блокирующий вызов."""
        media_type = self._reader.detect(path, file_name)
        if media_type is None:
            raise unsupported_file_type()
        try:
            page_count = self._reader.page_count(path, media_type)
        except UnreadableDocumentError:
            raise file_unreadable() from None
        max_pages = self._settings.document_max_pages
        if page_count is not None and page_count > max_pages:
            raise too_many_pages(max_pages)
        return media_type, page_count

    async def delete(self, user: CurrentUser, document_id: UUID) -> None:
        """Пометить документ удалённым и поручить стирание воркеру (§8.1, §11.3)."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            document = await _lock_manageable(uow.documents, user, document_id)
            await uow.documents.mark_deleted(user.id, document_id, now)
            for kind in ("index", "context", "reprocess"):
                await uow.jobs.remove(document_id, kind)
            await uow.jobs.add(Job.new(document_id, "delete", now))
            await uow.audit.record(
                "document_deleted", actor_id=user.id, ip=user.ip, details=_audit(user, document)
            )

    async def retry(self, user: CurrentUser, document_id: UUID) -> DocumentView:
        """Вернуть документ с ошибкой в очередь без повторной загрузки (§8.1).

        Готовый документ с отметкой о сбое переобработки остаётся готовым: отметка
        снимается сразу, а задание `reprocess` получает приоритет новой загрузки.
        """
        now = self._clock.now()
        async with self._uow_factory() as uow:
            document = await _lock_manageable(uow.documents, user, document_id)
            if document.status == "ready" and document.reprocess_error is not None:
                await uow.documents.set_reprocess_error(document_id, None)
                await uow.jobs.add_or_prioritize(
                    Job.new(document_id, "reprocess", now, JOB_PRIORITY["index"])
                )
            elif document.status != "error":
                raise errors.document_not_in_error()
            elif document.has_nothing_to_process:
                raise errors.source_unavailable()
            else:
                await uow.documents.set_status(document_id, "queued")
                await uow.jobs.add(Job.new(document_id, "index", now))
            await uow.audit.record(
                "document_retried", actor_id=user.id, ip=user.ip, details=_audit(user, document)
            )
            view = await uow.documents.get(user.id, document_id)
        if view is None:
            raise not_found()
        return view

    async def set_tags(
        self, user: CurrentUser, document_id: UUID, tags: Sequence[str]
    ) -> DocumentView:
        """Заменить теги документа целиком; повторной обработки не требуется (§8.5).

        Одна транзакция под блокировкой строки документа: теги в базе → поле `tags` его
        точек в Qdrant → аудит → фиксация. Не отвечает Qdrant — отказ, теги не меняются.
        """
        document_tags = self._document_tags(tags)
        async with self._uow_factory() as uow:
            document = await _lock_manageable(uow.documents, user, document_id)
            await uow.documents.set_tags(document_id, document_tags)
            try:
                await self._index.set_tags(document_id, sorted(tag.key for tag in document_tags))
            except VectorIndexUnavailableError:
                raise service_unavailable() from None
            await uow.audit.record(
                "document_tags_changed",
                actor_id=user.id,
                ip=user.ip,
                details=_audit(user, document),
            )
            view = await uow.documents.get(user.id, document_id)
        if view is None:
            raise not_found()
        return view

    async def suggest_tags(self, user: CurrentUser, scopes: Collection[Scope], q: str) -> list[str]:
        """Подсказки тегов: только теги документов, видимых пользователю (§8.5)."""
        key = tag_key(q)
        if "\x00" in key:  # в теге такого символа нет, а PostgreSQL его в запросе не примет
            return []
        async with self._uow_factory() as uow:
            return await uow.documents.suggest_tags(
                user.id, scopes, key, self._settings.tags.suggest_limit
            )

    async def text(
        self,
        user: CurrentUser,
        document_id: UUID,
        page: int | None,
        fragment_id: UUID | None,
        timecode: int | None = None,
    ) -> DocumentText:
        """Текст страницы готового документа; цитата — фрагмент на этой странице (§8.3).

        У записи страница — часть расшифровки; `timecode` (секунды) выбирает часть, когда
        не заданы ни страница, ни существующий фрагмент.
        """
        async with self._uow_factory() as uow:
            view = await uow.documents.get(user.id, document_id)
            if view is None:
                raise not_found()
            document = view.document
            if document.status != "ready":
                raise errors.document_not_ready()
            fragment = None
            if fragment_id is not None:
                fragment = await uow.documents.fragment(user.id, document_id, fragment_id)
            by_timecode = False
            if document.page_count is None and not document.is_recording:
                number = 1
            elif page is not None:
                number = page
            elif fragment is not None:
                number = fragment.page_number
            elif document.is_recording and timecode is not None:
                by_timecode = True
                number = await uow.documents.page_at(user.id, document_id, timecode) or 1
            else:
                number = 1
            found = await uow.documents.page(user.id, document_id, number)
            if found is None:
                raise not_found()
            page_count = document.page_count
            if document.is_recording:
                page_count = await uow.documents.page_count(user.id, document_id)
            here = fragment if fragment is not None and fragment.page_number == number else None
            image = None
            if here is not None and here.image_id is not None:
                image = await uow.documents.image(user.id, document_id, here.image_id)
        quote = None
        if here is not None and here.image_id is None:
            quote = (here.start_offset, here.end_offset)
        if not document.is_recording:
            return DocumentText(
                page=number if page_count is not None else None,
                page_count=page_count,
                recognized=found.recognized,
                segments=split_segments(found.text, quote),
                image=image,
            )
        return DocumentText(
            page=number,
            page_count=page_count,
            recognized=True,
            segments=split_transcript(found.text, found.marks or (), quote),
            timecode_seconds=_play_from(found, here, timecode if by_timecode else None),
        )

    async def file(
        self, user: CurrentUser, document_id: UUID
    ) -> tuple[KbDocument, MediaType, Path]:
        """Оригинал документа: доступен в любом состоянии (§8.2).

        У записи оригинала для показа нет: исходный файл наружу не отдаётся никогда.
        """
        document = (await self.get(user, document_id)).document
        if document.is_recording or document.storage_key is None:
            raise not_found()
        media_type = cast(MediaType, document.media_type)
        return document, media_type, self._storage.path(document.storage_key)

    async def image(
        self, user: CurrentUser, document_id: UUID, image_id: UUID
    ) -> tuple[KbImage, Path]:
        """Картинка документа; право проверяется при каждой выдаче (§8.6).

        Правило видимости документа стоит в самом запросе; картинка обязана принадлежать
        этому документу.
        """
        async with self._uow_factory() as uow:
            image = await uow.documents.image(user.id, document_id, image_id)
        if image is None:
            raise not_found()
        return image, self._storage.path(image.storage_key)

    async def audio(self, user: CurrentUser, document_id: UUID) -> Path:
        """Сжатый звук записи для прослушивания (§8.7)."""
        document = (await self.get(user, document_id)).document
        if not document.is_recording:
            raise not_found()
        if document.audio_key is None:
            raise errors.document_not_ready()
        return self._media.path(document.audio_key)

    async def has_cogis_documentation(self) -> bool:
        """Есть ли в общей базе готовая документация CoGIS."""
        async with self._uow_factory() as uow:
            return await uow.documents.has_cogis_documentation()


def _play_from(part: PageText, quote: Fragment | None, timecode: int | None) -> int | None:
    """С какой секунды начать проигрывание (§8.3).

    Начало подсвеченной цитаты; без подсветки — момент, по которому выбрана часть;
    иначе — начало части.
    """
    if quote is not None and quote.start_ms is not None:
        return quote.start_ms // 1000
    if timecode is not None:
        return timecode
    return None if part.start_ms is None else part.start_ms // 1000
