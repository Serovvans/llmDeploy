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
from portal.kb.evaluation import evaluate, format_report
from portal.kb.ports import (
    CollectionMismatchError,
    EmbeddingsUnavailableError,
    VectorIndexUnavailableError,
)
from portal.kb.qdrant import QdrantVectorIndex
from portal.kb.reindex import WorkerRunningError
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
    """Цикл очереди до сигнала остановки; взятые задания возвращаются в очередь."""
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


async def _eval_search(container: WorkerContainer) -> str:
    """Оценить поиск на контрольном наборе в отдельной коллекции, которая затем удаляется."""
    settings = container.settings.kb
    index = QdrantVectorIndex(container.qdrant, settings, f"{settings.qdrant.collection}_eval")
    documents, questions = evalset.load()
    await index.drop_collection()
    await index.ensure_collection()
    try:
        report = await evaluate(documents, questions, container.embedder, index, settings)
    finally:
        await index.drop_collection()
    return format_report(report)


async def _run_kb_command(settings: Settings, args: argparse.Namespace) -> str:
    container = build_worker_container(settings)
    try:
        if args.command == "worker":
            await _worker(container)
            return ""
        if args.command == "reindex":
            return await _reindex(container, args.only_errors, args.recreate_collection)
        return await _eval_search(container)
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
    commands.add_parser("eval-search", help="оценить поиск на контрольном наборе")
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
    except CollectionMismatchError:
        sys.stderr.write(
            "Параметры коллекции Qdrant расходятся с конфигурацией: "
            "portal reindex --recreate-collection.\n"
        )
        return 1
    except (VectorIndexUnavailableError, EmbeddingsUnavailableError, OSError) as error:
        sys.stderr.write(f"Служба недоступна: {type(error).__name__}\n")
        return 1
    return 0


def _describe(error: AppError) -> str:
    details = "; ".join(f"{item.field}: {item.message}" for item in error.fields)
    return f"{error.message} {details}".strip()
