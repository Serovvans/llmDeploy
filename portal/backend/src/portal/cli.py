"""Точка входа команд `portal` (docs/portal-api.md §13.4)."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from portal.core.app import create_app
from portal.core.audit import read_audit
from portal.core.container import AdminContainer, build_admin_container, build_container
from portal.core.errors import AppError
from portal.core.logging import configure_logging
from portal.core.settings import ConfigError, Settings, load_settings

_ALEMBIC_INI = Path("alembic.ini")
_API_HOST = "0.0.0.0"  # контейнер без портов на хост, слушает сеть compose
_API_PORT = 8000


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
    audit = commands.add_parser("audit", help="показать журнал аудита")
    audit.add_argument("--since", type=_parse_since, help="дата или время ISO 8601 (UTC)")
    audit.add_argument("--event", help="только события этого вида")
    audit.add_argument("--limit", type=int, default=100, help="сколько последних записей")
    return parser


async def _run_admin_command(settings: Settings, args: argparse.Namespace) -> str:
    container = build_admin_container(settings)
    try:
        if args.command == "create-admin":
            return await _create_admin(container, args.login, args.full_name)
        if args.command == "reset-second-factor":
            return await _reset_second_factor(container, args.login)
        return await _audit(container, args.since, args.event, args.limit)
    finally:
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
        else:
            sys.stdout.write(asyncio.run(_run_admin_command(settings, args)))
    except ConfigError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    except AppError as error:
        sys.stderr.write(f"{_describe(error)}\n")
        return 1
    return 0


def _describe(error: AppError) -> str:
    details = "; ".join(f"{item.field}: {item.message}" for item in error.fields)
    return f"{error.message} {details}".strip()
