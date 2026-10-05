"""Сборка зависимостей: единственное место, где порты связываются с реализациями."""

from dataclasses import dataclass

import httpx
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
from portal.dialogs.generation import GenerationService
from portal.dialogs.repositories import SqlDialogUnitOfWorkFactory
from portal.dialogs.service import DialogService
from portal.files.reader import ContentDocumentReader
from portal.files.storage import DiskFileStorage
from portal.llm.bifrost import BifrostChatModel
from portal.llm.estimator import RatioTokenEstimator
from portal.llm.ports import ChatModel


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
    http_client: httpx.AsyncClient
    dialogs: DialogService
    generation: GenerationService


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


def build_container(
    settings: Settings, clock: Clock | None = None, chat_model: ChatModel | None = None
) -> Container:
    """Собрать все зависимости API.

    Без `PORTAL_SECRET_KEY`, `PORTAL_LLM_API_KEY` и `MAX_MODEL_LEN` сборка отказывает
    (§13.5). `chat_model` подменяет клиент Bifrost в тестах.
    """
    clock = clock or SystemClock()
    secret_key = settings.require_secret_key()
    llm_api_key = settings.require_llm_api_key()
    max_model_len = settings.require_max_model_len()
    base = build_admin_container(settings, clock)
    auth = AuthService(
        SqlAuthUnitOfWorkFactory(base.engine, clock),
        Argon2PasswordHasher(settings.auth.password.argon2),
        HkdfSecretCipher(secret_key),
        PyotpTotpProvider(settings.auth.totp.issuer),
        clock,
        settings.auth,
        settings.common_passwords,
    )
    http_client = httpx.AsyncClient()
    storage = DiskFileStorage(settings.files.root)
    # Текст вложения длиннее всего контекста модели в запрос заведомо не поместится.
    text_max_chars = int(max_model_len * settings.llm.chars_per_token)
    reader = ContentDocumentReader(settings.files, settings.llm.image_max_side_px, text_max_chars)
    dialog_uow = SqlDialogUnitOfWorkFactory(base.engine)
    generation = GenerationService(
        dialog_uow,
        chat_model or BifrostChatModel(http_client, settings.llm, llm_api_key),
        RatioTokenEstimator(settings.llm.chars_per_token, settings.llm.tokens_per_image),
        storage,
        reader,
        auth,
        clock,
        settings,
        max_model_len,
    )
    return Container(
        settings=settings,
        engine=base.engine,
        clock=clock,
        admin=base.admin,
        auth=auth,
        authenticator=auth,
        http_client=http_client,
        dialogs=DialogService(
            dialog_uow,
            storage,
            reader,
            clock,
            settings.chat,
            settings.dialogs.empty_ttl_hours,
            text_max_chars,
            generation.is_forming,
        ),
        generation=generation,
    )
