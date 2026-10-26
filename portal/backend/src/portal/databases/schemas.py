"""Тела запросов и ответов подключений к базам (docs/portal-api.md §7.6.1–7.6.3)."""

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from portal.core.schemas import ApiTime, RequestModel
from portal.databases.domain import Selection, SnapshotSummary
from portal.databases.service import UNSET, AccessInput, AdminView, ConnectionInput, EmployeeView


class ExclusionIn(RequestModel):
    """Исключение из образцов: таблица (`column: null`) или столбец."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="schema")
    table: str
    column: str | None


class AccessIn(RequestModel):
    """Доступ: `{all: true}` или `{all: false, user_ids}`."""

    all: bool
    user_ids: list[UUID] | None = None


class ConnectionRequest(RequestModel):
    """`POST` и `PATCH /api/admin/db-connections`: поля проверяет сценарий."""

    title: str | None = None
    description: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = Field(default=None, repr=False)
    tls_mode: Literal["verify", "require", "disable"] | None = None
    schemas: list[str] | None = None
    samples_allowed: bool | None = None
    sample_exclusions: list[ExclusionIn] | None = None
    access: AccessIn | None = None

    def to_input(self) -> ConnectionInput:
        """Переданные поля; `null` у обязательного поля сценарий отвергает как пустое."""
        values: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            if name == "sample_exclusions" and value is not None:
                value = [
                    {"schema": item.schema_, "table": item.table, "column": item.column}
                    for item in value
                ]
            elif name == "access" and value is not None:
                value = AccessInput(value.all, tuple(value.user_ids or ()))
            elif name in ("samples_allowed", "description") and value is None:
                value = UNSET
            values[name] = value
        return ConnectionInput(**values)


class SnapshotSummaryOut(BaseModel):
    """`{taken_at, tables, truncated}`."""

    taken_at: ApiTime
    tables: int
    truncated: bool

    @classmethod
    def of(cls, summary: SnapshotSummary | None) -> Self | None:
        """Сводка снимка или `null`."""
        if summary is None:
            return None
        return cls(taken_at=summary.taken_at, tables=summary.tables, truncated=summary.truncated)


class AccessUserOut(BaseModel):
    """Сотрудник списка доступа."""

    id: UUID
    login: str
    full_name: str


class AccessOut(BaseModel):
    """Доступ к подключению."""

    all: bool
    users: list[AccessUserOut]


class RightsOut(BaseModel):
    """Итог последней проверки прав."""

    status: str
    checked_at: ApiTime | None
    findings: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


class AdminDbConnectionOut(BaseModel):
    """`AdminDbConnection`: пароля, его длины и признака «задан» нет."""

    id: UUID
    title: str
    description: str
    host: str
    port: int
    database: str
    username: str
    tls_mode: str
    schemas: list[str]
    samples_allowed: bool
    sample_exclusions: list[dict[str, str | None]]
    access: AccessOut
    rights: RightsOut
    last_error: dict[str, Any] | None
    snapshot: SnapshotSummaryOut | None
    refresh_available_at: ApiTime | None
    created_at: ApiTime
    updated_at: ApiTime

    @classmethod
    def of(cls, view: AdminView) -> Self:
        """Подключение для администратора."""
        connection = view.connection
        return cls(
            id=connection.id,
            title=connection.title,
            description=connection.description,
            host=connection.host,
            port=connection.port,
            database=connection.database,
            username=connection.username,
            tls_mode=connection.tls_mode,
            schemas=connection.schemas,
            samples_allowed=connection.samples_allowed,
            sample_exclusions=connection.sample_exclusions,
            access=AccessOut(
                all=connection.access_all,
                users=[
                    AccessUserOut(id=user.id, login=user.login, full_name=user.full_name)
                    for user in view.users
                ],
            ),
            rights=RightsOut(
                status=connection.rights_status,
                checked_at=connection.rights_checked_at,
                findings=connection.rights_findings,
                warnings=connection.rights_warnings,
            ),
            last_error=connection.last_error,
            snapshot=SnapshotSummaryOut.of(view.snapshot),
            refresh_available_at=view.refresh_available_at,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
        )


class AdminDbConnectionListOut(BaseModel):
    """`GET /api/admin/db-connections`."""

    items: list[AdminDbConnectionOut]


class DbConnectionOut(BaseModel):
    """`DbConnection` — строка списка сотрудника."""

    id: UUID
    title: str
    description: str
    usable: bool
    samples_allowed: bool
    snapshot: SnapshotSummaryOut | None
    refresh_available_at: ApiTime | None

    @classmethod
    def of(cls, view: EmployeeView) -> Self:
        """Подключение без адреса, учётной записи, исключений, доступа и находок."""
        connection = view.connection
        return cls(
            id=connection.id,
            title=connection.title,
            description=connection.description,
            usable=connection.rights_status != "excess",
            samples_allowed=connection.samples_allowed,
            snapshot=SnapshotSummaryOut.of(view.snapshot),
            refresh_available_at=view.refresh_available_at,
        )


class DbConnectionListOut(BaseModel):
    """`GET /api/db/connections`."""

    items: list[DbConnectionOut]


class DbSnapshotOut(BaseModel):
    """`DbSnapshot`."""

    connection_id: UUID
    taken_at: ApiTime
    truncated: bool
    tables: list[dict[str, Any]]


class TableRef(RequestModel):
    """Таблица выбора `{schema, name}`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="schema")
    name: str


class SelectionRequest(RequestModel):
    """Выбор подключения в `PATCH` диалога и проекта (§7.6.9)."""

    connection_id: UUID
    tables: list[TableRef]

    def pairs(self) -> list[tuple[str, str]]:
        """Таблицы `(schema, name)`."""
        return [(item.schema_, item.name) for item in self.tables]


class DbSelectionOut(BaseModel):
    """`DbSelection`."""

    connection_id: UUID | None
    title: str
    tables: list[dict[str, str]]
    available: bool

    @classmethod
    def of(cls, selection: Selection | None, available: bool) -> Self | None:
        """Выбор или `null`."""
        if selection is None:
            return None
        return cls(
            connection_id=selection.connection_id,
            title=selection.title,
            tables=selection.tables_data(),
            available=available,
        )
