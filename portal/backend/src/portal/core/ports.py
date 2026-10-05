"""Порты ядра, которыми пользуются остальные модули (docs/portal-api.md §13.3)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

Role = Literal["admin", "employee"]
LoginStep = Literal["second_factor", "password_change", "second_factor_setup", "ready"]


class Clock(Protocol):
    """Источник текущего времени; в тестах подменяется."""

    def now(self) -> datetime:
        """Текущее время в UTC."""
        ...


class AuditLog(Protocol):
    """Журнал аудита; запись идёт в транзакции вызывающего сценария."""

    async def record(
        self,
        event: str,
        *,
        actor_id: UUID | None = None,
        subject_user_id: UUID | None = None,
        ip: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Добавить запись о событии."""
        ...


@dataclass(frozen=True)
class CurrentUser:
    """Результат проверки сессии на шаге `ready`."""

    id: UUID
    role: Role
    full_name: str
    ip: str | None


@dataclass(frozen=True)
class SessionInfo:
    """Действующая сессия на любом шаге входа."""

    session_id: UUID
    user_id: UUID
    role: Role
    full_name: str
    step: LoginStep


class SessionMissingError(Exception):
    """Сессии нет или она истекла."""

    def __init__(self, step: LoginStep | None = None) -> None:
        """Запомнить шаг, на котором была истёкшая сессия; `None` — сессия не найдена."""
        super().__init__("session missing")
        self.step = step


class SessionAuthenticator(Protocol):
    """Проверка сессии по значению cookie; реализует модуль входа."""

    async def authenticate(self, token: str | None) -> SessionInfo:
        """Вернуть действующую сессию или поднять `SessionMissingError`."""
        ...

    async def session_exists(self, session_id: UUID) -> bool:
        """Есть ли ещё сессия в базе: её удаление обрывает открытый поток (§2.6)."""
        ...
