"""Сценарии маршрутов базы знаний: список, добавление, удаление, повторная обработка, просмотр.

Документ вне правила видимости (docs/portal-api.md §8.1) — всегда `not_found`, в том
числе для администратора: условие стоит в каждом запросе хранилища.
"""

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import Executor
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from portal.core.errors import (
    field_error,
    file_too_large,
    file_unreadable,
    forbidden,
    not_found,
    service_unavailable,
    too_many_pages,
    unsupported_file_type,
    validation_error,
)
from portal.core.pagination import Page, PageQuery
from portal.core.ports import Clock, CurrentUser
from portal.core.settings import KbSettings
from portal.files.names import display_file_name
from portal.files.ports import (
    DocumentReader,
    FileStorage,
    FileTooLargeError,
    MediaType,
    UnreadableDocumentError,
)
from portal.kb import errors
from portal.kb.domain import DocumentView, Job, KbDocument, Scope, SortField, can_delete
from portal.kb.store import DocumentRepository, KbUnitOfWorkFactory

_SORT_FIELDS = ("created_at", "title")


class DuplicateDocumentError(Exception):
    """Такой файл уже есть в коллекции, куда его добавляют."""

    def __init__(self, existing: DocumentView) -> None:
        """Запомнить существующий документ: отказ называет его автора и состояние."""
        super().__init__("duplicate document")
        self.existing = existing


@dataclass(frozen=True)
class Segment:
    """Отрезок текста страницы; `highlight` — это цитата."""

    text: str
    highlight: bool


@dataclass(frozen=True)
class DocumentText:
    """Текст одной страницы документа с положением цитаты (§8.3)."""

    page: int | None
    page_count: int | None
    recognized: bool
    segments: list[Segment]


def split_segments(text: str, quote: tuple[int, int] | None) -> list[Segment]:
    """Текст страницы отрезками; цитата — отрезок с `highlight`, пустые отрезки опущены."""
    if quote is None:
        parts = [(text, False)]
    else:
        start, end = quote
        parts = [(text[:start], False), (text[start:end], True), (text[end:], False)]
    return [Segment(part, highlight) for part, highlight in parts if part]


async def _lock_manageable(
    documents: DocumentRepository, user: CurrentUser, document_id: UUID
) -> KbDocument:
    """Документ под блокировкой строки, которым пользователь вправе распоряжаться.

    Невидимый — `not_found`; видимый, но чужой общий — `forbidden` (§8.1).
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
        reader: DocumentReader,
        document_executor: Executor,
        clock: Clock,
        settings: KbSettings,
    ) -> None:
        """Получить зависимости явно.

        `document_executor` — потоки чтения документов, отдельные от общего пула.
        """
        self._uow_factory = uow_factory
        self._storage = storage
        self._reader = reader
        self._document_executor = document_executor
        self._clock = clock
        self._settings = settings

    async def list_documents(
        self, user: CurrentUser, scope: Scope, query: PageQuery
    ) -> Page[DocumentView]:
        """Страница документов общей базы или личной базы пользователя."""
        if query.sort not in (None, *_SORT_FIELDS):
            raise validation_error([field_error("sort", "unknown_value")])
        sort: SortField = "title" if query.sort == "title" else "created_at"
        order = query.order or ("asc" if sort == "title" else "desc")
        async with self._uow_factory() as uow:
            items, total = await uow.documents.list_documents(
                user.id,
                scope,
                q=query.q,
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
    ) -> DocumentView:
        """Принять файл и поставить его в очередь индексации.

        Документ, задание и запись аудита создаются одной транзакцией. Повтор файла в
        той же коллекции — `DuplicateDocumentError`; принятый файл при любом отказе
        удаляется.
        """
        if is_cogis and scope != "shared":
            raise validation_error([field_error("is_cogis", "invalid_format")])
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
                await uow.jobs.add(Job(uuid4(), document.id, "index", 0, now, None, now))
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
        return DocumentView(document, user.full_name)

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
            await uow.jobs.remove(document_id, "index")
            await uow.jobs.add(Job(uuid4(), document_id, "delete", 0, now, None, now))
            await uow.audit.record(
                "document_deleted", actor_id=user.id, ip=user.ip, details=_audit(user, document)
            )

    async def retry(self, user: CurrentUser, document_id: UUID) -> DocumentView:
        """Вернуть документ с ошибкой в очередь без повторной загрузки (§8.1)."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            document = await _lock_manageable(uow.documents, user, document_id)
            if document.status != "error":
                raise errors.document_not_in_error()
            await uow.documents.set_status(document_id, "queued")
            await uow.jobs.add(Job(uuid4(), document_id, "index", 0, now, None, now))
            await uow.audit.record(
                "document_retried", actor_id=user.id, ip=user.ip, details=_audit(user, document)
            )
            view = await uow.documents.get(user.id, document_id)
        if view is None:
            raise not_found()
        return view

    async def text(
        self, user: CurrentUser, document_id: UUID, page: int | None, fragment_id: UUID | None
    ) -> DocumentText:
        """Текст страницы готового документа; цитата — фрагмент на этой странице."""
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
            if document.page_count is None:
                number = 1
            elif page is not None:
                number = page
            else:
                number = fragment.page_number if fragment else 1
            found = await uow.documents.page(user.id, document_id, number)
        if found is None:
            raise not_found()
        quote = None
        if fragment is not None and fragment.page_number == number:
            quote = (fragment.start_offset, fragment.end_offset)
        return DocumentText(
            page=number if document.page_count is not None else None,
            page_count=document.page_count,
            recognized=found.recognized,
            segments=split_segments(found.text, quote),
        )

    async def file(self, user: CurrentUser, document_id: UUID) -> tuple[KbDocument, Path]:
        """Оригинал документа: доступен в любом состоянии (§8.2)."""
        document = (await self.get(user, document_id)).document
        return document, self._storage.path(document.storage_key)

    async def has_cogis_documentation(self) -> bool:
        """Есть ли в общей базе готовая документация CoGIS."""
        async with self._uow_factory() as uow:
            return await uow.documents.has_cogis_documentation()
