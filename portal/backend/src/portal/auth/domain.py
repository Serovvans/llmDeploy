"""Сущности входа и правило шага входа (docs/portal-api.md §2.3, §9.1–9.4)."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from portal.core.ports import LoginStep, Role

ThrottleScope = Literal["login", "ip"]

LOGIN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{2,31}$")
FULL_NAME_MAX_LENGTH = 200
TOTP_PERIOD_SECONDS = 30


@dataclass
class User:
    """Учётная запись."""

    id: UUID
    login: str
    full_name: str
    role: Role
    password_hash: str
    must_change_password: bool
    is_blocked: bool
    totp_secret: bytes | None
    totp_enabled: bool
    totp_last_step: int | None
    last_login_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class AdminUser:
    """Учётная запись в ответах администратору: с временной блокировкой входа (§3)."""

    user: User
    login_locked_until: datetime | None


@dataclass
class Session:
    """Сессия; значение cookie хранится только хешем."""

    id: UUID
    user_id: UUID
    token_hash: bytes
    second_factor_passed: bool
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


@dataclass
class ThrottleEntry:
    """Счётчик неудачных попыток по логину или по адресу."""

    scope: ThrottleScope
    key: str
    failures: int
    first_failure_at: datetime
    last_failure_at: datetime
    locked_until: datetime | None


@dataclass(frozen=True)
class BackupCodes:
    """Остаток и общее число резервных кодов."""

    remaining: int
    total: int


@dataclass(frozen=True)
class SessionState:
    """Объект `Session` контракта и то, что нужно для cookie."""

    step: LoginStep
    user: User | None
    backup_codes: BackupCodes | None
    expires_at: datetime
    new_token: str | None = None


@dataclass(frozen=True)
class SecondFactorSetup:
    """Ключ для ручного ввода и QR-код в виде данных для `<path>`."""

    secret: str
    qr_size: int
    qr_path: str


def login_step(user: User, session: Session) -> LoginStep:
    """Вычислить текущий шаг входа; в сессии он не хранится."""
    if user.totp_enabled and not session.second_factor_passed:
        return "second_factor"
    if user.must_change_password:
        return "password_change"
    if not user.totp_enabled:
        return "second_factor_setup"
    return "ready"


def token_hash(token: str) -> bytes:
    """SHA-256 значения cookie."""
    return hashlib.sha256(token.encode()).digest()


def login_throttle_key(login: str) -> str:
    """Ключ счётчика логина: в базу не попадает то, что набрано в поле логина."""
    return hashlib.sha256(login.encode()).hexdigest()
