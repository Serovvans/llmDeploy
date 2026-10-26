"""Хранилище подключений на SQLAlchemy Core; условие доступа сотрудника — в запросе (§9.15)."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from portal.auth.tables import users
from portal.core.audit import SqlAuditLog
from portal.core.ports import AuditLog, Clock
from portal.databases.domain import AccessUser, Connection, SnapshotSummary, StoredSnapshot
from portal.databases.ports import DbRepository, DbUnitOfWork
from portal.databases.tables import db_connection_users, db_connections, db_snapshots

type _Row = sa.Row[*tuple[Any, ...]]

_TITLE_INDEX = "db_connections_title_key"


def _connection(row: _Row) -> Connection:
    values = {column.name: getattr(row, column.name) for column in db_connections.c}
    values["schemas"] = list(values["schemas"])
    return Connection(**values)


def accessible(user_id: UUID) -> sa.ColumnElement[bool]:
    """Условие доступа к подключению (§9.15): всем, по списку; администратору — все (§7.6.3)."""
    listed = sa.exists().where(
        db_connection_users.c.connection_id == db_connections.c.id,
        db_connection_users.c.user_id == user_id,
    )
    admin = sa.exists().where(users.c.id == user_id, users.c.role == "admin")
    return sa.or_(db_connections.c.access_all, listed, admin)


def _ordered(query: sa.Select[Any]) -> sa.Select[Any]:
    return query.order_by(sa.func.lower(db_connections.c.title), db_connections.c.id)


class SqlDbRepository:
    """Таблицы `db_connections`, `db_connection_users`, `db_snapshots`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def list_all(self) -> list[Connection]:
        """Все подключения."""
        rows = await self._connection.execute(_ordered(sa.select(db_connections)))
        return [_connection(row) for row in rows]

    async def list_for(self, user_id: UUID) -> list[Connection]:
        """Доступные сотруднику подключения."""
        rows = await self._connection.execute(
            _ordered(sa.select(db_connections).where(accessible(user_id)))
        )
        return [_connection(row) for row in rows]

    async def get(self, connection_id: UUID, *, lock: bool = False) -> Connection | None:
        """Подключение по идентификатору."""
        query = sa.select(db_connections).where(db_connections.c.id == connection_id)
        if lock:
            query = query.with_for_update()
        row = (await self._connection.execute(query)).first()
        return _connection(row) if row else None

    async def get_for(self, user_id: UUID, connection_id: UUID) -> Connection | None:
        """Подключение, доступное сотруднику."""
        row = (
            await self._connection.execute(
                sa.select(db_connections).where(
                    db_connections.c.id == connection_id, accessible(user_id)
                )
            )
        ).first()
        return _connection(row) if row else None

    async def title_taken(self, title: str, except_id: UUID | None) -> bool:
        """Есть ли другое подключение с этим названием без учёта регистра."""
        query = sa.select(db_connections.c.id).where(
            sa.func.lower(db_connections.c.title) == title.lower()
        )
        if except_id is not None:
            query = query.where(db_connections.c.id != except_id)
        return await self._connection.scalar(query) is not None

    async def _guarded(self, statement: sa.Executable) -> bool:
        """Выполнить в точке сохранения; нарушение уникальности названия — `False`."""
        try:
            async with self._connection.begin_nested():
                await self._connection.execute(statement)
        except IntegrityError as error:
            if _TITLE_INDEX in str(error.orig):
                return False
            raise
        return True

    async def add(self, connection: Connection) -> bool:
        """Создать подключение."""
        values = {column.name: getattr(connection, column.name) for column in db_connections.c}
        return await self._guarded(sa.insert(db_connections).values(values))

    async def save(self, connection: Connection) -> bool:
        """Записать все поля подключения."""
        values = {
            column.name: getattr(connection, column.name)
            for column in db_connections.c
            if column.name != "id"
        }
        return await self._guarded(
            sa.update(db_connections).where(db_connections.c.id == connection.id).values(values)
        )

    async def delete(self, connection_id: UUID) -> bool:
        """Удалить подключение каскадом."""
        result = await self._connection.execute(
            sa.delete(db_connections).where(db_connections.c.id == connection_id)
        )
        return bool(result.rowcount)

    async def access_users(self, connection_id: UUID) -> list[AccessUser]:
        """Список доступа по ФИО."""
        rows = await self._connection.execute(
            sa.select(users.c.id, users.c.login, users.c.full_name)
            .join(db_connection_users, db_connection_users.c.user_id == users.c.id)
            .where(db_connection_users.c.connection_id == connection_id)
            .order_by(users.c.full_name, users.c.id)
        )
        return [AccessUser(row.id, row.login, row.full_name) for row in rows]

    async def set_access(self, connection_id: UUID, user_ids: Sequence[UUID]) -> None:
        """Заменить список доступа."""
        await self._connection.execute(
            sa.delete(db_connection_users).where(
                db_connection_users.c.connection_id == connection_id
            )
        )
        if user_ids:
            await self._connection.execute(
                sa.insert(db_connection_users),
                [{"connection_id": connection_id, "user_id": user_id} for user_id in user_ids],
            )

    async def existing_users(self, user_ids: Sequence[UUID]) -> set[UUID]:
        """Какие из пользователей существуют."""
        if not user_ids:
            return set()
        rows = await self._connection.execute(
            sa.select(users.c.id).where(users.c.id.in_(list(user_ids)))
        )
        return {row.id for row in rows}

    async def summary(self, connection_id: UUID) -> SnapshotSummary | None:
        """Сводка снимка без чтения `content`."""
        row = (
            await self._connection.execute(
                sa.select(
                    db_snapshots.c.taken_at, db_snapshots.c.tables, db_snapshots.c.truncated
                ).where(db_snapshots.c.connection_id == connection_id)
            )
        ).first()
        return SnapshotSummary(row.taken_at, row.tables, row.truncated) if row else None

    async def snapshot(self, connection_id: UUID) -> StoredSnapshot | None:
        """Снимок подключения."""
        row = (
            await self._connection.execute(
                sa.select(db_snapshots).where(db_snapshots.c.connection_id == connection_id)
            )
        ).first()
        if row is None:
            return None
        return StoredSnapshot(
            row.connection_id,
            row.taken_at,
            list(row.content.get("tables", [])),
            row.truncated,
            row.postgis_schema,
        )

    async def save_snapshot(self, snapshot: StoredSnapshot) -> None:
        """Заменить снимок подключения целиком."""
        await self._connection.execute(
            sa.delete(db_snapshots).where(db_snapshots.c.connection_id == snapshot.connection_id)
        )
        await self._connection.execute(
            sa.insert(db_snapshots).values(
                connection_id=snapshot.connection_id,
                taken_at=snapshot.taken_at,
                content={"tables": snapshot.tables},
                tables=len(snapshot.tables),
                truncated=snapshot.truncated,
                postgis_schema=snapshot.postgis_schema,
            )
        )

    async def record(
        self,
        connection_id: UUID,
        *,
        rights: tuple[str, list[dict[str, Any]], list[dict[str, Any]], datetime] | None,
        last_error: dict[str, Any] | None,
        config_changed_at: datetime,
    ) -> bool:
        """Итог обращения: права, если проверка прав прошла, и последняя ошибка.

        Пишется, только если параметры входа не менялись с начала обращения
        (`config_changed_at` тот же); иначе `False` — итог устарел.
        """
        values: dict[str, Any] = {"last_error": last_error}
        if rights is not None:
            status, findings, warnings, checked_at = rights
            values.update(
                rights_status=status,
                rights_findings=findings,
                rights_warnings=warnings,
                rights_checked_at=checked_at,
            )
        result = await self._connection.execute(
            sa.update(db_connections)
            .where(
                db_connections.c.id == connection_id,
                db_connections.c.config_changed_at == config_changed_at,
            )
            .values(values)
        )
        return bool(result.rowcount)


class SqlDbUnitOfWork:
    """Хранилище подключений и журнал аудита на одном соединении."""

    def __init__(self, connection: AsyncConnection, clock: Clock) -> None:
        """Собрать хранилища вокруг соединения."""
        self.db: DbRepository = SqlDbRepository(connection)
        self.audit: AuditLog = SqlAuditLog(connection, clock)


class SqlDbUnitOfWorkFactory:
    """Открывает единицу работы на соединении из пула."""

    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        """Запомнить движок и часы журнала аудита."""
        self._engine = engine
        self._clock = clock

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[DbUnitOfWork]:
        """Фиксация при выходе без ошибки; при ошибке незафиксированное откатывается."""
        async with self._engine.connect() as connection:
            yield SqlDbUnitOfWork(connection, self._clock)
            await connection.commit()
