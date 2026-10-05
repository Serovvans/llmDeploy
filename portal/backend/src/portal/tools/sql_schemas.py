"""Личные схемы баз для SQL-помощника: сущность, порт хранилища и сценарии (§7.1).

Каждый метод хранилища принимает владельца и отбирает по `sql_schemas.owner_id`: чужая
схема — `not_found`, в том числе для администратора.
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from portal.core.errors import field_error, not_found, validation_error
from portal.core.ports import Clock
from portal.core.settings import SqlSettings
from portal.tools import errors

_NAME_MAX_LENGTH = 100


@dataclass
class SqlSchema:
    """Сохранённая схема базы: DDL или описание таблиц."""

    id: UUID
    owner_id: UUID
    name: str
    content: str
    created_at: datetime
    updated_at: datetime


class SqlSchemaRepository(Protocol):
    """Таблица `sql_schemas`."""

    async def lock_owner(self, owner_id: UUID) -> None:
        """До конца транзакции не пускать другие изменения схем этого владельца."""
        ...

    async def list(self, owner_id: UUID) -> list[SqlSchema]:
        """Все схемы владельца по названию."""
        ...

    async def count(self, owner_id: UUID) -> int:
        """Сколько схем у владельца."""
        ...

    async def get(self, owner_id: UUID, schema_id: UUID) -> SqlSchema | None:
        """Схема владельца."""
        ...

    async def add(self, schema: SqlSchema) -> bool:
        """Создать схему; `False` — название занято."""
        ...

    async def save(self, schema: SqlSchema) -> bool:
        """Записать название и текст; `False` — название занято."""
        ...

    async def delete(self, owner_id: UUID, schema_id: UUID) -> bool:
        """Удалить схему; `False` — её не было."""
        ...


class SqlSchemaStore(Protocol):
    """Открывает хранилище в транзакции: фиксация при выходе без ошибки."""

    def __call__(self) -> AbstractAsyncContextManager[SqlSchemaRepository]:
        """Начать транзакцию."""
        ...


class SqlSchemaService:
    """Схемы владельца."""

    def __init__(self, store: SqlSchemaStore, clock: Clock, settings: SqlSettings) -> None:
        """Получить зависимости явно."""
        self._store = store
        self._clock = clock
        self._settings = settings

    def _checked(self, name: str, content: str) -> tuple[str, str]:
        name = name.strip()
        fields = []
        if not name:
            fields.append(field_error("name", "required"))
        elif len(name) > _NAME_MAX_LENGTH:
            fields.append(field_error("name", "too_long"))
        if not content.strip():
            fields.append(field_error("content", "required"))
        elif len(content) > self._settings.schema_max_chars:
            fields.append(field_error("content", "too_long"))
        if fields:
            raise validation_error(fields)
        return name, content

    async def list(self, owner_id: UUID) -> list[SqlSchema]:
        """Все схемы владельца по названию."""
        async with self._store() as schemas:
            return await schemas.list(owner_id)

    async def get(self, owner_id: UUID, schema_id: UUID) -> SqlSchema:
        """Схема владельца."""
        schema = await self.find(owner_id, schema_id)
        if schema is None:
            raise not_found()
        return schema

    async def find(self, owner_id: UUID, schema_id: UUID) -> SqlSchema | None:
        """Схема владельца; `None` — такой нет (или она чужая)."""
        async with self._store() as schemas:
            return await schemas.get(owner_id, schema_id)

    async def create(self, owner_id: UUID, name: str, content: str) -> SqlSchema:
        """Сохранить новую схему; число схем у владельца ограничено."""
        name, content = self._checked(name, content)
        now = self._clock.now()
        schema = SqlSchema(uuid4(), owner_id, name, content, now, now)
        async with self._store() as schemas:
            await schemas.lock_owner(owner_id)
            if await schemas.count(owner_id) >= self._settings.max_schemas:
                raise errors.schema_limit_reached()
            if not await schemas.add(schema):
                raise errors.schema_name_taken()
        return schema

    async def update(self, owner_id: UUID, schema_id: UUID, name: str, content: str) -> SqlSchema:
        """Заменить название и текст схемы."""
        name, content = self._checked(name, content)
        async with self._store() as schemas:
            await schemas.lock_owner(owner_id)
            schema = await schemas.get(owner_id, schema_id)
            if schema is None:
                raise not_found()
            schema.name, schema.content, schema.updated_at = name, content, self._clock.now()
            if not await schemas.save(schema):
                raise errors.schema_name_taken()
            return schema

    async def delete(self, owner_id: UUID, schema_id: UUID) -> None:
        """Удалить схему."""
        async with self._store() as schemas:
            if not await schemas.delete(owner_id, schema_id):
                raise not_found()
