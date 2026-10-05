"""Сборка зависимостей: единственное место, где порты связываются с реализациями."""

import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
from qdrant_client import AsyncQdrantClient
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
from portal.dialogs.domain import DialogKind
from portal.dialogs.export import DocxExporter
from portal.dialogs.generation import GenerationService
from portal.dialogs.ports import DialogTool
from portal.dialogs.repositories import SqlDialogUnitOfWorkFactory
from portal.dialogs.service import DialogService
from portal.files.reader import ContentDocumentReader
from portal.files.storage import DiskFileStorage
from portal.kb.embedder import BifrostEmbedder
from portal.kb.indexing import Indexer
from portal.kb.ports import Embedder, KnowledgeBase
from portal.kb.qdrant import QdrantVectorIndex
from portal.kb.recognizer import ModelPageRecognizer
from portal.kb.reindex import Reindexer
from portal.kb.repositories import SqlKbUnitOfWorkFactory
from portal.kb.retrieval import KnowledgeRetriever
from portal.kb.service import KbService
from portal.llm.bifrost import BifrostChatModel
from portal.llm.estimator import RatioTokenEstimator
from portal.llm.ports import ChatModel
from portal.tools.dialog_tools import CogisTool, DocparseDialogTool, SqlTool
from portal.tools.docparse import DocparseService
from portal.tools.sql_repository import SqlSchemaStoreFactory
from portal.tools.sql_schemas import SqlSchemaService
from portal.tools.sql_syntax import SqlSyntaxChecker
from portal.worker.loop import Worker


@dataclass(frozen=True)
class AdminContainer:
    """Зависимости команд на ВМ, которым не нужен `PORTAL_SECRET_KEY`."""

    settings: Settings
    engine: AsyncEngine
    clock: Clock
    admin: AdminService
    # Потоки под Argon2, отдельные от общего пула цикла событий; закрывает владелец контейнера.
    hash_executor: ThreadPoolExecutor


@dataclass(frozen=True)
class Container(AdminContainer):
    """Зависимости процесса `portal-api`."""

    # Потоки чтения документов, отдельные от общего пула цикла событий: очередь к PDFium
    # не занимает потоки, нужные разрешению имён, записи файлов и экспорту.
    document_executor: ThreadPoolExecutor
    auth: AuthService
    authenticator: SessionAuthenticator
    http_client: httpx.AsyncClient
    dialogs: DialogService
    generation: GenerationService
    sql_schemas: SqlSchemaService
    sql_checker: SqlSyntaxChecker
    docparse: DocparseService
    qdrant: AsyncQdrantClient
    kb: KbService
    knowledge: KnowledgeBase


@dataclass(frozen=True)
class WorkerContainer:
    """Зависимости процесса `portal-worker` и команд базы знаний на ВМ."""

    settings: Settings
    engine: AsyncEngine
    http_client: httpx.AsyncClient
    qdrant: AsyncQdrantClient
    embedder: Embedder
    worker: Worker
    reindexer: Reindexer

    async def aclose(self) -> None:
        """Закрыть соединения с Bifrost, Qdrant и базой."""
        await self.http_client.aclose()
        await self.qdrant.close()
        await self.engine.dispose()


def _qdrant_client(settings: Settings) -> AsyncQdrantClient:
    """Клиент Qdrant; соединение открывается при первом запросе, а не здесь."""
    qdrant = settings.kb.qdrant
    api_key = settings.require_qdrant_api_key()
    with warnings.catch_warnings():
        # Qdrant доступен только во внутренней docker-сети `portal`, без TLS — так задумано
        # (docs/portal-design.md §3); предупреждение клиента об этом в журнале не нужно.
        warnings.filterwarnings("ignore", message="Api key is used with an insecure connection")
        return AsyncQdrantClient(
            url=qdrant.url,
            api_key=api_key,
            timeout=qdrant.timeout_seconds,
            # Версию сервера закрепляет compose; проверка при создании клиента — лишний запрос.
            check_compatibility=False,
        )


def _embedder(settings: Settings, http_client: httpx.AsyncClient) -> BifrostEmbedder:
    return BifrostEmbedder(
        http_client,
        settings.llm.base_url,
        settings.llm.embedding_model,
        settings.require_llm_api_key(),
        settings.kb.embeddings,
    )


def build_admin_container(settings: Settings, clock: Clock | None = None) -> AdminContainer:
    """Собрать зависимости администрирования."""
    clock = clock or SystemClock()
    engine = create_engine(settings)
    argon2 = settings.auth.password.argon2
    hash_executor = ThreadPoolExecutor(max_workers=argon2.workers, thread_name_prefix="argon2")
    admin = AdminService(
        SqlAuthUnitOfWorkFactory(engine, clock),
        Argon2PasswordHasher(argon2),
        hash_executor,
        clock,
        settings.auth.password,
    )
    return AdminContainer(
        settings=settings, engine=engine, clock=clock, admin=admin, hash_executor=hash_executor
    )


def build_container(
    settings: Settings,
    clock: Clock | None = None,
    chat_model: ChatModel | None = None,
    knowledge: KnowledgeBase | None = None,
) -> Container:
    """Собрать все зависимости API.

    Без `PORTAL_SECRET_KEY`, `PORTAL_LLM_API_KEY`, `QDRANT_API_KEY` и `MAX_MODEL_LEN`
    сборка отказывает (§13.5). `chat_model` и `knowledge` подменяют клиент Bifrost и
    базу знаний в тестах.
    """
    clock = clock or SystemClock()
    secret_key = settings.require_secret_key()
    llm_api_key = settings.require_llm_api_key()
    max_model_len = settings.require_max_model_len()
    qdrant = _qdrant_client(settings)
    base = build_admin_container(settings, clock)
    auth = AuthService(
        SqlAuthUnitOfWorkFactory(base.engine, clock),
        Argon2PasswordHasher(settings.auth.password.argon2),
        base.hash_executor,
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
    document_executor = ThreadPoolExecutor(
        max_workers=settings.files.reader_workers, thread_name_prefix="documents"
    )
    dialog_uow = SqlDialogUnitOfWorkFactory(base.engine)
    kb_uow = SqlKbUnitOfWorkFactory(base.engine, clock)
    estimator = RatioTokenEstimator(settings.llm.chars_per_token, settings.llm.tokens_per_image)
    knowledge = knowledge or KnowledgeRetriever(
        kb_uow,
        _embedder(settings, http_client),
        QdrantVectorIndex(qdrant, settings.kb),
        estimator,
        settings.kb,
    )
    model = chat_model or BifrostChatModel(http_client, settings.llm, llm_api_key)
    sql_schemas = SqlSchemaService(SqlSchemaStoreFactory(base.engine), clock, settings.sql)
    sql_checker = SqlSyntaxChecker(settings.sql.check_max_chars, settings.sql.check_timeout_seconds)
    docparse = DocparseService(
        dialog_uow,
        model,
        ModelPageRecognizer(model, settings.llm),
        estimator,
        storage,
        reader,
        document_executor,
        auth,
        clock,
        settings,
        max_model_len,
    )
    tools: dict[DialogKind, DialogTool] = {
        "sql": SqlTool(sql_schemas, sql_checker, settings.sql),
        "cogis": CogisTool(knowledge, settings.cogis),
        "docparse": DocparseDialogTool(dialog_uow, settings.docparse),
    }
    generation = GenerationService(
        dialog_uow,
        model,
        estimator,
        storage,
        reader,
        document_executor,
        auth,
        knowledge,
        tools,
        docparse.is_forming,
        clock,
        settings,
        max_model_len,
    )
    return Container(
        settings=settings,
        engine=base.engine,
        clock=clock,
        admin=base.admin,
        hash_executor=base.hash_executor,
        document_executor=document_executor,
        auth=auth,
        authenticator=auth,
        http_client=http_client,
        dialogs=DialogService(
            dialog_uow,
            storage,
            reader,
            document_executor,
            clock,
            settings.chat,
            settings.dialogs.empty_ttl_hours,
            text_max_chars,
            generation.is_forming,
            docparse.is_forming,
            DocxExporter(),
        ),
        sql_schemas=sql_schemas,
        sql_checker=sql_checker,
        docparse=docparse,
        generation=generation,
        qdrant=qdrant,
        kb=KbService(kb_uow, storage, reader, document_executor, clock, settings.kb),
        knowledge=knowledge,
    )


def build_worker_container(
    settings: Settings,
    clock: Clock | None = None,
    chat_model: ChatModel | None = None,
    qdrant: AsyncQdrantClient | None = None,
) -> WorkerContainer:
    """Собрать зависимости воркера и команд базы знаний.

    Без `PORTAL_LLM_API_KEY` и `QDRANT_API_KEY` сборка отказывает; контекст модели
    (`MAX_MODEL_LEN`) воркеру не нужен
    (§13.5). `chat_model` и `qdrant` подменяют клиент Bifrost и клиент Qdrant в тестах.
    """
    clock = clock or SystemClock()
    llm_api_key = settings.require_llm_api_key()
    qdrant = qdrant or _qdrant_client(settings)
    engine = create_engine(settings)
    http_client = httpx.AsyncClient()
    kb_uow = SqlKbUnitOfWorkFactory(engine, clock)
    vector_index = QdrantVectorIndex(qdrant, settings.kb)
    embedder = _embedder(settings, http_client)
    recognizer = ModelPageRecognizer(
        chat_model or BifrostChatModel(http_client, settings.llm, llm_api_key), settings.llm
    )
    # Читатель отдаёт на символ больше предела: так превышение отличимо от ровного попадания.
    reader = ContentDocumentReader(
        settings.files, settings.llm.image_max_side_px, settings.kb.indexing.document_max_chars + 1
    )
    indexer = Indexer(
        kb_uow,
        DiskFileStorage(settings.files.root),
        reader,
        recognizer,
        embedder,
        vector_index,
        settings.kb,
        # Столько страниц-сканов распознаётся одновременно: больше растров держать незачем.
        settings.llm.recognition_parallel_requests,
    )
    return WorkerContainer(
        settings=settings,
        engine=engine,
        http_client=http_client,
        qdrant=qdrant,
        embedder=embedder,
        worker=Worker(kb_uow, indexer, vector_index, clock, settings.kb.worker),
        reindexer=Reindexer(kb_uow, vector_index, clock),
    )
