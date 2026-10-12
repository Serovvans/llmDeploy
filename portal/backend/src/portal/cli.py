"""Точка входа команд `portal` (docs/portal-api.md §13.4)."""

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

from portal.core.app import create_app
from portal.core.audit import read_audit
from portal.core.clock import SystemClock
from portal.core.container import (
    AdminContainer,
    WorkerContainer,
    build_admin_container,
    build_container,
    build_worker_container,
)
from portal.core.db import create_engine, ping
from portal.core.errors import AppError
from portal.core.logging import configure_logging
from portal.core.settings import ConfigError, Settings, load_settings
from portal.kb import evalset
from portal.kb.evaluation import (
    FEATURES,
    EvalPorts,
    Feature,
    evaluate,
    format_report,
    idle_features,
)
from portal.kb.explaining import FragmentExplainer
from portal.kb.ports import (
    CollectionMismatchError,
    EmbeddingsUnavailableError,
    VectorIndexUnavailableError,
)
from portal.kb.qdrant import QdrantVectorIndex
from portal.kb.reindex import WorkerRunningError
from portal.kb.repositories import SqlKbUnitOfWorkFactory
from portal.kb.reprocess import Reprocessor
from portal.kb.reranker import BifrostReranker
from portal.kb.rewrite import QueryRewriter
from portal.llm.estimator import RatioTokenEstimator
from portal.llm.ports import ModelOverloadedError, ModelUnavailableError
from portal.media.gigaam import SpeechWeightsMissingError, require_weights
from portal.worker.health import heartbeat_is_fresh

_ALEMBIC_INI = Path("alembic.ini")
_API_HOST = "0.0.0.0"  # контейнер без портов на хост, слушает сеть compose
_API_PORT = 8000
_KB_COMMANDS = frozenset({"worker", "reindex", "eval-search"})


def migrate(settings: Settings) -> None:
    """Применить миграции."""
    config = Config(str(_ALEMBIC_INI))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")


def _serve(settings: Settings) -> None:
    """Запустить API одним процессом (§13.5); адрес клиента берёт само приложение."""
    configure_logging()
    uvicorn.run(
        create_app(build_container(settings)),
        host=_API_HOST,
        port=_API_PORT,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


async def _create_admin(container: AdminContainer, login: str, full_name: str) -> str:
    _, password = await container.admin.create_user(None, full_name, login, "admin")
    return f"Администратор создан. Временный пароль: {password}\n"


async def _reset_second_factor(container: AdminContainer, login: str) -> str:
    await container.admin.reset_second_factor_by_login(login)
    return "Второй фактор сброшен: при следующем входе настройка пройдёт заново.\n"


async def _unlock_login(container: AdminContainer, login: str) -> str:
    if await container.admin.unlock_login_by_login(login):
        return "Блокировка входа снята: счётчик неудач обнулён.\n"
    return "Блокировки входа нет: ничего не изменено.\n"


async def _audit(
    container: AdminContainer, since: datetime | None, event: str | None, limit: int
) -> str:
    async with container.engine.connect() as connection:
        rows = await read_audit(connection, since=since, event=event, limit=limit)
    lines = [
        "\t".join(
            [
                row.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                row.event,
                row.actor_login or "-",
                row.subject_login or "-",
                str(row.ip or "-"),
                json.dumps(row.details, ensure_ascii=False),
            ]
        )
        for row in rows
    ]
    return "".join(f"{line}\n" for line in lines)


async def _worker(container: WorkerContainer) -> None:
    """Цикл очереди до сигнала остановки; взятые задания возвращаются в очередь.

    Записи включены, а весов модели речи нет — воркер не стартует (§8.7).
    """
    media = container.settings.media
    if media.enabled and media.asr.engine == "gigaam":
        require_weights(media.asr.model_dir)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await container.worker.run(stop)


async def _worker_health(settings: Settings) -> str | None:
    """Причина, по которой воркер не здоров; `None` — цикл жив и база отвечает."""
    worker = settings.kb.worker
    if not heartbeat_is_fresh(worker.heartbeat_file, worker.heartbeat_stale_seconds):
        return "Цикл воркера не обновлял отметку: воркер не работает."
    engine = create_engine(settings)
    try:
        await ping(engine)
    except (DBAPIError, OSError):
        return "База данных не отвечает."
    finally:
        await engine.dispose()
    return None


async def _reindex(container: WorkerContainer, only_errors: bool, recreate: bool) -> str:
    count = await container.reindexer.run(only_errors=only_errors, recreate_collection=recreate)
    return f"Документов возвращено в очередь: {count}\n"


async def _eval_search(
    container: WorkerContainer, directory: Path | None, disabled: Sequence[Feature]
) -> str:
    """Оценить поиск на контрольном наборе в отдельной коллекции, которая затем удаляется.

    `directory` — набор из каталога вместо встроенного; `disabled` — улучшения поиска,
    выключенные на этот прогон: замер «до и после» без правки конфигурации.
    """
    settings = container.settings
    kb = settings.kb.model_copy(
        update={
            feature: getattr(settings.kb, feature).model_copy(update={"enabled": False})
            for feature in disabled
        }
    )
    index = QdrantVectorIndex(container.qdrant, kb, f"{kb.qdrant.collection}_eval")
    ports = EvalPorts(
        embedder=container.embedder,
        index=index,
        rewriter=QueryRewriter(container.chat_model, kb.rewrite),
        explainer=FragmentExplainer(
            container.chat_model,
            RatioTokenEstimator(settings.llm.chars_per_token, settings.llm.tokens_per_image),
            kb.context,
            asyncio.Semaphore(kb.worker.background_llm_parallel),
        ),
        reranker=BifrostReranker(
            container.http_client, kb.rerank, settings.require_rerank_api_key()
        ),
    )
    try:
        documents, questions = evalset.load(directory)
    except (OSError, ValueError, LookupError, TypeError) as error:
        # Содержимое набора в сообщение не попадает: только вид ошибки.
        raise ConfigError(f"Контрольный набор не читается: {type(error).__name__}") from error
    await index.drop_collection()
    await index.ensure_collection()
    try:
        report = await evaluate(documents, questions, ports, kb)
    finally:
        await index.drop_collection()
    if idle := idle_features(report):
        # Числа такого прогона равны замеру без улучшения: за замер «с ним» их не выдаём.
        raise ConfigError(
            f"Улучшения поиска включены, но не сработали ни на одном вопросе: {', '.join(idle)}."
            " Проверьте модель или выключите их на прогон (--disable)."
        )
    return format_report(report)


async def _reprocess(settings: Settings, action: str, all_ready: bool) -> str:
    """Команды массовой переобработки: им нужна только база (§13.5)."""
    clock = SystemClock()
    engine = create_engine(settings)
    reprocessor = Reprocessor(SqlKbUnitOfWorkFactory(engine, clock), clock)
    try:
        if action == "start":
            count = await reprocessor.start(all_ready=all_ready)
            return f"Документов поставлено на переобработку: {count}\n"
        if action == "pause":
            await reprocessor.set_paused(True)
            return "Массовая переобработка приостановлена.\n"
        if action == "resume":
            await reprocessor.set_paused(False)
            return "Массовая переобработка возобновлена.\n"
        status = await reprocessor.status()
    finally:
        await engine.dispose()
    lines = [
        f"Документов с текущим индексом: {status.current}",
        f"Заданий переобработки в очереди: {status.reprocess_jobs}",
        f"Заданий пояснений в очереди: {status.context_jobs}",
        f"Документов со сбоем переобработки: {sum(status.errors.values())}",
        *(f"  {code}: {count}" for code, count in status.errors.items()),
        f"Пауза: {'да' if status.paused else 'нет'}",
    ]
    return "".join(f"{line}\n" for line in lines)


async def _run_kb_command(settings: Settings, args: argparse.Namespace) -> str:
    container = build_worker_container(settings)
    try:
        if args.command == "worker":
            await _worker(container)
            return ""
        if args.command == "reindex":
            return await _reindex(container, args.only_errors, args.recreate_collection)
        return await _eval_search(container, args.set, args.disable)
    finally:
        await container.aclose()


def _parse_since(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portal", description="Команды портала сотрудников")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="применить миграции")
    commands.add_parser("serve", help="запустить API на 0.0.0.0:8000")
    create_admin = commands.add_parser("create-admin", help="создать администратора")
    create_admin.add_argument("--login", required=True)
    create_admin.add_argument("--full-name", required=True)
    reset = commands.add_parser("reset-second-factor", help="сбросить второй фактор")
    reset.add_argument("--login", required=True)
    unlock = commands.add_parser("unlock-login", help="снять временную блокировку входа")
    unlock.add_argument("--login", required=True)
    audit = commands.add_parser("audit", help="показать журнал аудита")
    audit.add_argument("--since", type=_parse_since, help="дата или время ISO 8601 (UTC)")
    audit.add_argument("--event", help="только события этого вида")
    audit.add_argument("--limit", type=int, default=100, help="сколько последних записей")
    commands.add_parser("worker", help="запустить цикл очереди базы знаний")
    commands.add_parser("worker-health", help="проверить, что воркер жив и база отвечает")
    reindex = commands.add_parser("reindex", help="вернуть документы базы знаний в очередь")
    # Пересоздание коллекции затрагивает все документы, поэтому с отбором не сочетается.
    reindex_mode = reindex.add_mutually_exclusive_group()
    reindex_mode.add_argument(
        "--only-errors", action="store_true", help="только документы с ошибкой"
    )
    reindex_mode.add_argument(
        "--recreate-collection",
        action="store_true",
        help="пересоздать коллекцию Qdrant; только при остановленном воркере",
    )
    reprocess = commands.add_parser(
        "reprocess", help="массовая переобработка документов без выпадения из поиска"
    )
    actions = reprocess.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="поставить задания переобработки")
    start.add_argument(
        "--all", action="store_true", help="всем готовым документам, а не только прежним"
    )
    actions.add_parser("pause", help="приостановить массовую переобработку")
    actions.add_parser("resume", help="возобновить массовую переобработку")
    actions.add_parser("status", help="показать числа хода переобработки")
    eval_search = commands.add_parser("eval-search", help="оценить поиск на контрольном наборе")
    eval_search.add_argument(
        "--set", type=Path, help="каталог набора (corpus.yaml и questions.yaml) вместо встроенного"
    )
    eval_search.add_argument(
        "--disable",
        action="append",
        choices=FEATURES,
        default=[],
        help="выключить улучшение поиска на этот прогон; можно несколько раз",
    )
    return parser


async def _run_admin_command(settings: Settings, args: argparse.Namespace) -> str:
    container = build_admin_container(settings)
    try:
        if args.command == "create-admin":
            return await _create_admin(container, args.login, args.full_name)
        if args.command == "reset-second-factor":
            return await _reset_second_factor(container, args.login)
        if args.command == "unlock-login":
            return await _unlock_login(container, args.login)
        return await _audit(container, args.since, args.event, args.limit)
    finally:
        container.hash_executor.shutdown()
        await container.engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Разобрать аргументы и выполнить команду; код возврата 1 — отказ."""
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(os.environ)
        if args.command == "migrate":
            migrate(settings)
        elif args.command == "serve":
            _serve(settings)
        elif args.command == "worker-health":
            if reason := asyncio.run(_worker_health(settings)):
                sys.stderr.write(f"{reason}\n")
                return 1
        elif args.command == "reprocess":
            all_ready = getattr(args, "all", False)
            sys.stdout.write(asyncio.run(_reprocess(settings, args.action, all_ready)))
        elif args.command in _KB_COMMANDS:
            if args.command == "worker":
                configure_logging()
            sys.stdout.write(asyncio.run(_run_kb_command(settings, args)))
        else:
            sys.stdout.write(asyncio.run(_run_admin_command(settings, args)))
    except ConfigError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    except AppError as error:
        sys.stderr.write(f"{_describe(error)}\n")
        return 1
    except WorkerRunningError:
        sys.stderr.write("Есть задания в работе: остановите воркер и повторите команду.\n")
        return 1
    except SpeechWeightsMissingError as error:
        sys.stderr.write(
            f"В каталоге media.asr.model_dir нет весов модели речи ({error}): "
            "загрузите их либо выключите media.enabled.\n"
        )
        return 1
    except CollectionMismatchError:
        sys.stderr.write(
            "Параметры коллекции Qdrant расходятся с конфигурацией: "
            "portal reindex --recreate-collection.\n"
        )
        return 1
    except (
        VectorIndexUnavailableError,
        EmbeddingsUnavailableError,
        ModelUnavailableError,
        ModelOverloadedError,
        OSError,
    ) as error:
        sys.stderr.write(f"Служба недоступна: {type(error).__name__}\n")
        return 1
    return 0


def _describe(error: AppError) -> str:
    details = "; ".join(f"{item.field}: {item.message}" for item in error.fields)
    return f"{error.message} {details}".strip()
