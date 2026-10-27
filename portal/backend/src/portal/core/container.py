"""Сборка зависимостей: единственное место, где порты связываются с реализациями."""

import asyncio
import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from portal.auth.admin import AdminService
from portal.auth.crypto import Argon2PasswordHasher, HkdfSecretCipher
from portal.auth.repositories import SqlAuthUnitOfWorkFactory
from portal.auth.service import AuthService
from portal.auth.totp import PyotpTotpProvider
from portal.code.assistant import CodeTool
from portal.code.edit import history_text
from portal.code.evaluation import CodeEvaluator
from portal.code.ports import CodeSandbox
from portal.code.reference import SdkIndex
from portal.code.repositories import SqlCodeUnitOfWorkFactory
from portal.code.sandbox import HttpCodeSandbox
from portal.code.service import CodeService
from portal.code.sql_check import SqlCheckService
from portal.code.templates import load_templates
from portal.code.web import ClientLibraries, load_web_assets
from portal.code.zipio import ZipProjectArchives
from portal.core.clock import SystemClock
from portal.core.db import create_engine
from portal.core.ports import Clock, SessionAuthenticator
from portal.core.settings import ConfigError, Settings
from portal.databases.crypto import AesPasswordCipher
from portal.databases.customer import AsyncpgCustomerDatabase, root_certificate_readable
from portal.databases.hosts import HostPolicy, IpNetwork
from portal.databases.network import SubnetsUnavailableError, interface_subnets
from portal.databases.ports import CustomerDatabase
from portal.databases.repositories import SqlDbUnitOfWorkFactory
from portal.databases.service import DbService
from portal.dialogs.domain import DialogKind
from portal.dialogs.drafts import SummaryDrafts
from portal.dialogs.export import DocxExporter
from portal.dialogs.files import AttachmentFiles
from portal.dialogs.generation import GenerationService, HistoryView
from portal.dialogs.ports import DialogTool
from portal.dialogs.repositories import SqlDialogUnitOfWorkFactory
from portal.dialogs.service import DialogService
from portal.dialogs.transcripts import SqlAttachmentTranscripts
from portal.files.media import DiskMediaStorage
from portal.files.ports import MediaStorage
from portal.files.reader import ContentDocumentReader
from portal.files.storage import DiskFileStorage
from portal.kb.embedder import BifrostEmbedder
from portal.kb.explaining import Contextualizer, FragmentExplainer
from portal.kb.images import ImageCollector
from portal.kb.indexing import Indexer
from portal.kb.ports import Embedder, KnowledgeBase
from portal.kb.qdrant import QdrantVectorIndex
from portal.kb.recognizer import ModelPageRecognizer
from portal.kb.recordings import RecordingProcessor
from portal.kb.reindex import Reindexer
from portal.kb.repositories import SqlKbUnitOfWorkFactory
from portal.kb.reranker import BifrostReranker
from portal.kb.retrieval import KnowledgeRetriever
from portal.kb.rewrite import QueryRewriter
from portal.kb.service import KbService
from portal.llm.bifrost import BifrostChatModel
from portal.llm.estimator import RatioTokenEstimator
from portal.llm.ports import ChatModel
from portal.media.ffmpeg import FfmpegAudioTools
from portal.media.files import stale_recording_keys
from portal.media.gigaam import GigaamSpeechRecognizer
from portal.media.ports import SpeechRecognizer
from portal.media.stub import StubSpeechRecognizer
from portal.media.transcription import SpeechPool
from portal.tools.dialog_tools import CogisTool, DocparseDialogTool, SqlTool
from portal.tools.docparse import DocparseService
from portal.tools.ports import SqlSandbox
from portal.tools.sandbox import AsyncpgSqlSandbox
from portal.tools.sql_repository import SqlSchemaStoreFactory
from portal.tools.sql_run import SqlTestRunner
from portal.tools.sql_schemas import SqlSchemaService
from portal.tools.sql_syntax import SqlSyntaxChecker
from portal.worker.loop import Worker
from portal.worker.ports import TranscriptionQueue
from portal.worker.queue import SqlTranscriptionQueue
from portal.worker.sweep import RecordingJanitor
from portal.worker.transcribe import AttachmentTranscriber


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
    drafts: SummaryDrafts
    sql_schemas: SqlSchemaService
    sql_checker: SqlSyntaxChecker
    docparse: DocparseService
    qdrant: AsyncQdrantClient
    kb: KbService
    knowledge: KnowledgeBase
    # Приём записей: резервы квоты живут в памяти этого объекта — он один на процесс (§1.6).
    media_storage: MediaStorage
    code: CodeService
    code_tool: CodeTool
    databases: DbService
    sql_tool: SqlTool
    # «Проверить запрос» проекта «Код» (§7.5.18).
    sql_check: SqlCheckService
    # Библиотеки клиента CoGIS и тело `GET /api/code/web` (§7.5.16).
    code_web: ClientLibraries


@dataclass(frozen=True)
class WorkerContainer:
    """Зависимости процесса `portal-worker` и команд базы знаний на ВМ."""

    settings: Settings
    engine: AsyncEngine
    http_client: httpx.AsyncClient
    qdrant: AsyncQdrantClient
    embedder: Embedder
    chat_model: ChatModel
    worker: Worker
    reindexer: Reindexer
    # Очереди распознавания речи: заданий базы знаний и места под вложения чата (§8.7).
    speech_pools: tuple[SpeechPool, ...]

    async def aclose(self) -> None:
        """Закрыть соединения с Bifrost, Qdrant и базой, остановить потоки распознавания."""
        for pool in self.speech_pools:
            pool.close()
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


def _knowledge(
    settings: Settings,
    engine: AsyncEngine,
    clock: Clock,
    http_client: httpx.AsyncClient,
    qdrant: AsyncQdrantClient,
    model: ChatModel,
) -> KnowledgeRetriever:
    """Поиск по базе знаний для ответов (§8.4)."""
    return KnowledgeRetriever(
        SqlKbUnitOfWorkFactory(engine, clock),
        _embedder(settings, http_client),
        QdrantVectorIndex(qdrant, settings.kb),
        QueryRewriter(model, settings.kb.rewrite),
        BifrostReranker(http_client, settings.kb.rerank, settings.require_rerank_api_key()),
        RatioTokenEstimator(settings.llm.chars_per_token, settings.llm.tokens_per_image),
        settings.kb,
    )


def _code(
    settings: Settings,
    engine: AsyncEngine,
    clock: Clock,
    http_client: httpx.AsyncClient,
    sql_schemas: SqlSchemaService,
    knowledge: KnowledgeBase,
    model: ChatModel,
    max_model_len: int,
    code_sandbox: CodeSandbox | None,
    databases: DbService | None,
) -> tuple[CodeService, CodeTool]:
    """Раздел «Код»: проекты и помощник; шаблоны мастера проверяются здесь (§7.5.4)."""
    code_settings = settings.code
    if code_settings.build.enabled:
        # При выключенном флаге сборки к песочнице портал не обращается вовсе (§7.5.15).
        code_sandbox = code_sandbox or HttpCodeSandbox(http_client, code_settings.build)
    else:
        code_sandbox = None
    code_uow = SqlCodeUnitOfWorkFactory(engine)
    code = CodeService(
        code_uow,
        code_sandbox,
        SdkIndex(code_settings),
        ZipProjectArchives(),
        sql_schemas,
        load_templates(
            code_settings.templates_dir, [package.id for package in code_settings.packages]
        ),
        clock,
        code_settings,
        web=load_web_assets(code_settings.web_dir),
        databases=databases,
        message_max_chars=settings.dialogs.message_max_chars,
        carried_max_chars=settings.memory.max_chars,
        zip_max_directory_bytes=settings.files.zip_max_directory_bytes,
    )
    estimator = RatioTokenEstimator(settings.llm.chars_per_token, settings.llm.tokens_per_image)
    tool = CodeTool(
        code_uow,
        code,
        sql_schemas,
        knowledge,
        model,
        estimator,
        settings,
        max_model_len,
        databases,
    )
    return code, tool


@dataclass(frozen=True)
class CodeEvalContainer:
    """Зависимости `portal eval-code`: те же объекты раздела «Код», что у `portal-api`."""

    settings: Settings
    engine: AsyncEngine
    http_client: httpx.AsyncClient
    qdrant: AsyncQdrantClient
    code: CodeService
    evaluator: CodeEvaluator

    async def aclose(self) -> None:
        """Закрыть соединения с Bifrost, песочницей, Qdrant и базой."""
        await self.http_client.aclose()
        await self.qdrant.close()
        await self.engine.dispose()


def build_code_eval_container(
    settings: Settings,
    chat_model: ChatModel | None = None,
    knowledge: KnowledgeBase | None = None,
    code_sandbox: CodeSandbox | None = None,
) -> CodeEvalContainer:
    """Собрать прогон контрольного набора; в базу он не пишет (§13.4).

    Нужны `PORTAL_LLM_API_KEY`, `QDRANT_API_KEY` и `MAX_MODEL_LEN` (§13.5). `chat_model`,
    `knowledge` и `code_sandbox` подменяют модель, поиск и песочницу в тестах.
    """
    clock = SystemClock()
    llm_api_key = settings.require_llm_api_key()
    max_model_len = settings.require_max_model_len()
    engine = create_engine(settings)
    http_client = httpx.AsyncClient()
    qdrant = _qdrant_client(settings)
    model = chat_model or BifrostChatModel(http_client, settings.llm, llm_api_key)
    knowledge = knowledge or _knowledge(settings, engine, clock, http_client, qdrant, model)
    sql_schemas = SqlSchemaService(SqlSchemaStoreFactory(engine), clock, settings.sql)
    code, tool = _code(
        settings,
        engine,
        clock,
        http_client,
        sql_schemas,
        knowledge,
        model,
        max_model_len,
        code_sandbox,
        None,
    )
    return CodeEvalContainer(
        settings=settings,
        engine=engine,
        http_client=http_client,
        qdrant=qdrant,
        code=code,
        evaluator=CodeEvaluator(tool, code, knowledge, model, settings.code),
    )


def _portal_subnets(settings: Settings) -> tuple[IpNetwork, ...]:
    """Подсети интерфейсов `portal-api` (§7.6.5, шаг 2); при `db.enabled` — обязательно."""
    try:
        return interface_subnets()
    except SubnetsUnavailableError as error:
        if settings.db.enabled:
            raise ConfigError(
                f"Не прочитались подсети интерфейсов портала ({error}): db.enabled недопустим"
            ) from None
        return ()


def build_container(
    settings: Settings,
    clock: Clock | None = None,
    chat_model: ChatModel | None = None,
    knowledge: KnowledgeBase | None = None,
    qdrant: AsyncQdrantClient | None = None,
    sql_sandbox: SqlSandbox | None = None,
    transcription_queue: TranscriptionQueue | None = None,
    code_sandbox: CodeSandbox | None = None,
    customer_database: CustomerDatabase | None = None,
    portal_subnets: Sequence[IpNetwork] | None = None,
) -> Container:
    """Собрать все зависимости API.

    Без `PORTAL_SECRET_KEY`, `PORTAL_LLM_API_KEY`, `QDRANT_API_KEY` и `MAX_MODEL_LEN`
    сборка отказывает (§13.5); при включённом `sql.sandbox.enabled` — и без паролей
    песочницы. `chat_model`, `knowledge`, `qdrant`, `sql_sandbox` и `transcription_queue`
    подменяют клиент Bifrost, базу знаний, клиент Qdrant, песочницу SQL и очередь
    расшифровки вложений в тестах; `code_sandbox` — песочницу сборки C#,
    `customer_database` — обращения к базам заказчика (§7.6), `portal_subnets` — подсети
    интерфейсов портала вместо прочитанных (§7.6.5, шаг 2). Шаблоны мастера
    читаются и проверяются здесь же: ошибка в них — отказ запуска (§7.5.6).
    """
    clock = clock or SystemClock()
    secret_key = settings.require_secret_key()
    llm_api_key = settings.require_llm_api_key()
    max_model_len = settings.require_max_model_len()
    qdrant = qdrant or _qdrant_client(settings)
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
    media_storage = DiskMediaStorage(settings.files.root, settings.media)
    attachment_files = AttachmentFiles(storage, media_storage)
    # Текст вложения длиннее всего контекста модели в запрос заведомо не поместится.
    text_max_chars = int(max_model_len * settings.llm.chars_per_token)
    reader = ContentDocumentReader(settings.files, settings.llm.image_max_side_px, text_max_chars)
    document_executor = ThreadPoolExecutor(
        max_workers=settings.files.reader_workers, thread_name_prefix="documents"
    )
    dialog_uow = SqlDialogUnitOfWorkFactory(base.engine)
    kb_uow = SqlKbUnitOfWorkFactory(base.engine, clock)
    estimator = RatioTokenEstimator(settings.llm.chars_per_token, settings.llm.tokens_per_image)
    vector_index = QdrantVectorIndex(qdrant, settings.kb)
    model = chat_model or BifrostChatModel(http_client, settings.llm, llm_api_key)
    knowledge = knowledge or KnowledgeRetriever(
        kb_uow,
        _embedder(settings, http_client),
        vector_index,
        QueryRewriter(model, settings.kb.rewrite),
        BifrostReranker(http_client, settings.kb.rerank, settings.require_rerank_api_key()),
        estimator,
        settings.kb,
    )
    sql_schemas = SqlSchemaService(SqlSchemaStoreFactory(base.engine), clock, settings.sql)
    hosts = HostPolicy.of(
        settings.db.forbidden_hosts,
        settings.db.allowed_internal_hosts,
        _portal_subnets(settings) if portal_subnets is None else portal_subnets,
    )
    databases = DbService(
        SqlDbUnitOfWorkFactory(base.engine, clock),
        customer_database or AsyncpgCustomerDatabase(settings.db, hosts),
        AesPasswordCipher(secret_key),
        clock,
        settings.db,
        partial(root_certificate_readable, settings.db.tls_root_cert_file),
        hosts,
    )
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
    sql_runner = None
    if settings.sql.sandbox.enabled:
        # При выключенном флаге песочницы у портала нет вовсе, и её пароли не нужны (§7.4).
        sql_sandbox = sql_sandbox or AsyncpgSqlSandbox(
            settings.sql.sandbox, *settings.require_sql_sandbox_passwords()
        )
        sql_runner = SqlTestRunner(sql_sandbox, model, sql_checker, settings.sql.sandbox)
    code, code_tool = _code(
        settings,
        base.engine,
        clock,
        http_client,
        sql_schemas,
        knowledge,
        model,
        max_model_len,
        code_sandbox,
        databases,
    )
    sql_tool = SqlTool(
        sql_schemas,
        sql_checker,
        settings.sql,
        sql_runner,
        databases=databases,
        dialogs=dialog_uow,
    )
    tools: dict[DialogKind, DialogTool] = {
        "sql": sql_tool,
        "cogis": CogisTool(knowledge, settings.cogis),
        "docparse": DocparseDialogTool(dialog_uow, settings.docparse),
        "code": code_tool,
    }
    history_views: dict[DialogKind, HistoryView] = {"code": history_text}
    generation = GenerationService(
        dialog_uow,
        model,
        estimator,
        storage,
        attachment_files,
        reader,
        document_executor,
        auth,
        knowledge,
        tools,
        docparse.is_forming,
        clock,
        settings,
        max_model_len,
        editors={"code": code_tool},
        history_views=history_views,
        rechecker=sql_tool,
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
            attachment_files,
            transcription_queue or SqlTranscriptionQueue(base.engine, clock),
            auth,
            reader,
            document_executor,
            clock,
            settings.chat,
            settings.dialogs.empty_ttl_hours,
            text_max_chars,
            settings.memory.max_chars,
            generation.is_forming,
            docparse.is_forming,
            DocxExporter(),
            settings.code.enabled,
            databases,
        ),
        sql_schemas=sql_schemas,
        sql_checker=sql_checker,
        docparse=docparse,
        generation=generation,
        drafts=SummaryDrafts(
            dialog_uow,
            model,
            estimator,
            auth,
            generation.wait_idle,
            settings,
            max_model_len,
            history_views,
        ),
        qdrant=qdrant,
        kb=KbService(
            kb_uow,
            storage,
            media_storage,
            reader,
            vector_index,
            auth,
            document_executor,
            clock,
            settings.kb,
            settings.media.min_free_bytes,
        ),
        knowledge=knowledge,
        media_storage=media_storage,
        code=code,
        code_tool=code_tool,
        databases=databases,
        sql_tool=sql_tool,
        sql_check=SqlCheckService(
            code,
            sql_schemas,
            sql_tool,
            sql_runner,
            databases,
            auth,
            dialect=settings.sql.sandbox.dialect,
            sql_max_chars=settings.dialogs.message_max_chars,
            recheck_seconds=settings.auth.session.stream_recheck_seconds,
        ),
        code_web=ClientLibraries(settings.code.sdk_index_dir / "client", code.web),
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
    (§13.5). `chat_model` и `qdrant` подменяют клиент Bifrost и клиент Qdrant в тестах;
    описание картинок идёт через клиент Bifrost (`CompletionModel`, §12.1).
    """
    clock = clock or SystemClock()
    llm_api_key = settings.require_llm_api_key()
    qdrant = qdrant or _qdrant_client(settings)
    engine = create_engine(settings)
    http_client = httpx.AsyncClient()
    kb_uow = SqlKbUnitOfWorkFactory(engine, clock)
    vector_index = QdrantVectorIndex(qdrant, settings.kb)
    embedder = _embedder(settings, http_client)
    # Запросы воркера без потока ждут ответа дольше, чем в portal-api: ответ приходит в
    # конце генерации, а описание и распознавание — до 4096 токенов (§12.1).
    bifrost = BifrostChatModel(
        http_client,
        settings.llm,
        llm_api_key,
        timeout_seconds=settings.llm.background_timeout_seconds,
    )
    model = chat_model or bifrost
    recognizer = ModelPageRecognizer(model, settings.llm)
    # Читатель отдаёт на символ и на картинку больше предела: так превышение отличимо от
    # ровного попадания.
    reader = ContentDocumentReader(
        settings.files,
        settings.llm.image_max_side_px,
        settings.kb.indexing.document_max_chars + 1,
        settings.kb.images.max_per_document + 1,
    )
    storage = DiskFileStorage(settings.files.root)
    media = settings.media
    media_storage = DiskMediaStorage(settings.files.root, media)
    audio = FfmpegAudioTools(media)
    speech: SpeechRecognizer = (
        StubSpeechRecognizer()
        if media.asr.engine == "stub"
        else GigaamSpeechRecognizer(media.asr.model_dir)
    )
    # Потоки распознавания речи общие на воркер: место под вложения чата берёт из них
    # `chat_threads`, остальное — задания базы знаний (§8.7).
    kb_speech = SpeechPool(speech, media.asr.threads - media.asr.chat_threads)
    chat_speech = SpeechPool(speech, media.asr.chat_threads)
    # Один на процесс: предел фоновых обращений к основной модели общий на весь воркер.
    background_llm = asyncio.Semaphore(settings.kb.worker.background_llm_parallel)
    indexer = Indexer(
        kb_uow,
        storage,
        media_storage,
        reader,
        recognizer,
        ImageCollector(
            kb_uow,
            storage,
            reader,
            bifrost,
            background_llm,
            settings.kb.images,
            settings.llm,
            settings.kb.worker.background_llm_parallel,
        ),
        RecordingProcessor(kb_uow, media_storage, audio, kb_speech, media),
        embedder,
        vector_index,
        clock,
        settings.kb,
        # Столько страниц-сканов распознаётся одновременно: больше растров держать незачем.
        settings.llm.recognition_parallel_requests,
    )
    explainer = FragmentExplainer(
        model,
        RatioTokenEstimator(settings.llm.chars_per_token, settings.llm.tokens_per_image),
        settings.kb.context,
        background_llm,
    )
    contextualizer = Contextualizer(kb_uow, explainer, embedder, vector_index, settings.kb)
    transcripts = SqlAttachmentTranscripts(engine)
    worker = Worker(
        kb_uow,
        indexer,
        contextualizer,
        AttachmentTranscriber(kb_uow, transcripts, media_storage, audio, chat_speech, clock, media),
        transcripts,
        media_storage,
        RecordingJanitor(
            kb_uow,
            media_storage,
            transcripts,
            partial(stale_recording_keys, settings.files.root),
            clock,
            media,
        ),
        vector_index,
        clock,
        settings.kb.worker,
    )
    return WorkerContainer(
        settings=settings,
        engine=engine,
        http_client=http_client,
        qdrant=qdrant,
        embedder=embedder,
        chat_model=model,
        worker=worker,
        reindexer=Reindexer(kb_uow, vector_index, clock),
        speech_pools=(kb_speech, chat_speech),
    )
