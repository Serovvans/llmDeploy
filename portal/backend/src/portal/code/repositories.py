"""Хранилище раздела «Код» на SQLAlchemy Core; условие владельца — в каждом запросе (§9.15)."""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from portal.auth.tables import users
from portal.code.domain import Project, ProjectFile, Snapshot, StoredBuild
from portal.code.ports import CodeRepository, CodeUnitOfWork, ProjectPage, StoredEdit
from portal.code.tables import (
    code_builds,
    code_edit_files,
    code_files,
    code_projects,
    code_snapshots,
)
from portal.dialogs.ports import DialogRepository
from portal.dialogs.repositories import SqlDialogRepository
from portal.dialogs.tables import dialogs, messages

type _Row = sa.Row[*tuple[Any, ...]]

_BUILD_COLUMNS = [column for column in code_builds.c if column.name != "archive"]


def _owned(owner_id: UUID) -> sa.Select[tuple[UUID]]:
    """Идентификаторы проектов владельца — условие изоляции для дочерних таблиц."""
    return sa.select(code_projects.c.id).where(code_projects.c.owner_id == owner_id)


def _project(row: _Row) -> Project:
    return Project(
        id=row.id,
        owner_id=row.owner_id,
        name=row.name,
        packages=list(row.packages),
        schema_id=row.schema_id,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        kind=row.kind,
        db_connection_id=row.db_connection_id,
        db_connection_title=row.db_connection_title,
        db_tables=list(row.db_tables),
    )


def _file(row: _Row) -> ProjectFile:
    return ProjectFile(**{column.name: getattr(row, column.name) for column in code_files.c})


def _snapshot(row: _Row) -> Snapshot:
    return Snapshot(**{column.name: getattr(row, column.name) for column in code_snapshots.c})


def _build(row: _Row) -> StoredBuild:
    return StoredBuild(**{column.name: getattr(row, column.name) for column in _BUILD_COLUMNS})


class SqlCodeRepository:
    """Таблицы `code_projects`, `code_files`, `code_snapshots`, `code_builds`, `code_edit_files`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def lock_owner(self, owner_id: UUID) -> None:
        """`SELECT … FOR UPDATE` строки пользователя (§7.5.3)."""
        await self._connection.execute(
            sa.select(users.c.id).where(users.c.id == owner_id).with_for_update()
        )

    async def count_projects(self, owner_id: UUID) -> int:
        """Число проектов сотрудника."""
        total = await self._connection.scalar(
            sa.select(sa.func.count())
            .select_from(code_projects)
            .where(code_projects.c.owner_id == owner_id)
        )
        return total or 0

    async def name_taken(self, owner_id: UUID, name: str) -> bool:
        """Есть ли проект с этим именем без учёта регистра."""
        found = await self._connection.scalar(
            sa.select(code_projects.c.id).where(
                code_projects.c.owner_id == owner_id,
                sa.func.lower(code_projects.c.name) == name.lower(),
            )
        )
        return found is not None

    async def add_project(self, project: Project, files: Sequence[ProjectFile]) -> None:
        """Создать проект с файлами."""
        await self._connection.execute(
            sa.insert(code_projects).values(
                {column.name: getattr(project, column.name) for column in code_projects.c}
            )
        )
        for file in files:
            await self.add_file(project.owner_id, file)

    async def list_projects(
        self, owner_id: UUID, limit: int, before: tuple[datetime, UUID] | None
    ) -> list[ProjectPage]:
        """Проекты по убыванию `updated_at`, затем `id`, после курсора, с последней сборкой."""
        query = (
            sa.select(
                code_projects, *(column.label(f"b_{column.name}") for column in _BUILD_COLUMNS)
            )
            .select_from(
                code_projects.outerjoin(code_builds, code_builds.c.project_id == code_projects.c.id)
            )
            .where(code_projects.c.owner_id == owner_id)
        )
        if before is not None:
            query = query.where(
                sa.tuple_(code_projects.c.updated_at, code_projects.c.id) < sa.tuple_(*before)
            )
        query = query.order_by(code_projects.c.updated_at.desc(), code_projects.c.id.desc()).limit(
            limit
        )
        pages = []
        for row in await self._connection.execute(query):
            build = None
            if row.b_project_id is not None:
                build = StoredBuild(
                    **{column.name: getattr(row, f"b_{column.name}") for column in _BUILD_COLUMNS}
                )
            pages.append(ProjectPage(_project(row), build))
        return pages

    async def get_project(
        self, owner_id: UUID, project_id: UUID, *, lock: bool = False
    ) -> Project | None:
        """Проект владельца; `lock` — `SELECT … FOR UPDATE`."""
        query = sa.select(code_projects).where(
            code_projects.c.id == project_id, code_projects.c.owner_id == owner_id
        )
        if lock:
            query = query.with_for_update()
        row = (await self._connection.execute(query)).first()
        return _project(row) if row else None

    async def project_by_dialog(self, owner_id: UUID, dialog_id: UUID) -> Project | None:
        """Проект диалога помощника."""
        row = (
            await self._connection.execute(
                sa.select(code_projects)
                .join(dialogs, dialogs.c.project_id == code_projects.c.id)
                .where(
                    dialogs.c.id == dialog_id,
                    dialogs.c.owner_id == owner_id,
                    code_projects.c.owner_id == owner_id,
                )
            )
        ).first()
        return _project(row) if row else None

    async def dialog_of(self, owner_id: UUID, project_id: UUID) -> UUID | None:
        """Диалог помощника проекта."""
        found: UUID | None = await self._connection.scalar(
            sa.select(dialogs.c.id).where(
                dialogs.c.project_id == project_id, dialogs.c.owner_id == owner_id
            )
        )
        return found

    async def save_project(self, project: Project) -> None:
        """Записать пакеты, схему, выбор подключения, ревизию и время изменения проекта."""
        await self._connection.execute(
            sa.update(code_projects)
            .where(code_projects.c.id == project.id, code_projects.c.owner_id == project.owner_id)
            .values(
                packages=project.packages,
                schema_id=project.schema_id,
                db_connection_id=project.db_connection_id,
                db_connection_title=project.db_connection_title,
                db_tables=project.db_tables,
                revision=project.revision,
                updated_at=project.updated_at,
            )
        )

    async def delete_project(self, owner_id: UUID, project_id: UUID) -> bool:
        """Удалить проект каскадом; `False` — его нет."""
        result = await self._connection.execute(
            sa.delete(code_projects).where(
                code_projects.c.id == project_id, code_projects.c.owner_id == owner_id
            )
        )
        return bool(result.rowcount)

    async def files(self, owner_id: UUID, project_id: UUID) -> list[ProjectFile]:
        """Файлы проекта по пути без учёта регистра."""
        rows = await self._connection.execute(
            sa.select(code_files)
            .where(
                code_files.c.project_id == project_id,
                code_files.c.project_id.in_(_owned(owner_id)),
            )
            .order_by(sa.func.lower(code_files.c.path))
        )
        return [_file(row) for row in rows]

    async def add_file(self, owner_id: UUID, file: ProjectFile) -> None:
        """Создать файл; проект владельца проверен в той же транзакции."""
        await self._connection.execute(
            sa.insert(code_files).values(
                {column.name: getattr(file, column.name) for column in code_files.c}
            )
        )

    async def save_file(self, owner_id: UUID, file: ProjectFile) -> None:
        """Записать путь, содержимое, размер, ревизию, время и настройки Каталога файла."""
        await self._connection.execute(
            sa.update(code_files)
            .where(code_files.c.id == file.id, code_files.c.project_id.in_(_owned(owner_id)))
            .values(
                path=file.path,
                content=file.content,
                size_bytes=file.size_bytes,
                revision=file.revision,
                updated_at=file.updated_at,
                catalog_insertion=file.catalog_insertion,
                catalog_clients=file.catalog_clients,
            )
        )

    async def delete_file(self, owner_id: UUID, project_id: UUID, file_id: UUID) -> None:
        """Удалить файл."""
        await self._connection.execute(
            sa.delete(code_files).where(
                code_files.c.id == file_id,
                code_files.c.project_id == project_id,
                code_files.c.project_id.in_(_owned(owner_id)),
            )
        )

    async def snapshots(self, owner_id: UUID, project_id: UUID) -> list[Snapshot]:
        """Хранимые версии по ревизии, новые сверху."""
        rows = await self._connection.execute(
            sa.select(code_snapshots)
            .where(
                code_snapshots.c.project_id == project_id,
                code_snapshots.c.project_id.in_(_owned(owner_id)),
            )
            .order_by(code_snapshots.c.project_revision.desc())
        )
        return [_snapshot(row) for row in rows]

    async def get_snapshot(
        self, owner_id: UUID, project_id: UUID, snapshot_id: UUID
    ) -> Snapshot | None:
        """Версия проекта владельца."""
        row = (
            await self._connection.execute(
                sa.select(code_snapshots).where(
                    code_snapshots.c.id == snapshot_id,
                    code_snapshots.c.project_id == project_id,
                    code_snapshots.c.project_id.in_(_owned(owner_id)),
                )
            )
        ).first()
        return _snapshot(row) if row else None

    async def add_snapshot(self, owner_id: UUID, snapshot: Snapshot, keep: int) -> bool:
        """Создать версию, если с её ревизией версии нет; оставить `keep` новейших."""
        exists = await self._connection.scalar(
            sa.select(code_snapshots.c.id).where(
                code_snapshots.c.project_id == snapshot.project_id,
                code_snapshots.c.project_revision == snapshot.project_revision,
                code_snapshots.c.project_id.in_(_owned(owner_id)),
            )
        )
        if exists is not None:
            return False
        await self._connection.execute(
            sa.insert(code_snapshots).values(
                {column.name: getattr(snapshot, column.name) for column in code_snapshots.c}
            )
        )
        kept = (
            sa.select(code_snapshots.c.id)
            .where(code_snapshots.c.project_id == snapshot.project_id)
            .order_by(code_snapshots.c.project_revision.desc())
            .limit(keep)
        )
        await self._connection.execute(
            sa.delete(code_snapshots).where(
                code_snapshots.c.project_id == snapshot.project_id,
                code_snapshots.c.project_id.in_(_owned(owner_id)),
                code_snapshots.c.id.not_in(kept),
            )
        )
        return True

    async def build(self, owner_id: UUID, project_id: UUID) -> StoredBuild | None:
        """Последняя сборка кнопкой без архива."""
        row = (
            await self._connection.execute(
                sa.select(*_BUILD_COLUMNS).where(
                    code_builds.c.project_id == project_id,
                    code_builds.c.project_id.in_(_owned(owner_id)),
                )
            )
        ).first()
        return _build(row) if row else None

    async def save_build(self, owner_id: UUID, build: StoredBuild, archive: bytes | None) -> None:
        """Заменить сборку проекта новой."""
        await self._connection.execute(
            sa.delete(code_builds).where(
                code_builds.c.project_id == build.project_id,
                code_builds.c.project_id.in_(_owned(owner_id)),
            )
        )
        values = {column.name: getattr(build, column.name) for column in _BUILD_COLUMNS}
        await self._connection.execute(sa.insert(code_builds).values(**values, archive=archive))

    async def archive(self, owner_id: UUID, project_id: UUID) -> bytes | None:
        """Сохранённый архив для CoGIS."""
        found: bytes | None = await self._connection.scalar(
            sa.select(code_builds.c.archive).where(
                code_builds.c.project_id == project_id,
                code_builds.c.project_id.in_(_owned(owner_id)),
            )
        )
        return found

    def _project_messages(self, owner_id: UUID, project_id: UUID) -> sa.Select[tuple[UUID]]:
        return sa.select(dialogs.c.id).where(
            dialogs.c.project_id == project_id, dialogs.c.owner_id == owner_id
        )

    async def edit(self, owner_id: UUID, project_id: UUID, message_id: UUID) -> StoredEdit | None:
        """Правка ответа диалога этого проекта."""
        summary = await self._connection.scalar(
            sa.select(messages.c.code_edit).where(
                messages.c.id == message_id,
                messages.c.role == "assistant",
                messages.c.code_edit.is_not(None),
                messages.c.dialog_id.in_(self._project_messages(owner_id, project_id)),
            )
        )
        if summary is None:
            return None
        rows = await self._connection.execute(
            sa.select(code_edit_files).where(code_edit_files.c.message_id == message_id)
        )
        files = {row.path: (row.base_revision, row.content) for row in rows}
        return StoredEdit(message_id, dict(summary), files)

    async def save_edit(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        message_id: UUID,
        summary: Mapping[str, Any],
        files: Mapping[str, tuple[int | None, str]],
    ) -> bool:
        """Записать правку и перевести прежние `proposed` диалога в `superseded` (§7.5.10)."""
        owned = sa.select(dialogs.c.id).where(
            dialogs.c.id == dialog_id, dialogs.c.owner_id == owner_id
        )
        # Одним условным UPDATE: решение, зафиксированное `decide` параллельно, PostgreSQL
        # перепроверит по новой версии строки и не затрёт (блокировка проекта не нужна).
        superseded = sa.func.jsonb_build_object("status", "superseded")
        await self._connection.execute(
            sa.update(messages)
            .where(
                messages.c.dialog_id.in_(owned),
                messages.c.id != message_id,
                messages.c.code_edit["status"].astext == "proposed",
            )
            .values(code_edit=messages.c.code_edit.op("||")(superseded))
        )
        result = await self._connection.execute(
            sa.update(messages)
            .where(messages.c.id == message_id, messages.c.dialog_id.in_(owned))
            .values(code_edit=dict(summary))
        )
        if not result.rowcount:
            return False
        for path, (base_revision, content) in files.items():
            await self._connection.execute(
                sa.insert(code_edit_files).values(
                    message_id=message_id, path=path, base_revision=base_revision, content=content
                )
            )
        return True

    async def decide_edit(
        self, owner_id: UUID, project_id: UUID, message_id: UUID, summary: Mapping[str, Any]
    ) -> None:
        """Записать решение по правке (сводка целиком)."""
        await self._connection.execute(
            sa.update(messages)
            .where(
                messages.c.id == message_id,
                messages.c.dialog_id.in_(self._project_messages(owner_id, project_id)),
            )
            .values(code_edit=dict(summary))
        )


class SqlCodeUnitOfWork:
    """Хранилища раздела и диалогов на одном соединении: проект и его диалог — одной транзакцией."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Собрать хранилища вокруг соединения."""
        self.code: CodeRepository = SqlCodeRepository(connection)
        self.dialogs: DialogRepository = SqlDialogRepository(connection)


class SqlCodeUnitOfWorkFactory:
    """Открывает единицу работы на соединении из пула."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Запомнить движок."""
        self._engine = engine

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[CodeUnitOfWork]:
        """Фиксация при выходе без ошибки; при ошибке незафиксированное откатывается."""
        async with self._engine.connect() as connection:
            yield SqlCodeUnitOfWork(connection)
            await connection.commit()
