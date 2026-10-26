"""Порты подключений: базы заказчика, хранилище подключений и шифр пароля (§13.3).

`CustomerDatabase` — граница с базой заказчика: каждый метод — одно обращение §7.6.5
(место по `host:port`, соединение, транзакция `READ ONLY`, проверка прав первой). Имена в
запросах — только из снимка после сверки; текст сотрудника и модели в порт не приходит.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from portal.core.ports import AuditLog
from portal.databases.domain import (
    AccessUser,
    Connection,
    RightsFinding,
    SnapshotSummary,
    StoredSnapshot,
)

DbReason = Literal[
    "unreachable",
    "timeout",
    "auth_failed",
    "database_not_found",
    "tls_failed",
    "tls_required",
    "busy",
    "schemas_unavailable",
    "other",
]


@dataclass(frozen=True)
class DbTarget:
    """Параметры входа подключения (§9.29); пароль расшифрован только на время вызова."""

    connection_id: UUID
    host: str
    port: int
    database: str
    username: str
    password: str = field(repr=False)
    tls_mode: Literal["verify", "require", "disable"]
    schemas: Sequence[str]


@dataclass(frozen=True)
class SampleRequest:
    """Таблица образца; имена уже сверены со снимком (§7.6.8)."""

    schema: str
    table: str
    columns: Sequence[str]
    geometry: str | None = None


@dataclass(frozen=True)
class SampleRows:
    """Строки образца: текст СУБД; значение больше `db.value_max_bytes` — `None`."""

    columns: Sequence[str]
    rows: Sequence[Sequence[str | None]] = field(repr=False)
    geojson: Sequence[str | None] | None = field(repr=False)
    truncated: bool


@dataclass(frozen=True)
class RightsReport:
    """Итог запросов §7.6.6: находки 1–5 (непусто — `excess`) и предупреждения 6."""

    findings: Sequence[RightsFinding]
    warnings: Sequence[RightsFinding]


@dataclass(frozen=True)
class SnapshotContent:
    """Снимок схемы для строки `db_snapshots` (§7.6.7, §9.31)."""

    tables: list[dict[str, Any]]
    truncated: bool
    postgis_schema: str | None


class DbRightsExcessError(Exception):
    """У роли лишние права: обращение остановлено после проверки прав."""

    def __init__(self, report: RightsReport | None) -> None:
        """Запомнить итог проверки прав; `None` — отказ по записанным правам (шаг 0)."""
        super().__init__()
        self.report = report


class DbUnavailableError(Exception):
    """Обращение не удалось: причина §7.6.4 и подробности (у `schemas_unavailable`)."""

    def __init__(
        self,
        reason: DbReason,
        details: dict[str, Any] | None = None,
        report: RightsReport | None = None,
    ) -> None:
        """Запомнить причину; `report` — итог проверки прав, если она прошла (у `check`)."""
        super().__init__(reason)
        self.reason: DbReason = reason
        self.details = details or {}
        self.report = report


class CustomerDatabase(Protocol):
    """Обращения к базе заказчика (§7.6.5–7.6.8)."""

    async def check(self, target: DbTarget) -> RightsReport:
        """Проверка подключения: права и схемы; `findings` пуст — `read_only`.

        Схемы недоступны — `DbUnavailableError("schemas_unavailable", report=…)`.
        """
        ...

    async def snapshot(self, target: DbTarget) -> tuple[RightsReport, SnapshotContent]:
        """Проверка прав и снимок схемы; лишние права — `DbRightsExcessError`."""
        ...

    async def sample(
        self, target: DbTarget, tables: Sequence[SampleRequest], *, rows: int, preview: bool
    ) -> tuple[RightsReport, Sequence[SampleRows]]:
        """Проверка прав и образцы таблиц в порядке `tables`; лишние права — исключение."""
        ...


class PasswordCipher(Protocol):
    """Шифр пароля подключения: AES-256-GCM, AAD — идентификатор подключения (§2.7)."""

    def encrypt(self, connection_id: UUID, password: str) -> bytes:
        """Nonce и шифртекст."""
        ...

    def decrypt(self, connection_id: UUID, data: bytes) -> str:
        """Пароль; чужое подключение или подмена — исключение."""
        ...


class DbRepository(Protocol):
    """Таблицы `db_connections`, `db_connection_users`, `db_snapshots`.

    Сотруднику подключение доступно при `access_all` или по списку доступа (§9.15);
    метод `get_for` и `list_for` включают это условие в сам запрос.
    """

    async def list_all(self) -> list[Connection]:
        """Все подключения по `lower(title)`, затем `id`."""
        ...

    async def list_for(self, user_id: UUID) -> list[Connection]:
        """Доступные сотруднику подключения по `lower(title)`, затем `id`."""
        ...

    async def get(self, connection_id: UUID, *, lock: bool = False) -> Connection | None:
        """Подключение по идентификатору (администратор и служебные сценарии)."""
        ...

    async def get_for(self, user_id: UUID, connection_id: UUID) -> Connection | None:
        """Подключение, доступное сотруднику."""
        ...

    async def title_taken(self, title: str, except_id: UUID | None) -> bool:
        """Есть ли другое подключение с этим названием без учёта регистра."""
        ...

    async def add(self, connection: Connection) -> bool:
        """Создать подключение; `False` — название занято."""
        ...

    async def save(self, connection: Connection) -> bool:
        """Записать все поля подключения; `False` — название занято."""
        ...

    async def delete(self, connection_id: UUID) -> bool:
        """Удалить подключение каскадом; `False` — его нет."""
        ...

    async def access_users(self, connection_id: UUID) -> list[AccessUser]:
        """Список доступа по ФИО."""
        ...

    async def set_access(self, connection_id: UUID, user_ids: Sequence[UUID]) -> None:
        """Заменить список доступа."""
        ...

    async def existing_users(self, user_ids: Sequence[UUID]) -> set[UUID]:
        """Какие из пользователей существуют."""
        ...

    async def summary(self, connection_id: UUID) -> SnapshotSummary | None:
        """Сводка снимка без `content`."""
        ...

    async def snapshot(self, connection_id: UUID) -> StoredSnapshot | None:
        """Снимок подключения."""
        ...

    async def save_snapshot(self, snapshot: StoredSnapshot) -> None:
        """Заменить снимок подключения целиком."""
        ...

    async def record(
        self,
        connection_id: UUID,
        *,
        rights: tuple[str, list[dict[str, Any]], list[dict[str, Any]], datetime] | None,
        last_error: dict[str, Any] | None,
        config_changed_at: datetime,
    ) -> bool:
        """Итог обращения: права (если проверка прав прошла) и последняя ошибка.

        Пишется, только если `config_changed_at` не изменился с начала обращения;
        иначе `False` (параметры входа сменили, итог устарел).
        """
        ...


class DbUnitOfWork(Protocol):
    """Хранилище подключений и журнал аудита одной транзакции."""

    db: DbRepository
    audit: AuditLog


class DbUnitOfWorkFactory(Protocol):
    """Открывает единицу работы: фиксация при выходе без ошибки, иначе откат."""

    def __call__(self) -> AbstractAsyncContextManager[DbUnitOfWork]:
        """Начать транзакцию."""
        ...
