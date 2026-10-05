"""Хранилище схем SQL на SQLAlchemy Core; условие владельца — в каждом запросе."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from portal.tools.sql_schemas import SqlSchema, SqlSchemaRepository
from portal.tools.tables import sql_schemas


def _schema(row: sa.Row[*tuple[Any, ...]]) -> SqlSchema:
    return SqlSchema(**row._asdict())


class SqlSchemaTable:
    """Таблица `sql_schemas`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def lock_owner(self, owner_id: UUID) -> None:
        """Блокировка уровня транзакции: счёт схем и вставка идут по очереди."""
        key = f"sql_schemas:{owner_id}"
        await self._connection.execute(
            sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(key, 0)))
        )

    async def list(self, owner_id: UUID) -> list[SqlSchema]:
        """Все схемы владельца по названию."""
        rows = await self._connection.execute(
            sa.select(sql_schemas)
            .where(sql_schemas.c.owner_id == owner_id)
            .order_by(sa.func.lower(sql_schemas.c.name), sql_schemas.c.id)
        )
        return [_schema(row) for row in rows]

    async def count(self, owner_id: UUID) -> int:
        """Сколько схем у владельца."""
        total = await self._connection.scalar(
            sa.select(sa.func.count())
            .select_from(sql_schemas)
            .where(sql_schemas.c.owner_id == owner_id)
        )
        return total or 0

    async def get(self, owner_id: UUID, schema_id: UUID) -> SqlSchema | None:
        """Схема владельца."""
        row = (
            await self._connection.execute(
                sa.select(sql_schemas).where(
                    sql_schemas.c.id == schema_id, sql_schemas.c.owner_id == owner_id
                )
            )
        ).first()
        return _schema(row) if row else None

    async def add(self, schema: SqlSchema) -> bool:
        """Создать схему; `False` — название занято."""
        values = {column.name: getattr(schema, column.name) for column in sql_schemas.c}
        try:
            async with self._connection.begin_nested():
                await self._connection.execute(sa.insert(sql_schemas).values(**values))
        except IntegrityError:
            return False
        return True

    async def save(self, schema: SqlSchema) -> bool:
        """Записать название и текст; `False` — название занято."""
        try:
            async with self._connection.begin_nested():
                await self._connection.execute(
                    sa.update(sql_schemas)
                    .where(sql_schemas.c.id == schema.id, sql_schemas.c.owner_id == schema.owner_id)
                    .values(name=schema.name, content=schema.content, updated_at=schema.updated_at)
                )
        except IntegrityError:
            return False
        return True

    async def delete(self, owner_id: UUID, schema_id: UUID) -> bool:
        """Удалить схему; `False` — её не было."""
        result = await self._connection.execute(
            sa.delete(sql_schemas).where(
                sql_schemas.c.id == schema_id, sql_schemas.c.owner_id == owner_id
            )
        )
        return result.rowcount > 0


class SqlSchemaStoreFactory:
    """Открывает хранилище схем на соединении из пула."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Запомнить движок."""
        self._engine = engine

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlSchemaRepository]:
        """Фиксация при выходе без ошибки; при ошибке незафиксированное откатывается."""
        async with self._engine.connect() as connection:
            yield SqlSchemaTable(connection)
            await connection.commit()
