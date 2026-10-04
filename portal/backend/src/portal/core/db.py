"""Подключение к PostgreSQL и общий реестр таблиц."""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from portal.core.settings import Settings

metadata = sa.MetaData()


def database_url(settings: Settings) -> sa.URL:
    """Адрес базы из настроек и пароля окружения."""
    database = settings.database
    return sa.URL.create(
        "postgresql+asyncpg",
        username=database.user,
        password=settings.db_password.get_secret_value() or None,
        host=database.host,
        port=database.port,
        database=database.name,
    )


def create_engine(settings: Settings) -> AsyncEngine:
    """Создать движок; значения параметров запросов в текст ошибок не попадают."""
    return create_async_engine(database_url(settings), hide_parameters=True, pool_pre_ping=True)


async def ping(engine: AsyncEngine) -> None:
    """Проверить, что база отвечает."""
    async with engine.connect() as connection:
        await connection.execute(sa.text("SELECT 1"))
