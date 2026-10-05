"""Тестовая база — настоящий PostgreSQL из пакета pixeltable-pgserver.

Сервер поднимается один раз на прогон во временном каталоге, без Docker и без сети;
схема создаётся миграциями Alembic — тем самым миграции проверяются каждым прогоном.
"""

import base64
import shutil
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
from tests.kb_support import KbBench, kb_settings, make_bench
from tests.support import FakeClock, FakeKnowledge, Portal, ScriptedModel

CONFIG_DIR = Path(__file__).parent.parent / "config"
SECRET_KEY = base64.b64encode(bytes(range(32))).decode()
START = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def environ() -> dict[str, str]:
    return {
        "PORTAL_DB_PASSWORD": "",
        "PORTAL_SECRET_KEY": SECRET_KEY,
        "PORTAL_LLM_API_KEY": "sk-bf-test",
        "QDRANT_API_KEY": "qdrant-test",
        "MAX_MODEL_LEN": "65536",
    }


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
        "auth": {
            "password": {"argon2": {"time_cost": 1, "memory_cost_kib": 8, "parallelism": 1}},
            "session": {"stream_recheck_seconds": 1},
        },
        "files": {"root": str(directory / "files")},
        # Короткие ожидания: тесты потока не должны ждать секунды.
        "dialogs": {"stop_grace_seconds": 1, "title_wait_seconds": 1, "keepalive_seconds": 0.2},
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


async def make_portal(settings: Settings, *, scripted_model: bool = True) -> Portal:
    """Приложение на чистой базе с подменными часами.

    Модель подменная; с `scripted_model=False` работает настоящий клиент Bifrost — его
    адрес тест направляет на заглушку стенда.
    """
    clock = FakeClock(START)
    model = ScriptedModel()
    knowledge = FakeKnowledge()
    container = build_container(settings, clock, model if scripted_model else None, knowledge)
    shutil.rmtree(settings.files.root, ignore_errors=True)
    async with container.engine.begin() as connection:
        await connection.execute(
            sa.text("TRUNCATE users, auth_throttle, audit_log RESTART IDENTITY CASCADE")
        )
    return Portal(create_app(container), container, clock, model, knowledge)


async def close_portal(portal: Portal) -> None:
    await portal.container.generation.shutdown()
    await portal.container.docparse.shutdown()
    portal.container.sql_checker.close()
    await portal.container.http_client.aclose()
    await portal.container.qdrant.close()
    await portal.container.engine.dispose()


@pytest.fixture
async def portal(settings: Settings) -> AsyncIterator[Portal]:
    built = await make_portal(settings)
    yield built
    await close_portal(built)


@pytest.fixture
async def bench(settings: Settings, tmp_path: Path) -> AsyncIterator[KbBench]:
    """Портал вместе с воркером базы знаний на локальном Qdrant."""
    tuned = kb_settings(settings, tmp_path)
    built = await make_bench(await make_portal(tuned), tuned)
    yield built
    await built.qdrant.close()
    await close_portal(built.portal)
