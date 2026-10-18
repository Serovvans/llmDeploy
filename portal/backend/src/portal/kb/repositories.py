"""Хранилище базы знаний на SQLAlchemy Core; условие видимости — в каждом запросе маршрутов."""

from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from portal.core.audit import SqlAuditLog
from portal.core.pagination import SortOrder
from portal.core.ports import AuditLog, Clock
from portal.files.ports import DOCX
from portal.kb.domain import (
    BULK_PRIORITY_FROM,
    DESCRIPTION_VERSION,
    INDEX_VERSION,
    DocumentStatus,
    DocumentView,
    ErrorCode,
    FoundFragment,
    Fragment,
    FragmentText,
    Job,
    KbDocument,
    KbImage,
    PageText,
    ReprocessStatus,
    Scope,
    SortField,
    TranscriptionJob,
)
from portal.kb.ports import KnowledgeSelection
from portal.kb.store import DocumentRepository, JobQueue, KbUnitOfWork
from portal.kb.tables import (
    kb_document_tags,
    kb_documents,
    kb_fragments,
    kb_images,
    kb_jobs,
    kb_pages,
    kb_queue_control,
)
from portal.kb.tags import Tag, selected_keys

type _Row = sa.Row[*tuple[Any, ...]]

# Таблица модуля входа: отсюда читается только ФИО автора для списков и отбора (§8.1).
_users = sa.table("users", sa.column("id"), sa.column("full_name"))
_AUTHOR = _users.c.full_name.label("author_full_name")
_DOCUMENT_FIELDS = tuple(KbDocument.__dataclass_fields__)
_WITH_AUTHOR = kb_documents.join(_users, _users.c.id == kb_documents.c.owner_id)
# Теги документа выбираются вместе с его строкой — значит, под тем же условием видимости.
_TAGS = (
    sa.select(
        sa.func.coalesce(
            sa.func.array_agg(
                postgresql.aggregate_order_by(kb_document_tags.c.tag, kb_document_tags.c.tag_key)
            ),
            sa.literal_column("'{}'::text[]"),
        )
    )
    .where(kb_document_tags.c.document_id == kb_documents.c.id)
    .scalar_subquery()
    .label("tags")
)
_REPROCESSING = (
    sa.exists()
    .where(
        kb_jobs.c.document_id == kb_documents.c.id,
        kb_jobs.c.kind == "reprocess",
        kb_jobs.c.priority < BULK_PRIORITY_FROM,
    )
    .label("reprocessing")
)
_VIEW_COLUMNS = (kb_documents, _AUTHOR, _TAGS, _REPROCESSING)


def _document(row: _Row) -> KbDocument:
    return KbDocument(**{name: getattr(row, name) for name in _DOCUMENT_FIELDS})


def _view(row: _Row) -> DocumentView:
    return DocumentView(_document(row), row.author_full_name, tuple(row.tags), row.reprocessing)


def _values(item: object, table: sa.Table, skip: str | None = None) -> dict[str, Any]:
    return {column.name: getattr(item, column.name) for column in table.c if column.name != skip}


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _visible(user_id: UUID) -> sa.ColumnElement[bool]:
    """Правило видимости §8.1: не удалён и (общий, «Министерства» или свой).

    Роль не участвует: у администратора нет исключений на чтение.
    """
    return sa.and_(
        kb_documents.c.deleted_at.is_(None),
        sa.or_(
            kb_documents.c.scope.in_(("shared", "ministries")),
            kb_documents.c.owner_id == user_id,
        ),
    )


def _in_collection(user_id: UUID, scope: Scope) -> sa.ColumnElement[bool]:
    """Документы одной коллекции; личная — только этого пользователя."""
    conditions = [kb_documents.c.deleted_at.is_(None), kb_documents.c.scope == scope]
    if scope == "personal":
        conditions.append(kb_documents.c.owner_id == user_id)
    return sa.and_(*conditions)


def _has_any_tag(keys: Sequence[str]) -> sa.ColumnElement[bool]:
    """У документа есть хотя бы один из тегов (по ключам)."""
    return sa.exists().where(
        kb_document_tags.c.document_id == kb_documents.c.id,
        kb_document_tags.c.tag_key.in_(keys),
    )


def _selected(selection: KnowledgeSelection) -> sa.ColumnElement[bool]:
    """Выбор клиента поверх правила видимости — те же условия, что в фильтре Qdrant (§10.3).

    Выбор только сужает: личная коллекция и здесь остаётся личной текущего пользователя,
    потому что условие стоит рядом с правилом видимости, а не вместо него.
    """
    if selection.cogis_only:
        return sa.and_(kb_documents.c.scope == "shared", kb_documents.c.is_cogis)
    conditions: list[sa.ColumnElement[bool]] = [
        kb_documents.c.scope.in_(sorted(selection.collections))
    ]
    if keys := selected_keys(selection.tags):
        conditions.append(_has_any_tag(keys))
    return sa.and_(*conditions)


_FRAGMENT_TEXT = sa.func.substr(
    kb_pages.c.text,
    kb_fragments.c.start_offset + 1,
    kb_fragments.c.end_offset - kb_fragments.c.start_offset,
).label("text")
_FRAGMENTS_WITH_PAGES = kb_fragments.join(
    kb_pages,
    sa.and_(
        kb_pages.c.document_id == kb_fragments.c.document_id,
        kb_pages.c.number == kb_fragments.c.page_number,
    ),
)
# Текст найденного (§8.6, §9.13): у фрагмента-картинки — его часть описания; у записанного
# до 0.16 (`end_offset = 0`) — описание целиком.
_FOUND_TEXT = sa.case(
    (kb_fragments.c.image_id.is_(None), _FRAGMENT_TEXT),
    (kb_fragments.c.end_offset == 0, kb_images.c.description),
    else_=sa.func.substr(
        kb_images.c.description,
        kb_fragments.c.start_offset + 1,
        kb_fragments.c.end_offset - kb_fragments.c.start_offset,
    ),
).label("text")
# Документы, из которых могли выпасть картинки: переобработка `--images` (§11.6).
_WITH_IMAGES = ("application/pdf", DOCX, "image/jpeg", "image/png")
_PAGE_COLUMNS = (
    kb_pages.c.number,
    kb_pages.c.text,
    kb_pages.c.recognized,
    kb_pages.c.start_ms,
    kb_pages.c.marks,
)
_PAGES_OF_DOCUMENTS = kb_pages.join(kb_documents, kb_documents.c.id == kb_pages.c.document_id)
_RECORDING = sa.or_(
    kb_documents.c.media_type.like("audio/%"), kb_documents.c.media_type.like("video/%")
)


def _found(user_id: UUID, selection: KnowledgeSelection) -> sa.Select[tuple[Any, ...]]:
    """Фрагменты готовых документов, доступных пользователю в его выборе (§8.4, шаг 4)."""
    return (
        sa.select(
            kb_fragments,
            kb_documents.c.title,
            kb_documents.c.scope,
            kb_documents.c.page_count,
            _FOUND_TEXT,
        )
        .select_from(
            _FRAGMENTS_WITH_PAGES.join(
                kb_documents, kb_documents.c.id == kb_fragments.c.document_id
            ).outerjoin(kb_images, kb_images.c.id == kb_fragments.c.image_id)
        )
        .where(_visible(user_id), _selected(selection), kb_documents.c.status == "ready")
    )


def _found_fragment(row: _Row) -> FoundFragment:
    return FoundFragment(
        fragment_id=row.id,
        document_id=row.document_id,
        document_title=row.title,
        scope=row.scope,
        page=row.page_number if row.page_count is not None else None,
        text=row.text,
        ordinal=row.ordinal,
        page_number=row.page_number,
        start_offset=row.start_offset,
        end_offset=row.end_offset,
        section=row.section,
        context=row.context,
        image_id=row.image_id,
        start_ms=row.start_ms,
    )


def _fragment_texts(document_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Текстовые фрагменты документа по порядку; фрагменты-картинки сюда не входят."""
    return (
        sa.select(kb_fragments.c.id, kb_fragments.c.ordinal, kb_fragments.c.section, _FRAGMENT_TEXT)
        .select_from(_FRAGMENTS_WITH_PAGES)
        .where(kb_fragments.c.document_id == document_id, kb_fragments.c.image_id.is_(None))
        .order_by(kb_fragments.c.ordinal)
    )


class SqlDocumentRepository:
    """Таблицы `kb_documents`, `kb_document_tags`, `kb_pages`, `kb_fragments`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def add(self, document: KbDocument) -> bool:
        """Создать документ; `False` — сработал уникальный индекс повтора файла."""
        result = await self._connection.execute(
            postgresql.insert(kb_documents)
            .values(**_values(document, kb_documents))
            .on_conflict_do_nothing()
        )
        return result.rowcount > 0

    async def find_duplicate(
        self, user_id: UUID, scope: Scope, sha256: bytes
    ) -> DocumentView | None:
        """Документ с тем же содержимым в этой же коллекции (личная — своя)."""
        query = (
            sa.select(*_VIEW_COLUMNS)
            .select_from(_WITH_AUTHOR)
            .where(_in_collection(user_id, scope), kb_documents.c.sha256 == sha256)
        )
        row = (await self._connection.execute(query)).first()
        return _view(row) if row else None

    async def get(
        self, user_id: UUID, document_id: UUID, *, lock: bool = False
    ) -> DocumentView | None:
        """Документ, видимый пользователю; `lock` — `SELECT … FOR UPDATE` строки документа."""
        query = (
            sa.select(*_VIEW_COLUMNS)
            .select_from(_WITH_AUTHOR)
            .where(kb_documents.c.id == document_id, _visible(user_id))
        )
        if lock:
            query = query.with_for_update(of=kb_documents)
        row = (await self._connection.execute(query)).first()
        return _view(row) if row else None

    async def list_documents(
        self,
        user_id: UUID,
        scope: Scope,
        *,
        q: str | None,
        tags: Sequence[str],
        sort: SortField,
        order: SortOrder,
        offset: int,
        limit: int,
    ) -> tuple[list[DocumentView], int]:
        """Страница документов коллекции; отбор — по названию, ФИО автора и ключам тегов."""
        condition = _in_collection(user_id, scope)
        if tags:
            condition = sa.and_(condition, _has_any_tag(tags))
        if q:
            pattern = _like_pattern(q)
            condition = sa.and_(
                condition,
                sa.or_(
                    kb_documents.c.title.ilike(pattern, escape="\\"),
                    _users.c.full_name.ilike(pattern, escape="\\"),
                ),
            )
        total = await self._connection.scalar(
            sa.select(sa.func.count()).select_from(_WITH_AUTHOR).where(condition)
        )
        direction = sa.asc if order == "asc" else sa.desc
        key = sa.func.lower(kb_documents.c.title) if sort == "title" else kb_documents.c.created_at
        rows = await self._connection.execute(
            sa.select(*_VIEW_COLUMNS)
            .select_from(_WITH_AUTHOR)
            .where(condition)
            .order_by(direction(key), kb_documents.c.id)
            .offset(offset)
            .limit(limit)
        )
        return [_view(row) for row in rows], total or 0

    async def mark_deleted(self, user_id: UUID, document_id: UUID, now: datetime) -> None:
        """Пометить документ удалённым."""
        await self._connection.execute(
            sa.update(kb_documents)
            .where(kb_documents.c.id == document_id, _visible(user_id))
            .values(deleted_at=now)
        )

    async def set_tags(self, document_id: UUID, tags: Sequence[Tag]) -> None:
        """Заменить теги документа целиком."""
        await self._connection.execute(
            sa.delete(kb_document_tags).where(kb_document_tags.c.document_id == document_id)
        )
        if tags:
            await self._connection.execute(
                sa.insert(kb_document_tags),
                [{"document_id": document_id, "tag_key": tag.key, "tag": tag.text} for tag in tags],
            )

    async def suggest_tags(
        self, user_id: UUID, scopes: Collection[Scope], q: str, limit: int
    ) -> list[str]:
        """Теги документов, видимых пользователю: одно написание на ключ, по алфавиту ключей."""
        query = (
            sa.select(sa.func.min(kb_document_tags.c.tag))
            .select_from(
                kb_document_tags.join(
                    kb_documents, kb_documents.c.id == kb_document_tags.c.document_id
                )
            )
            .where(_visible(user_id))
            .group_by(kb_document_tags.c.tag_key)
            .order_by(kb_document_tags.c.tag_key)
            .limit(limit)
        )
        if scopes:
            query = query.where(kb_documents.c.scope.in_(sorted(scopes)))
        if q:
            query = query.where(kb_document_tags.c.tag_key.like(_like_pattern(q), escape="\\"))
        return list((await self._connection.scalars(query)).all())

    async def page(self, user_id: UUID, document_id: UUID, number: int) -> PageText | None:
        """Текст страницы видимого документа."""
        query = (
            sa.select(*_PAGE_COLUMNS)
            .select_from(_PAGES_OF_DOCUMENTS)
            .where(
                kb_pages.c.document_id == document_id,
                kb_pages.c.number == number,
                _visible(user_id),
            )
        )
        row = (await self._connection.execute(query)).first()
        return PageText(**row._asdict()) if row else None

    async def page_count(self, user_id: UUID, document_id: UUID) -> int:
        """Сколько страниц сохранено у видимого документа."""
        query = (
            sa.select(sa.func.count())
            .select_from(_PAGES_OF_DOCUMENTS)
            .where(kb_pages.c.document_id == document_id, _visible(user_id))
        )
        return await self._connection.scalar(query) or 0

    async def page_at(self, user_id: UUID, document_id: UUID, timecode_seconds: int) -> int | None:
        """Номер части видимой записи с наибольшим временем начала не позже момента."""
        query = (
            sa.select(kb_pages.c.number)
            .select_from(_PAGES_OF_DOCUMENTS)
            .where(
                kb_pages.c.document_id == document_id,
                _visible(user_id),
                # Сравнение в целых секундах (§8.3); деление целых в PostgreSQL — нацело.
                kb_pages.c.start_ms / 1000 <= timecode_seconds,
            )
            .order_by(kb_pages.c.start_ms.desc())
            .limit(1)
        )
        number: int | None = await self._connection.scalar(query)
        return number

    async def image(self, user_id: UUID, document_id: UUID, image_id: UUID) -> KbImage | None:
        """Картинка видимого документа; условие видимости — в самом запросе (§8.6)."""
        query = (
            sa.select(kb_images)
            .select_from(kb_images.join(kb_documents, kb_documents.c.id == kb_images.c.document_id))
            .where(
                kb_images.c.id == image_id,
                kb_images.c.document_id == document_id,
                _visible(user_id),
            )
        )
        row = (await self._connection.execute(query)).first()
        return KbImage(**row._asdict()) if row else None

    async def fragment(
        self, user_id: UUID, document_id: UUID, fragment_id: UUID
    ) -> Fragment | None:
        """Фрагмент видимого документа."""
        query = (
            sa.select(kb_fragments)
            .select_from(
                kb_fragments.join(kb_documents, kb_documents.c.id == kb_fragments.c.document_id)
            )
            .where(
                kb_fragments.c.id == fragment_id,
                kb_fragments.c.document_id == document_id,
                _visible(user_id),
            )
        )
        row = (await self._connection.execute(query)).first()
        return Fragment(**row._asdict()) if row else None

    async def found_fragments(
        self, user_id: UUID, selection: KnowledgeSelection, fragment_ids: Sequence[UUID]
    ) -> list[FoundFragment]:
        """Тексты найденных фрагментов готовых документов, доступных пользователю в выборе."""
        if not fragment_ids:
            return []
        query = _found(user_id, selection).where(kb_fragments.c.id.in_(fragment_ids))
        return [_found_fragment(row) for row in await self._connection.execute(query)]

    async def neighbor_fragments(
        self, user_id: UUID, selection: KnowledgeSelection, places: Sequence[tuple[UUID, int]]
    ) -> list[FoundFragment]:
        """Текстовые фрагменты по местам (документ, порядковый номер) — под той же проверкой."""
        if not places:
            return []
        query = _found(user_id, selection).where(
            sa.tuple_(kb_fragments.c.document_id, kb_fragments.c.ordinal).in_(places),
            kb_fragments.c.image_id.is_(None),
        )
        return [_found_fragment(row) for row in await self._connection.execute(query)]

    async def has_cogis_documentation(self) -> bool:
        """Есть ли в общей базе готовый документ с отметкой «документация CoGIS»."""
        query = sa.select(
            sa.exists().where(
                kb_documents.c.deleted_at.is_(None),
                kb_documents.c.scope == "shared",
                kb_documents.c.is_cogis,
                kb_documents.c.status == "ready",
            )
        )
        return bool(await self._connection.scalar(query))

    async def lock(self, document_id: UUID, *, include_deleted: bool = False) -> KbDocument | None:
        """Документ под блокировкой строки (`SELECT … FOR UPDATE`)."""
        query = sa.select(kb_documents).where(kb_documents.c.id == document_id).with_for_update()
        if not include_deleted:
            query = query.where(kb_documents.c.deleted_at.is_(None))
        row = (await self._connection.execute(query)).first()
        return _document(row) if row else None

    async def set_status(
        self, document_id: UUID, status: DocumentStatus, error_code: ErrorCode | None = None
    ) -> None:
        """Сменить состояние документа."""
        await self._connection.execute(
            sa.update(kb_documents)
            .where(kb_documents.c.id == document_id)
            .values(status=status, error_code=error_code)
        )

    async def requeue_abandoned(self, now: datetime) -> int:
        """Вернуть в `queued` документы `processing`, чьё задание никто не держит в аренде."""
        unleased = sa.exists().where(
            kb_jobs.c.document_id == kb_documents.c.id,
            kb_jobs.c.kind == "index",
            _unleased(now),
        )
        result = await self._connection.execute(
            sa.update(kb_documents)
            .where(kb_documents.c.status == "processing", unleased)
            .values(status="queued")
        )
        return result.rowcount

    async def set_recognizing(self, document_id: UUID) -> None:
        """Отметить, что среди страниц есть сканы."""
        await self._connection.execute(
            sa.update(kb_documents).where(kb_documents.c.id == document_id).values(recognizing=True)
        )

    async def tag_keys(self, document_id: UUID) -> list[str]:
        """Ключи тегов документа — поле `tags` его точек в Qdrant."""
        rows = await self._connection.scalars(
            sa.select(kb_document_tags.c.tag_key)
            .where(kb_document_tags.c.document_id == document_id)
            .order_by(kb_document_tags.c.tag_key)
        )
        return list(rows.all())

    async def stored_pages(self, document_id: UUID) -> dict[int, int]:
        """Уже прочитанные страницы: номер → длина текста в символах."""
        rows = await self._connection.execute(
            sa.select(kb_pages.c.number, sa.func.char_length(kb_pages.c.text)).where(
                kb_pages.c.document_id == document_id
            )
        )
        return {number: length for number, length in rows}

    async def add_page(self, document_id: UUID, page: PageText) -> None:
        """Сохранить страницу и увеличить счётчик хода; повтор той же страницы — без изменений."""
        inserted = await self._connection.execute(
            postgresql.insert(kb_pages)
            .values(document_id=document_id, **_values(page, kb_pages, skip="document_id"))
            .on_conflict_do_nothing()
        )
        if inserted.rowcount:
            await self._connection.execute(
                sa.update(kb_documents)
                .where(kb_documents.c.id == document_id)
                .values(pages_done=kb_documents.c.pages_done + 1)
            )

    async def pages(self, document_id: UUID) -> list[PageText]:
        """Все страницы документа по порядку."""
        rows = await self._connection.execute(
            sa.select(*_PAGE_COLUMNS)
            .where(kb_pages.c.document_id == document_id)
            .order_by(kb_pages.c.number)
        )
        return [PageText(**row._asdict()) for row in rows]

    async def _update(self, document_id: UUID, **values: Any) -> None:
        await self._connection.execute(
            sa.update(kb_documents).where(kb_documents.c.id == document_id).values(**values)
        )

    async def set_duration(self, document_id: UUID, seconds: int) -> None:
        """Записать измеренную длительность записи."""
        await self._update(document_id, duration_seconds=seconds)

    async def set_seconds_done(self, document_id: UUID, seconds: int) -> None:
        """Сколько секунд записи уже расшифровано."""
        await self._update(document_id, seconds_done=seconds)

    async def set_audio_key(self, document_id: UUID, key: str) -> None:
        """Ключ сжатого звука записи."""
        await self._update(document_id, audio_key=key)

    async def clear_source(self, document_id: UUID, error_code: ErrorCode | None = None) -> None:
        """Отметить, что исходный файл записи удалён; `error_code` — сменить причину ошибки."""
        values: dict[str, Any] = {"storage_key": None}
        if error_code is not None:
            values["error_code"] = error_code
        await self._update(document_id, **values)

    async def recordings_to_clean(self, expired_before: datetime) -> list[UUID]:
        """Записи с исходником: готовые и с ошибкой, загруженные раньше `expired_before`."""
        rows = await self._connection.scalars(
            sa.select(kb_documents.c.id).where(
                _RECORDING,
                kb_documents.c.deleted_at.is_(None),
                kb_documents.c.storage_key.is_not(None),
                sa.or_(
                    kb_documents.c.status == "ready",
                    sa.and_(
                        kb_documents.c.status == "error",
                        kb_documents.c.created_at < expired_before,
                    ),
                ),
            )
        )
        return list(rows.all())

    async def recording_keys(self) -> set[str]:
        """Ключи хранения всех записей: исходники и сжатый звук, включая удаляемые."""
        rows = await self._connection.execute(
            sa.select(kb_documents.c.storage_key, kb_documents.c.audio_key).where(_RECORDING)
        )
        return {key for row in rows for key in row if key is not None}

    async def images(self, document_id: UUID) -> list[KbImage]:
        """Картинки документа по порядку."""
        rows = await self._connection.execute(
            sa.select(kb_images)
            .where(kb_images.c.document_id == document_id)
            .order_by(kb_images.c.ordinal)
        )
        return [KbImage(**row._asdict()) for row in rows]

    async def add_image(self, image: KbImage) -> None:
        """Сохранить строку картинки."""
        await self._connection.execute(sa.insert(kb_images).values(**_values(image, kb_images)))

    async def describe_image(self, image_id: UUID, description: str, *, pending: bool) -> None:
        """Записать описание текущей версии: в `pending_description` либо в `description`."""
        values: dict[str, Any] = (
            {"pending_description": description}
            if pending
            else {"description": description, "description_version": DESCRIPTION_VERSION}
        )
        await self._connection.execute(
            sa.update(kb_images).where(kb_images.c.id == image_id).values(**values)
        )

    async def finish_images(self, document_id: UUID, image_ids: Sequence[UUID]) -> None:
        """Порядок картинок нового индекса и перенос нового описания (§10.6, шаг 2.1)."""
        if not image_ids:
            return
        await self._connection.execute(
            sa.update(kb_images)
            .where(kb_images.c.id == sa.bindparam("image_id"))
            .values(ordinal=sa.bindparam("new_ordinal")),
            [
                {"image_id": image_id, "new_ordinal": ordinal}
                for ordinal, image_id in enumerate(image_ids)
            ],
        )
        await self._connection.execute(
            sa.update(kb_images)
            .where(
                kb_images.c.document_id == document_id,
                kb_images.c.id.in_(image_ids),
                kb_images.c.pending_description.is_not(None),
            )
            .values(
                description=kb_images.c.pending_description,
                description_version=DESCRIPTION_VERSION,
                pending_description=None,
            )
        )

    async def set_images_truncated(self, document_id: UUID, truncated: bool) -> None:
        """Отметить, что картинок в документе больше лимита."""
        await self._update(document_id, images_truncated=truncated)

    async def drop_unreferenced_images(self, document_id: UUID) -> list[str]:
        """Удалить картинки, на которые не ссылается ни один фрагмент; вернуть ключи их файлов."""
        referenced = sa.exists().where(kb_fragments.c.image_id == kb_images.c.id)
        rows = await self._connection.scalars(
            sa.delete(kb_images)
            .where(kb_images.c.document_id == document_id, ~referenced)
            .returning(kb_images.c.storage_key)
        )
        return list(rows.all())

    async def replace_fragments(self, document_id: UUID, fragments: Sequence[Fragment]) -> None:
        """Удалить прежние фрагменты документа и записать новые."""
        await self._connection.execute(
            sa.delete(kb_fragments).where(kb_fragments.c.document_id == document_id)
        )
        if fragments:
            await self._connection.execute(
                sa.insert(kb_fragments),
                [_values(fragment, kb_fragments) for fragment in fragments],
            )

    async def fragment_ids(self, document_id: UUID) -> list[UUID]:
        """Идентификаторы текущих фрагментов документа — они же его действующие точки."""
        rows = await self._connection.scalars(
            sa.select(kb_fragments.c.id).where(kb_fragments.c.document_id == document_id)
        )
        return list(rows.all())

    async def mark_indexed(self, document_id: UUID) -> None:
        """Индекс построен текущим порядком обработки; отметка о сбое переобработки снята."""
        await self._update(document_id, index_version=INDEX_VERSION, reprocess_error=None)

    async def set_reprocess_error(self, document_id: UUID, code: ErrorCode | None) -> None:
        """Поставить или снять отметку о сбое переобработки (§11.6)."""
        await self._update(document_id, reprocess_error=code)

    async def fragments_without_context(self, document_id: UUID, limit: int) -> list[FragmentText]:
        """Первые по порядку текстовые фрагменты документа, к которым пояснение не дописано."""
        rows = await self._connection.execute(
            _fragment_texts(document_id).where(kb_fragments.c.context.is_(None)).limit(limit)
        )
        return [FragmentText(**row._asdict()) for row in rows]

    async def fragment_texts(self, document_id: UUID, first: int, last: int) -> list[FragmentText]:
        """Текстовые фрагменты документа с порядковыми номерами от `first` до `last`."""
        rows = await self._connection.execute(
            _fragment_texts(document_id).where(kb_fragments.c.ordinal.between(first, last))
        )
        return [FragmentText(**row._asdict()) for row in rows]

    async def set_contexts(self, document_id: UUID, contexts: Mapping[UUID, str]) -> set[UUID]:
        """Записать пояснения; результат — фрагменты, которые ещё существуют."""
        kept: set[UUID] = set()
        for fragment_id, context in contexts.items():
            updated = await self._connection.execute(
                sa.update(kb_fragments)
                .where(kb_fragments.c.id == fragment_id, kb_fragments.c.document_id == document_id)
                .values(context=context)
            )
            if updated.rowcount:
                kept.add(fragment_id)
        return kept

    async def contexts_by_text(self, document_id: UUID) -> dict[tuple[str | None, str], str]:
        """Пояснения текстовых фрагментов по (раздел, текст); первый по порядку — главный."""
        rows = await self._connection.execute(
            _fragment_texts(document_id)
            .add_columns(kb_fragments.c.context)
            .where(kb_fragments.c.context.is_not(None))
        )
        contexts: dict[tuple[str | None, str], str] = {}
        for row in rows:
            contexts.setdefault((row.section, row.text), row.context)
        return contexts

    async def delete(self, document_id: UUID) -> None:
        """Стереть строку документа; страницы, фрагменты и задания уходят каскадом."""
        await self._connection.execute(
            sa.delete(kb_documents).where(kb_documents.c.id == document_id)
        )

    async def ids_for_reindex(self, *, only_errors: bool) -> list[UUID]:
        """Неудалённые документы по времени добавления; `only_errors` — только с ошибкой."""
        # Запись без исходника и без сжатого звука обработать нечем (§8.7, §13.4).
        unprocessable = sa.and_(
            _RECORDING, kb_documents.c.storage_key.is_(None), kb_documents.c.audio_key.is_(None)
        )
        query = sa.select(kb_documents.c.id).where(
            kb_documents.c.deleted_at.is_(None), ~unprocessable
        )
        if only_errors:
            query = query.where(kb_documents.c.status == "error")
        rows = await self._connection.execute(query.order_by(kb_documents.c.created_at))
        return [row.id for row in rows]

    async def ids_for_reprocess(self, *, all_ready: bool, images: bool = False) -> list[UUID]:
        """Готовые неудалённые документы для массовой переобработки, по времени добавления."""
        query = sa.select(kb_documents.c.id).where(
            kb_documents.c.deleted_at.is_(None), kb_documents.c.status == "ready"
        )
        if images:
            query = query.where(kb_documents.c.media_type.in_(_WITH_IMAGES))
        elif not all_ready:
            query = query.where(
                sa.or_(
                    kb_documents.c.index_version < INDEX_VERSION,
                    kb_documents.c.reprocess_error.is_not(None),
                )
            )
        rows = await self._connection.scalars(query.order_by(kb_documents.c.created_at))
        return list(rows.all())

    async def reprocess_status(self) -> ReprocessStatus:
        """Числа хода переобработки; названий и содержимого документов в них нет."""
        live = kb_documents.c.deleted_at.is_(None)
        current = await self._connection.scalar(
            sa.select(sa.func.count()).where(live, kb_documents.c.index_version == INDEX_VERSION)
        )
        jobs = await self._connection.execute(
            sa.select(kb_jobs.c.kind, sa.func.count())
            .where(kb_jobs.c.kind.in_(("reprocess", "context")))
            .group_by(kb_jobs.c.kind)
        )
        waiting = {kind: count for kind, count in jobs}
        errors = await self._connection.execute(
            sa.select(kb_documents.c.reprocess_error, sa.func.count())
            .where(live, kb_documents.c.reprocess_error.is_not(None))
            .group_by(kb_documents.c.reprocess_error)
            .order_by(kb_documents.c.reprocess_error)
        )
        paused = await self._connection.scalar(sa.select(kb_queue_control.c.bulk_paused))
        old_descriptions = await self._connection.scalar(
            sa.select(sa.func.count())
            .select_from(kb_images.join(kb_documents, kb_documents.c.id == kb_images.c.document_id))
            .where(
                live,
                kb_images.c.description_version < DESCRIPTION_VERSION,
                kb_images.c.description != "",
            )
        )
        return ReprocessStatus(
            current=current or 0,
            reprocess_jobs=waiting.get("reprocess", 0),
            context_jobs=waiting.get("context", 0),
            errors={code: count for code, count in errors},
            paused=bool(paused),
            old_descriptions=old_descriptions or 0,
        )


def _unleased(now: datetime) -> sa.ColumnElement[bool]:
    """Задание никто не держит в аренде: не взято либо аренда истекла (§11.1)."""
    return sa.or_(kb_jobs.c.locked_until.is_(None), kb_jobs.c.locked_until <= now)


def _main_slot(busy_documents: Collection[UUID]) -> list[sa.ColumnElement[bool]]:
    """Какие задания берёт основное место (§11.1), кроме срока и аренды.

    Не `transcribe`; при паузе — не массовая переобработка; `index` и `reprocess`
    документа из `busy_documents` ждут.
    """
    paused = sa.exists().where(kb_queue_control.c.bulk_paused)
    conditions = [
        kb_jobs.c.kind != "transcribe",
        (kb_jobs.c.priority < BULK_PRIORITY_FROM) | ~paused,
    ]
    if busy_documents:
        conditions.append(
            ~(
                kb_jobs.c.kind.in_(("index", "reprocess"))
                & kb_jobs.c.document_id.in_(busy_documents)
            )
        )
    return conditions


class SqlJobQueue:
    """Таблица `kb_jobs`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def add(self, job: Job) -> None:
        """Поставить задание."""
        await self._connection.execute(sa.insert(kb_jobs).values(**_values(job, kb_jobs)))

    async def reset_or_add(self, job: Job) -> None:
        """Поставить задание; у существующего обнулить попытки, аренду не трогать (§13.4)."""
        await self._connection.execute(
            postgresql.insert(kb_jobs)
            .values(**_values(job, kb_jobs))
            .on_conflict_do_update(
                constraint="kb_jobs_document_kind_key",
                set_={"attempts": 0, "run_after": job.run_after},
            )
        )

    async def add_if_absent(self, job: Job) -> bool:
        """Поставить задание; если такое у документа уже есть, оно остаётся как есть."""
        inserted = await self._connection.execute(
            postgresql.insert(kb_jobs).values(**_values(job, kb_jobs)).on_conflict_do_nothing()
        )
        return inserted.rowcount > 0

    async def add_or_prioritize(self, job: Job) -> None:
        """Поставить задание; у существующего поднять приоритет до приоритета нового (§11.6)."""
        await self._connection.execute(
            postgresql.insert(kb_jobs)
            .values(**_values(job, kb_jobs))
            .on_conflict_do_update(
                constraint="kb_jobs_document_kind_key",
                set_={"priority": sa.func.least(kb_jobs.c.priority, job.priority)},
            )
        )

    async def remove(self, document_id: UUID, kind: str) -> None:
        """Снять задание документа."""
        await self._connection.execute(
            sa.delete(kb_jobs).where(kb_jobs.c.document_id == document_id, kb_jobs.c.kind == kind)
        )

    async def _claim(
        self, now: datetime, lease_until: datetime, *conditions: sa.ColumnElement[bool]
    ) -> _Row | None:
        """Взять одно задание: `SELECT … FOR UPDATE SKIP LOCKED` и аренда в той же транзакции."""
        candidate = (
            sa.select(kb_jobs.c.id)
            .where(kb_jobs.c.run_after <= now, _unleased(now), *conditions)
            .order_by(kb_jobs.c.priority, kb_jobs.c.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        claimed = await self._connection.execute(
            sa.update(kb_jobs)
            .where(kb_jobs.c.id == candidate.scalar_subquery())
            .values(attempts=kb_jobs.c.attempts + 1, locked_until=lease_until)
            .returning(kb_jobs)
        )
        row: _Row | None = claimed.first()
        return row

    async def claim(
        self, now: datetime, lease_until: datetime, busy_documents: Collection[UUID] = ()
    ) -> Job | None:
        """Взять одно задание документа — основное место воркера (§11.1).

        Задания `transcribe` не берутся; при паузе — и задания массовой переобработки;
        `index` и `reprocess` документа из `busy_documents` ждут.
        """
        row = await self._claim(now, lease_until, *_main_slot(busy_documents))
        return Job(**row._asdict()) if row else None

    async def bulk_paused(self) -> bool:
        """Приостановлена ли массовая переобработка."""
        return bool(await self._connection.scalar(sa.select(kb_queue_control.c.bulk_paused)))

    async def has_waiting(
        self, now: datetime, busy_documents: Collection[UUID], up_to_priority: int
    ) -> bool:
        """Ждёт ли задание, которое основное место взяло бы сейчас, не ниже приоритетом."""
        query = sa.select(
            sa.exists().where(
                kb_jobs.c.run_after <= now,
                _unleased(now),
                kb_jobs.c.priority <= up_to_priority,
                *_main_slot(busy_documents),
            )
        )
        return bool(await self._connection.scalar(query))

    async def yield_turn(self, job_id: UUID, requeued_at: datetime | None) -> bool:
        """Снять аренду уступившего задания; попытки — с нуля, `run_after` прежний (§11.1)."""
        values: dict[str, Any] = {"locked_until": None, "attempts": 0}
        if requeued_at is not None:
            values["created_at"] = requeued_at
        result = await self._connection.execute(
            sa.update(kb_jobs).where(kb_jobs.c.id == job_id).values(**values)
        )
        return result.rowcount > 0

    async def claim_transcription(
        self, now: datetime, lease_until: datetime
    ) -> TranscriptionJob | None:
        """Взять одно задание `transcribe` — место под вложения чата (§11.1)."""
        row = await self._claim(now, lease_until, kb_jobs.c.kind == "transcribe")
        return TranscriptionJob(row.id, row.attachment_id, row.attempts) if row else None

    async def unleased_transcriptions(self, now: datetime) -> list[UUID]:
        """Вложения, чьи задания `transcribe` никто не держит в аренде."""
        rows = await self._connection.scalars(
            sa.select(kb_jobs.c.attachment_id).where(kb_jobs.c.kind == "transcribe", _unleased(now))
        )
        return list(rows.all())

    async def exists(self, job_id: UUID) -> bool:
        """Есть ли ещё задание."""
        return bool(
            await self._connection.scalar(sa.select(sa.exists().where(kb_jobs.c.id == job_id)))
        )

    async def renew(self, job_id: UUID, lease_until: datetime) -> bool:
        """Продлить аренду; `False` — задания больше нет."""
        result = await self._connection.execute(
            sa.update(kb_jobs).where(kb_jobs.c.id == job_id).values(locked_until=lease_until)
        )
        return result.rowcount > 0

    async def postpone(self, job_id: UUID, run_after: datetime) -> None:
        """Снять аренду и отложить задание до повтора."""
        await self._connection.execute(
            sa.update(kb_jobs)
            .where(kb_jobs.c.id == job_id)
            .values(locked_until=None, run_after=run_after)
        )

    async def release(self, job_id: UUID) -> bool:
        """Вернуть взятое задание в очередь, не считая попытку; `False` — задания уже нет."""
        result = await self._connection.execute(
            sa.update(kb_jobs)
            .where(kb_jobs.c.id == job_id)
            .values(locked_until=None, attempts=sa.func.greatest(kb_jobs.c.attempts - 1, 0))
        )
        return result.rowcount > 0

    async def requeue(self, job_id: UUID) -> None:
        """Вернуть задание в очередь после удачной порции: счёт попыток — с нуля (§11.5)."""
        await self._connection.execute(
            sa.update(kb_jobs).where(kb_jobs.c.id == job_id).values(locked_until=None, attempts=0)
        )

    async def finish(self, job_id: UUID) -> None:
        """Удалить выполненное задание."""
        await self._connection.execute(sa.delete(kb_jobs).where(kb_jobs.c.id == job_id))

    async def has_leased(self, now: datetime) -> bool:
        """Есть ли задания в аренде."""
        query = sa.select(sa.exists().where(kb_jobs.c.locked_until > now))
        return bool(await self._connection.scalar(query))

    async def set_bulk_paused(self, paused: bool) -> None:
        """Приостановить или возобновить массовую переобработку (§11.6)."""
        await self._connection.execute(sa.update(kb_queue_control).values(bulk_paused=paused))


class SqlKbUnitOfWork:
    """Хранилище на одном соединении."""

    def __init__(self, connection: AsyncConnection, clock: Clock) -> None:
        """Собрать хранилище вокруг соединения."""
        self._connection = connection
        self.documents: DocumentRepository = SqlDocumentRepository(connection)
        self.jobs: JobQueue = SqlJobQueue(connection)
        self.audit: AuditLog = SqlAuditLog(connection, clock)

    async def commit(self) -> None:
        """Зафиксировать сделанное."""
        await self._connection.commit()


class SqlKbUnitOfWorkFactory:
    """Открывает единицу работы на соединении из пула."""

    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        """Запомнить движок и часы журнала аудита."""
        self._engine = engine
        self._clock = clock

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[KbUnitOfWork]:
        """Фиксация при выходе без ошибки; при ошибке незафиксированное откатывается."""
        async with self._engine.connect() as connection:
            yield SqlKbUnitOfWork(connection, self._clock)
            await connection.commit()
