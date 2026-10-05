"""Сценарии администрирования учётных записей (docs/portal-api.md §3, §13.4)."""

import asyncio
import secrets
from collections.abc import Sequence
from concurrent.futures import Executor
from uuid import UUID, uuid4

from portal.auth import errors
from portal.auth.domain import (
    FULL_NAME_MAX_LENGTH,
    LOGIN_PATTERN,
    AdminUser,
    User,
    login_throttle_key,
)
from portal.auth.ports import AuthUnitOfWork, AuthUnitOfWorkFactory, PasswordHasher
from portal.core.errors import field_error, not_found, validation_error
from portal.core.pagination import Page, PageQuery
from portal.core.ports import Clock, CurrentUser, Role
from portal.core.settings import PasswordSettings

# Без похожих на вид символов: пароль переписывают вручную.
_TEMPORARY_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_GROUP_LENGTH = 4
_MIN_GROUPS = 4
_CLI_DETAILS = {"via": "cli"}


def generate_temporary_password(min_length: int) -> str:
    """Случайный пароль группами по четыре символа через дефис, не короче `min_length`."""
    groups = max(_MIN_GROUPS, -(-min_length // _GROUP_LENGTH))
    return "-".join(
        "".join(secrets.choice(_TEMPORARY_PASSWORD_ALPHABET) for _ in range(_GROUP_LENGTH))
        for _ in range(groups)
    )


def _clean_full_name(full_name: str) -> str:
    cleaned = full_name.strip()
    if not cleaned:
        raise validation_error([field_error("full_name", "required")])
    if len(cleaned) > FULL_NAME_MAX_LENGTH:
        raise validation_error([field_error("full_name", "too_long")])
    return cleaned


class AdminService:
    """Действия администратора; `actor=None` — команда на ВМ."""

    def __init__(
        self,
        uow_factory: AuthUnitOfWorkFactory,
        hasher: PasswordHasher,
        hash_executor: Executor,
        clock: Clock,
        settings: PasswordSettings,
    ) -> None:
        """Получить зависимости явно; `hash_executor` — потоки под расчёт Argon2."""
        self._uow_factory = uow_factory
        self._hasher = hasher
        self._hash_executor = hash_executor
        self._clock = clock
        self._settings = settings

    async def list_users(self, query: PageQuery) -> Page[AdminUser]:
        """Страница учётных записей по ФИО."""
        if query.sort not in (None, "full_name"):
            raise validation_error([field_error("sort", "unknown_value")])
        async with self._uow_factory() as uow:
            users, total = await uow.users.search(
                query.q, query.order or "asc", query.offset, query.page_size
            )
            items = await self._views(uow, users)
        return Page(items, query.page, query.page_size, total)

    async def create_user(
        self, actor: CurrentUser | None, full_name: str, login: str, role: Role
    ) -> tuple[AdminUser, str]:
        """Создать учётную запись с временным паролем, который показывается один раз."""
        full_name = _clean_full_name(full_name)
        login = login.lower()
        if not LOGIN_PATTERN.fullmatch(login):
            raise validation_error([field_error("login", "invalid_format")])
        password = generate_temporary_password(self._settings.min_length)
        user = User(
            id=uuid4(),
            login=login,
            full_name=full_name,
            role=role,
            password_hash=await self._hash(password),
            must_change_password=True,
            is_blocked=False,
            totp_secret=None,
            totp_enabled=False,
            totp_last_step=None,
            last_login_at=None,
            created_at=self._clock.now(),
        )
        async with self._uow_factory() as uow:
            if not await uow.users.add(user):
                raise errors.login_taken()
            await self._audit(uow, "user_created", actor, user)
            return await self._view(uow, user), password

    async def update_user(
        self, actor: CurrentUser, user_id: UUID, full_name: str | None, role: Role | None
    ) -> AdminUser:
        """Изменить ФИО и (или) роль другого пользователя; смена роли гасит его сессии."""
        if full_name is not None:
            full_name = _clean_full_name(full_name)
        async with self._uow_factory() as uow:
            user = await self._other_user(uow, actor, user_id)
            changed: list[str] = []
            details: dict[str, object] = {"fields": changed}
            if full_name is not None and full_name != user.full_name:
                user.full_name = full_name
                changed.append("full_name")
            if role is not None and role != user.role:
                details |= {"role_before": user.role, "role_after": role}
                user.role = role
                changed.append("role")
                await uow.sessions.delete_for_user(user.id)
            if changed:
                await uow.users.update(user, *changed)
                await self._audit(uow, "user_updated", actor, user, details)
            return await self._view(uow, user)

    async def reset_password(self, actor: CurrentUser, user_id: UUID) -> tuple[AdminUser, str]:
        """Выдать новый временный пароль и погасить сессии пользователя."""
        password = generate_temporary_password(self._settings.min_length)
        password_hash = await self._hash(password)
        async with self._uow_factory() as uow:
            user = await self._other_user(uow, actor, user_id)
            user.password_hash = password_hash
            user.must_change_password = True
            await uow.users.update(user, "password_hash", "must_change_password")
            await uow.sessions.delete_for_user(user.id)
            await self._unlock_login(uow, user)
            await self._audit(uow, "password_reset", actor, user)
            return await self._view(uow, user), password

    async def reset_second_factor(self, actor: CurrentUser, user_id: UUID) -> AdminUser:
        """Сбросить второй фактор другого пользователя."""
        async with self._uow_factory() as uow:
            user = await self._other_user(uow, actor, user_id)
            await self._reset_second_factor(uow, actor, user)
            return await self._view(uow, user)

    async def reset_second_factor_by_login(self, login: str) -> User:
        """Сбросить второй фактор командой на ВМ; так его сбрасывают и администратору."""
        async with self._uow_factory() as uow:
            user = await uow.users.by_login(login.lower(), lock=True)
            if user is None:
                raise not_found()
            await self._reset_second_factor(uow, None, user)
            return user

    async def unlock_login(self, actor: CurrentUser, user_id: UUID) -> tuple[AdminUser, bool]:
        """Снять временную блокировку входа другого пользователя; `False` — её не было."""
        async with self._uow_factory() as uow:
            user = await self._other_user(uow, actor, user_id)
            return AdminUser(user, None), await self._unlock_locked_login(uow, actor, user)

    async def unlock_login_by_login(self, login: str) -> bool:
        """Снять блокировку входа командой на ВМ — так её снимают и администратору."""
        async with self._uow_factory() as uow:
            user = await uow.users.by_login(login.lower(), lock=True)
            if user is None:
                raise not_found()
            return await self._unlock_locked_login(uow, None, user)

    async def set_blocked(self, actor: CurrentUser, user_id: UUID, blocked: bool) -> AdminUser:
        """Заблокировать (сессии гасятся) или разблокировать пользователя."""
        async with self._uow_factory() as uow:
            user = (
                await self._other_user(uow, actor, user_id)
                if blocked
                else await self._user(uow, user_id)
            )
            if user.is_blocked != blocked:
                user.is_blocked = blocked
                await uow.users.update(user, "is_blocked")
                if blocked:
                    await uow.sessions.delete_for_user(user.id)
                await self._audit(uow, "user_blocked" if blocked else "user_unblocked", actor, user)
            return await self._view(uow, user)

    async def _hash(self, password: str) -> str:
        return await asyncio.get_running_loop().run_in_executor(
            self._hash_executor, self._hasher.hash, password
        )

    @staticmethod
    async def _user(uow: AuthUnitOfWork, user_id: UUID) -> User:
        # Строка держится до конца транзакции: решение и запись не расходятся с тем, что
        # в это же время делает сам пользователь (вход, смена пароля, второй фактор).
        user = await uow.users.get(user_id, lock=True)
        if user is None:
            raise not_found()
        return user

    async def _other_user(self, uow: AuthUnitOfWork, actor: CurrentUser, user_id: UUID) -> User:
        if user_id == actor.id:
            raise errors.cannot_modify_self()
        return await self._user(uow, user_id)

    async def _reset_second_factor(
        self, uow: AuthUnitOfWork, actor: CurrentUser | None, user: User
    ) -> None:
        if user.totp_secret is None and not user.totp_enabled:
            return
        user.totp_secret = None
        user.totp_enabled = False
        user.totp_last_step = None
        await uow.users.update(user, "totp_secret", "totp_enabled", "totp_last_step")
        await uow.backup_codes.delete_for_user(user.id)
        await uow.sessions.delete_for_user(user.id)
        await self._unlock_login(uow, user)
        await self._audit(uow, "second_factor_reset", actor, user)

    async def _views(self, uow: AuthUnitOfWork, users: Sequence[User]) -> list[AdminUser]:
        """Учётные записи с блокировками входа — одним запросом на всю страницу."""
        keys = {user.id: login_throttle_key(user.login) for user in users}
        locks = await uow.throttle.login_locks(list(keys.values()), self._clock.now())
        return [AdminUser(user, locks.get(keys[user.id])) for user in users]

    async def _view(self, uow: AuthUnitOfWork, user: User) -> AdminUser:
        return (await self._views(uow, [user]))[0]

    async def _unlock_locked_login(
        self, uow: AuthUnitOfWork, actor: CurrentUser | None, user: User
    ) -> bool:
        """Снять блокировку входа, если она действует; иначе ничего не менять (§3)."""
        if (await self._view(uow, user)).login_locked_until is None:
            return False
        await self._unlock_login(uow, user)
        await self._audit(uow, "login_unlocked", actor, user)
        return True

    @staticmethod
    async def _unlock_login(uow: AuthUnitOfWork, user: User) -> None:
        """Снять блокировку логина и обнулить счётчик неудач; счётчик адреса не трогается."""
        await uow.throttle.delete("login", login_throttle_key(user.login))

    @staticmethod
    async def _audit(
        uow: AuthUnitOfWork,
        event: str,
        actor: CurrentUser | None,
        user: User,
        details: dict[str, object] | None = None,
    ) -> None:
        await uow.audit.record(
            event,
            actor_id=actor.id if actor else None,
            subject_user_id=user.id,
            ip=actor.ip if actor else None,
            details=details if actor else _CLI_DETAILS,
        )
