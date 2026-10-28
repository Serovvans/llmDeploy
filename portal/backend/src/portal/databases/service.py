"""Сценарии подключений к базам заказчика (docs/portal-api.md §7.6).

Администратор ведёт подключения и проверяет права; сотрудник видит доступные ему,
обновляет снимок схемы и выбирает таблицы. Каждое обращение к базе идёт через
`access`: шаг 0 (записанные лишние права — без соединения), порт `CustomerDatabase`,
запись итога в строку подключения и строка журнала `db access` — только числа.
"""

import asyncio
import ipaddress
import json
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC
from typing import Any, Literal
from uuid import UUID, uuid4

from portal.core.errors import (
    FieldError,
    feature_disabled,
    field_error,
    not_found,
    validation_error,
)
from portal.core.ports import Clock, CurrentUser
from portal.core.settings import DbSettings
from portal.databases import errors
from portal.databases.domain import (
    AccessUser,
    Connection,
    Purpose,
    Selection,
    SnapshotSummary,
    StoredSnapshot,
    copy_plan,
    ddl_text,
    exclusion_map,
    find_table,
    refresh_available_at,
    sample_origin,
    selected_tables,
    snapshot_body,
    table_index,
)
from portal.databases.hosts import HostPolicy
from portal.databases.ports import (
    CustomerDatabase,
    DbRightsExcessError,
    DbTarget,
    DbUnavailableError,
    DbUnitOfWork,
    DbUnitOfWorkFactory,
    PasswordCipher,
    RightsReport,
    SampleRequest,
    SampleRows,
)

logger = logging.getLogger(__name__)

_TITLE_MAX = 100
_DESCRIPTION_MAX = 500
_HOST_MAX = 253
_NAME_MAX_BYTES = 63
_PASSWORD_MAX = 1024
_SCHEMAS_MAX = 50
_EXCLUSIONS_MAX = 500
_ACCESS_USERS_MAX = 1000
_FORBIDDEN = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
_HOST_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
# Смена этих полей — смена параметров входа (§7.6.2).
LOGIN_FIELDS = (
    "host",
    "port",
    "database",
    "username",
    "password",
    "tls_mode",
    "all_schemas",
    "schemas",
)


class _Unset:
    """Поле не передано."""


UNSET: Any = _Unset()


@dataclass(frozen=True)
class AccessInput:
    """Доступ: всем или списку сотрудников."""

    all: bool
    user_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class ConnectionInput:
    """Поля `POST` и `PATCH` подключения; `UNSET` — поле не передано."""

    title: Any = UNSET
    description: Any = UNSET
    host: Any = UNSET
    port: Any = UNSET
    database: Any = UNSET
    username: Any = UNSET
    password: Any = field(default=UNSET, repr=False)
    tls_mode: Any = UNSET
    all_schemas: Any = UNSET
    schemas: Any = UNSET
    samples_allowed: Any = UNSET
    sample_exclusions: Any = UNSET
    access: Any = UNSET

    def given(self) -> list[str]:
        """Имена переданных полей."""
        return [name for name in self.__dataclass_fields__ if getattr(self, name) is not UNSET]


@dataclass(frozen=True)
class AdminView:
    """Подключение для `AdminDbConnection`."""

    connection: Connection
    users: list[AccessUser]
    snapshot: SnapshotSummary | None
    refresh_available_at: Any


@dataclass(frozen=True)
class EmployeeView:
    """Подключение для `DbConnection` списка сотрудника."""

    connection: Connection
    snapshot: SnapshotSummary | None
    refresh_available_at: Any


@dataclass(frozen=True)
class SelectionInput:
    """`db` в `PATCH` диалога или проекта: подключение и таблицы `(schema, name)` (§7.6.9)."""

    connection_id: UUID
    tables: list[tuple[str, str]]


@dataclass(frozen=True)
class SelectionSource:
    """Выбор подключения с текущим подключением и снимком — для запроса и образца."""

    selection: Selection
    connection: Connection
    snapshot: StoredSnapshot


def _bad_text(value: str, allow_newline: bool = False) -> bool:
    return any(
        unicodedata.category(char) in _FORBIDDEN and not (allow_newline and char == "\n")
        for char in value
    )


def _name_error(value: Any) -> str | None:
    """Имя базы, роли, схемы, таблицы или столбца: 1…63 байта UTF-8 без управляющих."""
    if not isinstance(value, str) or not value:
        return "required"
    if len(value.encode()) > _NAME_MAX_BYTES:
        return "too_long"
    if any(unicodedata.category(char) == "Cc" for char in value):
        return "invalid_format"
    return None


_HOST_FORBIDDEN_MESSAGE = "Это внутренний адрес портала. Укажите сервер базы заказчика."


def _host_error(value: Any, policy: HostPolicy) -> str | None:
    """Сервер: имя узла или адрес, не внутри портала (§7.6.2) — без разрешения имени."""
    if not isinstance(value, str) or not value:
        return "required"
    if len(value) > _HOST_MAX:
        return "too_long"
    if policy.saved_host_forbidden(value):
        return "host_forbidden"
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None if _HOST_NAME.match(value) else "invalid_format"
    return None


class DbService:
    """Подключения к базам заказчика; доступ сотрудника — условием в каждом запросе."""

    def __init__(
        self,
        uow_factory: DbUnitOfWorkFactory,
        database: CustomerDatabase,
        cipher: PasswordCipher,
        clock: Clock,
        settings: DbSettings,
        tls_verify_available: Callable[[], bool],
        hosts: HostPolicy,
    ) -> None:
        """Получить зависимости явно; `hosts` — запрет внутренних адресов портала."""
        self._uow_factory = uow_factory
        self._database = database
        self._cipher = cipher
        self._clock = clock
        self._settings = settings
        self._tls_verify_available = tls_verify_available
        self._hosts = hosts
        self._refreshing: dict[UUID, asyncio.Lock] = {}
        self._previewing: set[UUID] = set()

    @property
    def enabled(self) -> bool:
        """Флаг `db.enabled`."""
        return self._settings.enabled

    @property
    def sample_rows_check(self) -> int:
        """Строк образца на таблицу при проверке запроса (`db.sample_rows_check`)."""
        return self._settings.sample_rows_check

    @property
    def sample_max_tables(self) -> int:
        """Таблиц образца в одной проверке блока (`db.sample_max_tables`)."""
        return self._settings.sample_max_tables

    def tls_verify_available(self) -> bool:
        """Режим `verify` доступен: корневой сертификат читается."""
        return self._tls_verify_available()

    # --- администратор ---

    async def _admin_view(self, uow: DbUnitOfWork, connection: Connection) -> AdminView:
        summary = await uow.db.summary(connection.id)
        return AdminView(
            connection,
            [] if connection.access_all else await uow.db.access_users(connection.id),
            summary,
            refresh_available_at(connection, summary, self._settings.snapshot_refresh_minutes),
        )

    async def admin_list(self) -> list[AdminView]:
        """Все подключения по названию."""
        async with self._uow_factory() as uow:
            return [await self._admin_view(uow, item) for item in await uow.db.list_all()]

    async def admin_get(self, connection_id: UUID) -> AdminView:
        """Подключение для администратора."""
        async with self._uow_factory() as uow:
            connection = await uow.db.get(connection_id)
            if connection is None:
                raise not_found()
            return await self._admin_view(uow, connection)

    def _validate(self, data: ConnectionInput, current: Connection | None) -> list[FieldError]:
        """Поля подключения по §7.6.2; `current` — подключение до `PATCH`, `None` у `POST`.

        Режим схем проверяется по итогу тела: у `PATCH` — с текущими значениями
        непереданных полей.
        """
        creating = current is None
        problems: list[FieldError] = []

        def check(name: str, code: str | None) -> None:
            if code == "host_forbidden":
                problems.append(field_error(name, code, _HOST_FORBIDDEN_MESSAGE))
            elif code is not None:
                problems.append(field_error(name, code))

        if creating or data.title is not UNSET:
            title = data.title.strip() if isinstance(data.title, str) else ""
            check(
                "title",
                "required"
                if not title
                else "too_long"
                if len(title) > _TITLE_MAX
                else "invalid_format"
                if _bad_text(title)
                else None,
            )
        if isinstance(data.description, str):
            check(
                "description",
                "too_long"
                if len(data.description) > _DESCRIPTION_MAX
                else "invalid_format"
                if _bad_text(data.description, allow_newline=True)
                else None,
            )
        if creating or data.host is not UNSET:
            check("host", _host_error(data.host, self._hosts))
        if data.port is not UNSET and not (isinstance(data.port, int) and 1 <= data.port <= 65535):
            check("port", "invalid_format")
        for name in ("database", "username"):
            if creating or getattr(data, name) is not UNSET:
                check(name, _name_error(getattr(data, name)))
        if creating or data.password is not UNSET:
            password = data.password
            check(
                "password",
                "required"
                if not isinstance(password, str) or not password
                else "too_long"
                if len(password) > _PASSWORD_MAX
                else None,
            )
        if creating or data.tls_mode is not UNSET:
            if data.tls_mode not in ("verify", "require", "disable"):
                check("tls_mode", "required")
            elif data.tls_mode == "verify" and not self.tls_verify_available():
                check("tls_mode", "invalid_format")
        all_schemas = _all_schemas(data, current)
        if all_schemas:
            if data.schemas is not UNSET and data.schemas != []:
                check("schemas", "invalid_format")
        elif data.schemas is not UNSET or current is None or current.all_schemas:
            # Переход к списку — только со списком: `required`, если его нет в теле.
            check("schemas", self._schemas_error(data.schemas))
        if data.sample_exclusions is not UNSET:
            schemas: Sequence[str] | None = None  # «все схемы»: любое корректное имя
            if not all_schemas:
                if isinstance(data.schemas, list):
                    schemas = data.schemas
                else:
                    schemas = [] if current is None else current.schemas
            check("sample_exclusions", self._exclusions_error(data.sample_exclusions, schemas))
        if creating or data.access is not UNSET:
            access = data.access
            if not isinstance(access, AccessInput) or (not access.all and not access.user_ids):
                check("access", "required")
            elif len(access.user_ids) > _ACCESS_USERS_MAX:
                check("access", "too_many")
            elif len(set(access.user_ids)) != len(access.user_ids):
                check("access", "invalid_format")
        return problems

    @staticmethod
    def _schemas_error(schemas: Any) -> str | None:
        if not isinstance(schemas, list) or not schemas:
            return "required"
        if len(schemas) > _SCHEMAS_MAX:
            return "too_many"
        if len(set(schemas)) != len(schemas) or any(_name_error(item) for item in schemas):
            return "invalid_format"
        return None

    @staticmethod
    def _exclusions_error(exclusions: Any, schemas: Sequence[str] | None) -> str | None:
        """Исключения образцов; `schemas` — итоговый список, `None` — «все схемы»."""
        if not isinstance(exclusions, list):
            return "invalid_format"
        if len(exclusions) > _EXCLUSIONS_MAX:
            return "too_many"
        keys = [(item["schema"], item["table"], item["column"]) for item in exclusions]
        if len(set(keys)) != len(keys):
            return "invalid_format"
        for schema, table, column in keys:
            if _name_error(table) or (column is not None and _name_error(column)):
                return "invalid_format"
            if schemas is None:
                # «Все схемы»: каталог базы при сохранении не читается — любое корректное имя.
                if _name_error(schema):
                    return "invalid_format"
            elif schema not in schemas:
                return "unknown_value"
        return None

    async def _check_users(self, uow: DbUnitOfWork, access: AccessInput) -> None:
        if access.all:
            return
        found = await uow.db.existing_users(access.user_ids)
        if len(found) != len(access.user_ids):
            raise validation_error([field_error("access", "unknown_value")])

    async def create(self, actor: CurrentUser, data: ConnectionInput) -> AdminView:
        """Новое подключение; права — `unknown` до первой проверки (§7.6.2)."""
        if problems := self._validate(data, None):
            raise validation_error(problems)
        all_schemas = _all_schemas(data, None)
        access: AccessInput = data.access
        now = self._clock.now()
        connection_id = uuid4()
        connection = Connection(
            id=connection_id,
            title=data.title.strip(),
            description="" if data.description is UNSET else data.description,
            host=data.host,
            port=5432 if data.port is UNSET else data.port,
            database=data.database,
            username=data.username,
            password_encrypted=self._cipher.encrypt(connection_id, data.password),
            tls_mode=data.tls_mode,
            all_schemas=all_schemas,
            schemas=[] if all_schemas else list(data.schemas),
            samples_allowed=False if data.samples_allowed is UNSET else data.samples_allowed,
            sample_exclusions=[] if data.sample_exclusions is UNSET else data.sample_exclusions,
            access_all=access.all,
            config_changed_at=now,
            rights_status="unknown",
            rights_findings=[],
            rights_warnings=[],
            rights_checked_at=None,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        async with self._uow_factory() as uow:
            await self._check_users(uow, access)
            if not await uow.db.add(connection):
                raise errors.title_taken()
            await uow.db.set_access(connection.id, [] if access.all else access.user_ids)
            await uow.audit.record(
                "db_connection_created",
                actor_id=actor.id,
                ip=actor.ip,
                details={"connection_id": str(connection.id)},
            )
            return await self._admin_view(uow, connection)

    async def update(
        self, actor: CurrentUser, connection_id: UUID, data: ConnectionInput
    ) -> AdminView:
        """Изменить поля; смена параметров входа сбрасывает права и ошибку (§7.6.2)."""
        given = data.given()
        if not given:
            raise validation_error([field_error("title", "required")])
        async with self._uow_factory() as uow:
            connection = await uow.db.get(connection_id, lock=True)
            if connection is None:
                raise not_found()
            if problems := self._validate(data, connection):
                raise validation_error(problems)
            changed: dict[str, Any] = {}
            for name in given:
                value = getattr(data, name)
                if name == "title":
                    changed["title"] = value.strip()
                elif name == "password":
                    changed["password_encrypted"] = self._cipher.encrypt(connection.id, value)
                elif name == "access":
                    await self._check_users(uow, value)
                    changed["access_all"] = value.all
                    await uow.db.set_access(connection.id, [] if value.all else value.user_ids)
                else:
                    changed[name] = value
            if _all_schemas(data, connection):
                changed["schemas"] = []
            now = self._clock.now()
            if any(name in LOGIN_FIELDS for name in given):
                changed.update(
                    config_changed_at=now,
                    rights_status="unknown",
                    rights_findings=[],
                    rights_warnings=[],
                    rights_checked_at=None,
                    last_error=None,
                )
            updated = replace(connection, **changed, updated_at=now)
            if not await uow.db.save(updated):
                raise errors.title_taken()
            await uow.audit.record(
                "db_connection_updated",
                actor_id=actor.id,
                ip=actor.ip,
                details={"connection_id": str(connection.id), "fields": sorted(given)},
            )
            return await self._admin_view(uow, updated)

    async def delete(self, actor: CurrentUser, connection_id: UUID) -> None:
        """Удалить подключение; выборы диалогов и проектов остаются с названием (§7.6.2)."""
        async with self._uow_factory() as uow:
            if not await uow.db.delete(connection_id):
                raise not_found()
            await uow.audit.record(
                "db_connection_deleted",
                actor_id=actor.id,
                ip=actor.ip,
                details={"connection_id": str(connection_id)},
            )

    async def check(self, actor: CurrentUser, connection_id: UUID) -> AdminView:
        """«Проверить подключение»: права и схемы; неудача — в `last_error`, ответ `200`."""
        async with self._uow_factory() as uow:
            connection = await uow.db.get(connection_id)
            if connection is None:
                raise not_found()
        rights: str | None = None
        reason: str | None = None
        fresh = True

        def note(written: bool) -> None:
            nonlocal fresh
            fresh = written

        try:
            report = await self.access(
                actor.id,
                connection,
                "check",
                lambda target: self._database.check(target),
                recorded=note,
            )
            rights = "excess" if report.findings else "read_only"
        except DbUnavailableError as error:
            reason = error.reason
            if error.report is not None:
                rights = "excess" if error.report.findings else "read_only"
        if not fresh:
            # Параметры входа сменили во время проверки: итог — о прежней учётной записи.
            rights = None
        async with self._uow_factory() as uow:
            details: dict[str, Any] = {"connection_id": str(connection_id), "rights": rights}
            if reason is not None:
                details["reason"] = reason
            await uow.audit.record(
                "db_connection_checked", actor_id=actor.id, ip=actor.ip, details=details
            )
            current = await uow.db.get(connection_id)
            if current is None:
                raise not_found()
            return await self._admin_view(uow, current)

    # --- обращение к базе ---

    def target(self, connection: Connection) -> DbTarget:
        """Параметры входа с паролем, расшифрованным на время вызова."""
        return DbTarget(
            connection.id,
            connection.host,
            connection.port,
            connection.database,
            connection.username,
            self._cipher.decrypt(connection.id, connection.password_encrypted),
            connection.tls_mode,
            list(connection.schemas),
            connection.all_schemas,
        )

    async def access[T](
        self,
        user_id: UUID,
        connection: Connection,
        purpose: Purpose,
        work: Callable[[DbTarget], Awaitable[T]],
        *,
        tables: int = 0,
        rows: Callable[[T], int] = lambda _: 0,
        recorded: Callable[[bool], None] = lambda _: None,
    ) -> T:
        """Одно обращение §7.6.5: шаг 0, порт, итог в строку подключения, журнал.

        Записанный `excess` — `DbRightsExcessError(None)` без соединения (кроме `check`).
        `recorded` узнаёт, записан ли итог (`False` — отброшен как устаревший).
        """
        started = time.monotonic()

        def log(outcome: str, rights: str | None, read_tables: int, read_rows: int) -> None:
            logger.info(
                "db access",
                extra={
                    "user_id": str(user_id),
                    "connection_id": str(connection.id),
                    "purpose": purpose,
                    "rights": rights,
                    "tables": read_tables,
                    "rows": read_rows,
                    "outcome": outcome,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )

        if connection.rights_status == "excess" and purpose != "check":
            log("excess_rights", None, 0, 0)
            raise DbRightsExcessError(None)
        report: RightsReport | None = None
        error: dict[str, Any] | None = None
        try:
            result = await work(self.target(connection))
        except DbRightsExcessError as excess:
            report = excess.report
            recorded(await self._record(connection, report, None))
            log("excess_rights", "excess", 0, 0)
            raise
        except DbUnavailableError as failure:
            report = failure.report
            error = {
                "reason": failure.reason,
                "details": failure.details,
                "at": self._clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            recorded(await self._record(connection, report, error))
            log(failure.reason, _status(report), 0, 0)
            raise
        report = result if isinstance(result, RightsReport) else _report_of(result)
        recorded(await self._record(connection, report, None))
        log(
            "excess_rights" if report is not None and report.findings else "ok",
            _status(report),
            tables,
            rows(result),
        )
        return result

    async def _record(
        self, connection: Connection, report: RightsReport | None, error: dict[str, Any] | None
    ) -> bool:
        """Записать итог; устаревший (параметры входа сменили во время обращения) — отбросить.

        `False` — итог отброшен.
        """
        rights = None
        if report is not None:
            rights = (
                _status(report) or "read_only",
                [item.data() for item in report.findings],
                [item.data() for item in report.warnings],
                self._clock.now(),
            )
        async with self._uow_factory() as uow:
            recorded = await uow.db.record(
                connection.id,
                rights=rights,
                last_error=error,
                config_changed_at=connection.config_changed_at,
            )
        if not recorded:
            logger.info("db access result stale", extra={"connection_id": str(connection.id)})
        return recorded

    # --- сотрудник ---

    @staticmethod
    async def _accessible(
        uow: DbUnitOfWork, user_id: UUID, connection_id: UUID
    ) -> Connection | None:
        """Подключение, доступное сотруднику; администратору — любое (условие в запросе)."""
        return await uow.db.get_for(user_id, connection_id)

    async def employee_list(self, user: CurrentUser) -> list[EmployeeView]:
        """Доступные подключения; администратору — все (§7.6.3)."""
        async with self._uow_factory() as uow:
            listed = await (uow.db.list_all() if user.role == "admin" else uow.db.list_for(user.id))
            views = []
            for connection in listed:
                summary = await uow.db.summary(connection.id)
                views.append(
                    EmployeeView(
                        connection,
                        summary,
                        refresh_available_at(
                            connection, summary, self._settings.snapshot_refresh_minutes
                        ),
                    )
                )
            return views

    async def snapshot(self, user: CurrentUser, connection_id: UUID) -> dict[str, Any]:
        """`DbSnapshot` доступного подключения."""
        async with self._uow_factory() as uow:
            connection = await self._accessible(uow, user.id, connection_id)
            if connection is None:
                raise not_found()
            snapshot = await uow.db.snapshot(connection_id)
        if snapshot is None:
            raise errors.snapshot_missing()
        return snapshot_body(connection, snapshot)

    async def refresh(self, user: CurrentUser, connection_id: UUID) -> dict[str, Any]:
        """«Обновить схему»: не чаще интервала, одно обновление подключения за раз (§7.6.3)."""
        async with self._uow_factory() as uow:
            if await self._accessible(uow, user.id, connection_id) is None:
                raise not_found()
        lock = self._refreshing.setdefault(connection_id, asyncio.Lock())
        async with lock:
            async with self._uow_factory() as uow:
                connection = await self._accessible(uow, user.id, connection_id)
                if connection is None:
                    raise not_found()
                summary = await uow.db.summary(connection_id)
            available_at = refresh_available_at(
                connection, summary, self._settings.snapshot_refresh_minutes
            )
            if available_at is not None and available_at > self._clock.now():
                raise errors.snapshot_too_soon(available_at)
            taken_at = self._clock.now()
            try:
                _, content = await self.access(
                    user.id,
                    connection,
                    "snapshot",
                    self._database.snapshot,
                )
            except DbRightsExcessError:
                raise errors.connection_unusable() from None
            except DbUnavailableError as error:
                raise errors.db_unavailable(error.reason, error.details or None) from None
            snapshot = StoredSnapshot(
                connection_id, taken_at, content.tables, content.truncated, content.postgis_schema
            )
            async with self._uow_factory() as uow:
                if await uow.db.get(connection_id) is None:
                    raise not_found()
                await uow.db.save_snapshot(snapshot)
                current = await uow.db.get(connection_id)
        return snapshot_body(current or connection, snapshot)

    # --- выбор у диалога и проекта (§7.6.9) ---

    async def select(self, user: CurrentUser, selection: SelectionInput | None) -> Selection | None:
        """Выбор из `PATCH`: флаг включён (иначе `403`), `null` — снять, иначе проверить."""
        if not self.enabled:
            raise feature_disabled()
        if selection is None:
            return None
        return await self.validate_selection(user, selection.connection_id, selection.tables)

    async def validate_selection(
        self, user: CurrentUser, connection_id: UUID, tables: Sequence[tuple[str, str]]
    ) -> Selection:
        """Новый выбор: подключение доступно, снимок есть, таблицы — из снимка."""
        async with self._uow_factory() as uow:
            connection = await self._accessible(uow, user.id, connection_id)
            if connection is None:
                raise errors.connection_not_found()
            snapshot = await uow.db.snapshot(connection_id)
        if snapshot is None:
            raise errors.snapshot_missing()
        if len(tables) > self._settings.max_selected_tables:
            raise validation_error([field_error("db.tables", "too_many")])
        if len(set(tables)) != len(tables):
            raise validation_error([field_error("db.tables", "invalid_format")])
        index = table_index(connection, snapshot)
        if any((schema, name) not in index for schema, name in tables):
            raise validation_error([field_error("db.tables", "unknown_value")])
        return Selection(connection.id, connection.title, tuple(tables))

    async def available(self, user_id: UUID, selection: Selection | None) -> bool:
        """Подключение выбора есть, доступно сотруднику и флаг включён."""
        if selection is None or selection.connection_id is None or not self.enabled:
            return False
        async with self._uow_factory() as uow:
            return await self._accessible(uow, user_id, selection.connection_id) is not None

    async def source(self, user_id: UUID, selection: Selection) -> SelectionSource | None:
        """Текущее подключение и снимок выбора; `None` — выбор недоступен."""
        if selection.connection_id is None or not self.enabled:
            return None
        async with self._uow_factory() as uow:
            connection = await self._accessible(uow, user_id, selection.connection_id)
            if connection is None:
                return None
            snapshot = await uow.db.snapshot(connection.id)
        if snapshot is None:
            snapshot = StoredSnapshot(connection.id, connection.created_at, [], False, None)
        return SelectionSource(selection, connection, snapshot)

    async def schema_text(self, user_id: UUID, selection: Selection) -> str | None:
        """DDL-подобный текст отмеченных таблиц по текущему снимку (§7.6.9).

        `None` — выбор недоступен или ни одной отмеченной таблицы в снимке нет: блока нет.
        """
        source = await self.source(user_id, selection)
        if source is None:
            return None
        tables = selected_tables(source.connection, source.snapshot, selection)
        return ddl_text(tables) if tables else None

    async def sample(
        self,
        user_id: UUID,
        connection: Connection,
        requests: Sequence[SampleRequest],
        *,
        preview: bool,
    ) -> Sequence[SampleRows]:
        """Образцы таблиц одним обращением (§7.6.8); ошибки — исключения порта.

        Слой предпросмотра (`preview`) — не больше одной загрузки у сотрудника (ПД §8.6
        п. 3): отметка ставится после шага 0, перед обращением к базе, и снимается по его
        концу при любом исходе; вторая — `429 db_sample_in_progress` без обращения к порту.
        """
        rows = self._settings.sample_rows_preview if preview else self._settings.sample_rows_check

        async def work(target: DbTarget) -> tuple[RightsReport, Sequence[SampleRows]]:
            if not preview:
                return await self._database.sample(target, requests, rows=rows, preview=False)
            if user_id in self._previewing:
                raise errors.sample_in_progress()
            self._previewing.add(user_id)
            try:
                return await self._database.sample(target, requests, rows=rows, preview=True)
            finally:
                self._previewing.discard(user_id)

        _, result = await self.access(
            user_id,
            connection,
            "sample_preview" if preview else "sample_check",
            work,
            tables=len(requests),
            rows=lambda answer: sum(len(item.rows) for item in answer[1]),
        )
        return result

    async def preview(
        self, user_id: UUID, selection: Selection, schema: str, table: str
    ) -> dict[str, Any]:
        """Слой предпросмотра из образца отмеченной таблицы (§7.5.18, §7.6.8).

        Выбор недоступен — `409 db_connection_unavailable`; таблица не отмечена или её нет в
        снимке — `validation_error` по `table`; образцы не разрешены или таблица исключена —
        `409 db_samples_not_allowed`; лишние права — `409 db_connection_unusable`; у
        сотрудника уже идёт загрузка слоя из образца — `429 db_sample_in_progress`.
        """
        source = await self.source(user_id, selection)
        if source is None:
            raise errors.connection_unavailable()
        found = None
        if (schema, table) in selection.tables:
            found = find_table(source.connection, source.snapshot, schema, table)
        if found is None:
            raise validation_error([field_error("table", "unknown_value")])
        plan = copy_plan(
            source.connection, found, exclusion_map(source.connection.sample_exclusions)
        )
        if sample_origin(source.connection, found, plan) is not None:
            raise errors.samples_not_allowed()
        spatial = next(
            (
                column
                for column in found["columns"]
                if column["geometry"] is not None and column["name"] in plan.columns
            ),
            None,
        )
        attributes = [name for name in plan.columns if spatial is None or name != spatial["name"]]
        request = SampleRequest(
            schema, table, attributes, None if spatial is None else spatial["name"]
        )
        try:
            (rows,) = await self.sample(user_id, source.connection, [request], preview=True)
        except DbRightsExcessError:
            raise errors.connection_unusable() from None
        except DbUnavailableError as error:
            raise errors.db_unavailable(error.reason, error.details or None) from None
        shapes = rows.geojson or [None] * len(rows.rows)
        features = [
            {
                "type": "Feature",
                "geometry": None if shape is None else json.loads(shape),
                "properties": dict(zip(attributes, values, strict=True)),
            }
            for values, shape in zip(rows.rows, shapes, strict=True)
        ]
        return {
            "connection_title": source.connection.title,
            "schema": schema,
            "table": table,
            "rows": len(rows.rows),
            "truncated": rows.truncated,
            "excluded_columns": len(plan.excluded),
            "geometry": None
            if spatial is None
            else {"column": spatial["name"], "type": spatial["geometry"]["type"]},
            "features": {"type": "FeatureCollection", "features": features},
        }


def _all_schemas(data: ConnectionInput, current: Connection | None) -> bool:
    """Итоговый режим «все схемы»: из тела, иначе текущий (у нового — список)."""
    if data.all_schemas is not UNSET:
        return bool(data.all_schemas)
    return current is not None and current.all_schemas


def _status(report: RightsReport | None) -> Literal["read_only", "excess"] | None:
    if report is None:
        return None
    return "excess" if report.findings else "read_only"


def _report_of(result: object) -> RightsReport | None:
    """Итог проверки прав из ответа порта: `(report, …)` или сам `RightsReport`."""
    if isinstance(result, tuple) and result and isinstance(result[0], RightsReport):
        return result[0]
    return None
