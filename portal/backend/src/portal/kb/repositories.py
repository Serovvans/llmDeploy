"""Хранилище базы знаний на SQLAlchemy Core; условие видимости — в каждом запросе маршрутов."""

from collections.abc import AsyncIterator, Sequence
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
from portal.kb.domain import (
    DocumentStatus,
    DocumentView,
    ErrorCode,
    FoundFragment,
    Fragment,
    Job,
    KbDocument,
    PageText,
    Scope,
    SortField,
)
from portal.kb.ports import KnowledgeScope
from portal.kb.store import DocumentRepository, JobQueue, KbUnitOfWork
from portal.kb.tables import kb_documents, kb_fragments, kb_jobs, kb_pages

type _Row = sa.Row[*tuple[Any, ...]]

# Таблица модуля входа: отсюда читается только ФИО автора для списков и отбора (§8.1).
_users = sa.table("users", sa.column("id"), sa.column("full_name"))
_AUTHOR = _users.c.full_name.label("author_full_name")
_DOCUMENT_FIELDS = tuple(KbDocument.__dataclass_fields__)
_WITH_AUTHOR = kb_documents.join(_users, _users.c.id == kb_documents.c.owner_id)


def _document(row: _Row) -> KbDocument:
    return KbDocument(**{name: getattr(row, name) for name in _DOCUMENT_FIELDS})


def _view(row: _Row) -> DocumentView:
    return DocumentView(_document(row), row.author_full_name)


def _values(item: object, table: sa.Table) -> dict[str, Any]:
    return {column.name: getattr(item, column.name) for column in table.c}


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _visible(user_id: UUID) -> sa.ColumnElement[bool]:
    """Правило видимости §8.1: не удалён и (общий или свой). Роль не участвует."""
    return sa.and_(
        kb_documents.c.deleted_at.is_(None),
        sa.or_(kb_documents.c.scope == "shared", kb_documents.c.owner_id == user_id),
    )


def _in_collection(user_id: UUID, scope: Scope) -> sa.ColumnElement[bool]:
    """Документы одной коллекции: общей либо личной этого пользователя."""
    if scope == "shared":
        return sa.and_(kb_documents.c.deleted_at.is_(None), kb_documents.c.scope == "shared")
    return sa.and_(
        kb_documents.c.deleted_at.is_(None),
        kb_documents.c.scope == "personal",
        kb_documents.c.owner_id == user_id,
    )


def _in_knowledge_scope(scope: KnowledgeScope) -> sa.ColumnElement[bool]:
    """Область поиска поверх правила видимости — те же три варианта, что в §10.3."""
    if scope == "shared":
        return kb_documents.c.scope == "shared"
    if scope == "cogis":
        return sa.and_(kb_documents.c.scope == "shared", kb_documents.c.is_cogis)
    return sa.true()


class SqlDocumentRepository:
    """Таблицы `kb_documents`, `kb_pages`, `kb_fragments`."""

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
        """Документ с тем же содержимым в общей базе или в личной базе пользователя."""
        query = (
            sa.select(kb_documents, _AUTHOR)
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
            sa.select(kb_documents, _AUTHOR)
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
        sort: SortField,
        order: SortOrder,
        offset: int,
        limit: int,
    ) -> tuple[list[DocumentView], int]:
        """Страница документов коллекции; отбор — по названию и ФИО автора."""
        condition = _in_collection(user_id, scope)
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
            sa.select(kb_documents, _AUTHOR)
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

    async def page(self, user_id: UUID, document_id: UUID, number: int) -> PageText | None:
        """Текст страницы видимого документа."""
        query = (
            sa.select(kb_pages.c.number, kb_pages.c.text, kb_pages.c.recognized)
            .select_from(kb_pages.join(kb_documents, kb_documents.c.id == kb_pages.c.document_id))
            .where(
                kb_pages.c.document_id == document_id,
                kb_pages.c.number == number,
                _visible(user_id),
            )
        )
        row = (await self._connection.execute(query)).first()
        return PageText(row.number, row.text, row.recognized) if row else None

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
        self, user_id: UUID, scope: KnowledgeScope, fragment_ids: Sequence[UUID]
    ) -> list[FoundFragment]:
        """Тексты найденных фрагментов готовых документов, доступных пользователю в области."""
        if not fragment_ids:
            return []
        length = kb_fragments.c.end_offset - kb_fragments.c.start_offset
        query = (
            sa.select(
                kb_fragments.c.id,
                kb_fragments.c.document_id,
                kb_documents.c.title,
                kb_documents.c.scope,
                kb_documents.c.page_count,
                kb_fragments.c.page_number,
                sa.func.substr(kb_pages.c.text, kb_fragments.c.start_offset + 1, length).label(
                    "text"
                ),
            )
            .select_from(
                kb_fragments.join(
                    kb_pages,
                    sa.and_(
                        kb_pages.c.document_id == kb_fragments.c.document_id,
                        kb_pages.c.number == kb_fragments.c.page_number,
                    ),
                ).join(kb_documents, kb_documents.c.id == kb_fragments.c.document_id)
            )
            .where(
                kb_fragments.c.id.in_(fragment_ids),
                _visible(user_id),
                _in_knowledge_scope(scope),
                kb_documents.c.status == "ready",
            )
        )
        return [
            FoundFragment(
                fragment_id=row.id,
                document_id=row.document_id,
                document_title=row.title,
                scope=row.scope,
                page=row.page_number if row.page_count is not None else None,
                text=row.text,
            )
            for row in await self._connection.execute(query)
        ]

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
            sa.or_(kb_jobs.c.locked_until.is_(None), kb_jobs.c.locked_until <= now),
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
            .values(
                document_id=document_id,
                number=page.number,
                text=page.text,
                recognized=page.recognized,
            )
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
            sa.select(kb_pages.c.number, kb_pages.c.text, kb_pages.c.recognized)
            .where(kb_pages.c.document_id == document_id)
            .order_by(kb_pages.c.number)
        )
        return [PageText(row.number, row.text, row.recognized) for row in rows]

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

    async def delete(self, document_id: UUID) -> None:
        """Стереть строку документа; страницы, фрагменты и задания уходят каскадом."""
        await self._connection.execute(
            sa.delete(kb_documents).where(kb_documents.c.id == document_id)
        )

    async def ids_for_reindex(self, *, only_errors: bool) -> list[UUID]:
        """Неудалённые документы по времени добавления; `only_errors` — только с ошибкой."""
        query = sa.select(kb_documents.c.id).where(kb_documents.c.deleted_at.is_(None))
        if only_errors:
            query = query.where(kb_documents.c.status == "error")
        rows = await self._connection.execute(query.order_by(kb_documents.c.created_at))
        return [row.id for row in rows]


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

    async def remove(self, document_id: UUID, kind: str) -> None:
        """Снять задание документа."""
        await self._connection.execute(
            sa.delete(kb_jobs).where(kb_jobs.c.document_id == document_id, kb_jobs.c.kind == kind)
        )

    async def claim(self, now: datetime, lease_until: datetime) -> Job | None:
        """Взять одно задание: `SELECT … FOR UPDATE SKIP LOCKED` и аренда в той же транзакции."""
        candidate = (
            sa.select(kb_jobs.c.id)
            .where(
                kb_jobs.c.run_after <= now,
                sa.or_(kb_jobs.c.locked_until.is_(None), kb_jobs.c.locked_until <= now),
            )
            .order_by(sa.case((kb_jobs.c.kind == "delete", 0), else_=1), kb_jobs.c.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = (
            await self._connection.execute(
                sa.update(kb_jobs)
                .where(kb_jobs.c.id == candidate.scalar_subquery())
                .values(attempts=kb_jobs.c.attempts + 1, locked_until=lease_until)
                .returning(kb_jobs)
            )
        ).first()
        return Job(**row._asdict()) if row else None

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

    async def release(self, job_id: UUID) -> None:
        """Вернуть взятое задание в очередь, не считая попытку."""
        await self._connection.execute(
            sa.update(kb_jobs)
            .where(kb_jobs.c.id == job_id)
            .values(locked_until=None, attempts=sa.func.greatest(kb_jobs.c.attempts - 1, 0))
        )

    async def finish(self, job_id: UUID) -> None:
        """Удалить выполненное задание."""
        await self._connection.execute(sa.delete(kb_jobs).where(kb_jobs.c.id == job_id))

    async def has_leased(self, now: datetime) -> bool:
        """Есть ли задания в аренде."""
        query = sa.select(sa.exists().where(kb_jobs.c.locked_until > now))
        return bool(await self._connection.scalar(query))


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
