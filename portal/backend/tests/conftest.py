"""Тестовая база — настоящий PostgreSQL из пакета pixeltable-pgserver.

Сервер поднимается один раз на прогон во временном каталоге, без Docker и без сети;
схема создаётся миграциями Alembic — тем самым миграции проверяются каждым прогоном.
"""

import base64
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from pixeltable_pgserver.postgres_server import get_server

from portal.cli import migrate
from portal.core.app import create_app
from portal.core.container import build_container
from portal.core.settings import Settings, load_settings
from tests.support import FakeClock, Portal

CONFIG_DIR = Path(__file__).parent.parent / "config"
SECRET_KEY = base64.b64encode(bytes(range(32))).decode()
START = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def environ() -> dict[str, str]:
    return {"PORTAL_DB_PASSWORD": "", "PORTAL_SECRET_KEY": SECRET_KEY}


@pytest.fixture(scope="session")
def override_path(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Файл-накладка стенда тестов: адрес временной базы и дешёвый Argon2."""
    directory = tmp_path_factory.mktemp("portal")
    server = get_server(directory / "pgdata", cleanup_mode="stop")
    info = server.get_postmaster_info()
    override = {
        "database": {
            "host": str(info.socket_dir),
            "port": info.port,
            "name": "postgres",
            "user": "postgres",
        },
        "auth": {"password": {"argon2": {"time_cost": 1, "memory_cost_kib": 8, "parallelism": 1}}},
    }
    path = directory / "config.override.yaml"
    path.write_text(yaml.safe_dump(override), encoding="utf-8")
    yield path
    server.cleanup()


@pytest.fixture(scope="session")
def settings(environ: dict[str, str], override_path: Path) -> Settings:
    loaded = load_settings(environ, CONFIG_DIR, override_path)
    migrate(loaded)
    return loaded


@pytest.fixture
async def portal(settings: Settings) -> AsyncIterator[Portal]:
    clock = FakeClock(START)
    container = build_container(settings, clock)
    async with container.engine.begin() as connection:
        await connection.execute(
            sa.text("TRUNCATE users, auth_throttle, audit_log RESTART IDENTITY CASCADE")
        )
    yield Portal(create_app(container), container, clock)
    await container.engine.dispose()
