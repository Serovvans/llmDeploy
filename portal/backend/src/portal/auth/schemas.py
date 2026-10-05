"""Тела запросов и ответов входа и администрирования (docs/portal-api.md §2, §3)."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from portal.auth.domain import AdminUser, SessionState
from portal.core.ports import LoginStep, Role
from portal.core.schemas import ApiTime, RequestModel


class LoginRequest(RequestModel):
    """`POST /api/auth/login`."""

    login: str
    password: str


class SecondFactorRequest(RequestModel):
    """`POST /api/auth/second-factor`: ровно одно из полей."""

    code: str | None = None
    backup_code: str | None = None


class PasswordRequest(RequestModel):
    """`POST /api/auth/password`."""

    new_password: str
    current_password: str | None = None


class ConfirmRequest(RequestModel):
    """`POST /api/auth/second-factor/confirm`."""

    code: str


class CreateUserRequest(RequestModel):
    """`POST /api/admin/users`."""

    full_name: str
    login: str
    role: Role


class UpdateUserRequest(RequestModel):
    """`PATCH /api/admin/users/{id}`: хотя бы одно поле."""

    full_name: str | None = None
    role: Role | None = None


class BackupCodesOut(BaseModel):
    """Остаток резервных кодов."""

    remaining: int
    total: int


class SessionUserOut(BaseModel):
    """Пользователь в объекте `Session`."""

    id: UUID
    login: str
    full_name: str
    role: Role
    second_factor_configured: bool
    backup_codes: BackupCodesOut | None


class SessionOut(BaseModel):
    """Объект `Session`."""

    step: LoginStep
    user: SessionUserOut | None

    @classmethod
    def of(cls, state: SessionState) -> "SessionOut":
        """Собрать из состояния сессии."""
        user = state.user
        if user is None:
            return cls(step=state.step, user=None)
        codes = state.backup_codes
        return cls(
            step=state.step,
            user=SessionUserOut(
                id=user.id,
                login=user.login,
                full_name=user.full_name,
                role=user.role,
                second_factor_configured=user.totp_enabled,
                backup_codes=BackupCodesOut(remaining=codes.remaining, total=codes.total)
                if codes
                else None,
            ),
        )


class SecondFactorOut(BaseModel):
    """Ответ на ввод кода."""

    session: SessionOut
    backup_code_used: bool


class QrOut(BaseModel):
    """QR-код данными для `<path>`."""

    size: int
    path: str


class SecondFactorSetupOut(BaseModel):
    """Объект `SecondFactorSetup`."""

    secret: str
    qr: QrOut


class ConfirmOut(BaseModel):
    """Ответ на подтверждение настройки второго фактора."""

    session: SessionOut
    backup_codes: list[str]


class AdminUserOut(BaseModel):
    """Объект `AdminUser`."""

    id: UUID
    login: str
    full_name: str
    role: Role
    state: Literal["blocked", "never_logged_in", "active"]
    login_locked_until: ApiTime | None
    second_factor_configured: bool
    is_me: bool
    created_at: ApiTime

    @classmethod
    def of(cls, admin_user: AdminUser, current_user_id: UUID) -> "AdminUserOut":
        """Собрать из учётной записи."""
        user = admin_user.user
        state: Literal["blocked", "never_logged_in", "active"] = "active"
        if user.is_blocked:
            state = "blocked"
        elif user.last_login_at is None:
            state = "never_logged_in"
        return cls(
            id=user.id,
            login=user.login,
            full_name=user.full_name,
            role=user.role,
            state=state,
            login_locked_until=admin_user.login_locked_until,
            second_factor_configured=user.totp_enabled,
            is_me=user.id == current_user_id,
            created_at=user.created_at,
        )


class AdminUserWithPasswordOut(BaseModel):
    """Учётная запись и временный пароль, который показывается один раз."""

    user: AdminUserOut
    temporary_password: str


class LoginUnlockOut(BaseModel):
    """Ответ на снятие блокировки входа: `unlocked` — была ли она снята сейчас."""

    user: AdminUserOut
    unlocked: bool


class AdminUserPageOut(BaseModel):
    """Страница учётных записей."""

    items: list[AdminUserOut]
    page: int
    page_size: int
    total: int
