"""Окружение Alembic: адрес базы — из настроек портала, миграции идут через asyncpg."""

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from portal.core.db import database_url
from portal.core.settings import Settings, load_settings


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def _migrate(settings: Settings) -> None:
    engine = create_async_engine(database_url(settings))
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


# `portal migrate` передаёт уже прочитанные настройки; при запуске `alembic` напрямую
# они читаются здесь тем же классом настроек.
asyncio.run(_migrate(context.config.attributes.get("settings") or load_settings(os.environ)))
