"""Сборка зависимостей: единственное место, где порты связываются с реализациями."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from portal.auth.admin import AdminService
from portal.auth.crypto import Argon2PasswordHasher, HkdfSecretCipher
from portal.auth.repositories import SqlAuthUnitOfWorkFactory
from portal.auth.service import AuthService
from portal.auth.totp import PyotpTotpProvider
from portal.core.clock import SystemClock
from portal.core.db import create_engine
from portal.core.ports import Clock, SessionAuthenticator
from portal.core.settings import Settings


@dataclass(frozen=True)
class AdminContainer:
    """Зависимости команд на ВМ, которым не нужен `PORTAL_SECRET_KEY`."""

    settings: Settings
    engine: AsyncEngine
    clock: Clock
    admin: AdminService


@dataclass(frozen=True)
class Container(AdminContainer):
    """Зависимости процесса `portal-api`."""

    auth: AuthService
    authenticator: SessionAuthenticator


def build_admin_container(settings: Settings, clock: Clock | None = None) -> AdminContainer:
    """Собрать зависимости администрирования."""
    clock = clock or SystemClock()
    engine = create_engine(settings)
    admin = AdminService(
        SqlAuthUnitOfWorkFactory(engine, clock),
        Argon2PasswordHasher(settings.auth.password.argon2),
        clock,
        settings.auth.password,
    )
    return AdminContainer(settings=settings, engine=engine, clock=clock, admin=admin)


def build_container(settings: Settings, clock: Clock | None = None) -> Container:
    """Собрать все зависимости API; без `PORTAL_SECRET_KEY` сборка отказывает."""
    clock = clock or SystemClock()
    base = build_admin_container(settings, clock)
    auth = AuthService(
        SqlAuthUnitOfWorkFactory(base.engine, clock),
        Argon2PasswordHasher(settings.auth.password.argon2),
        HkdfSecretCipher(settings.require_secret_key()),
        PyotpTotpProvider(settings.auth.totp.issuer),
        clock,
        settings.auth,
        settings.common_passwords,
    )
    return Container(
        settings=settings,
        engine=base.engine,
        clock=clock,
        admin=base.admin,
        auth=auth,
        authenticator=auth,
    )
