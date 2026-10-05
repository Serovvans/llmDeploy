"""Тела запросов и ответов инструментов (docs/portal-api.md §7.1)."""

from uuid import UUID

from pydantic import BaseModel

from portal.core.schemas import ApiTime, RequestModel
from portal.tools.sql_schemas import SqlSchema


class SqlSchemaRequest(RequestModel):
    """`POST` и `PUT /api/sql/schemas`."""

    name: str
    content: str


class SqlSchemaSummaryOut(BaseModel):
    """Схема в списке: без текста."""

    id: UUID
    name: str
    updated_at: ApiTime


class SqlSchemaListOut(BaseModel):
    """Все схемы пользователя."""

    items: list[SqlSchemaSummaryOut]


class SqlSchemaOut(SqlSchemaSummaryOut):
    """Объект `SqlSchema`."""

    content: str

    @classmethod
    def of(cls, schema: SqlSchema) -> "SqlSchemaOut":
        """Собрать из схемы."""
        return cls(
            id=schema.id, name=schema.name, content=schema.content, updated_at=schema.updated_at
        )
