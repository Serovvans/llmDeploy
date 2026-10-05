"""Команды `portal` этапа 2 (docs/portal-api.md §13.4)."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from portal import cli
from portal.core import settings as settings_module
from tests.conftest import CONFIG_DIR
from tests.support import Portal

pytestmark = pytest.mark.anyio


@pytest.fixture
def command_line(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], override_path: Path
) -> Iterator[None]:
    """Окружение команды: те же настройки, что у тестового приложения."""
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda env: settings_module.load_settings(env, CONFIG_DIR, override_path),
    )
    yield


def _run(*argv: str) -> int:
    return cli.main(argv)


async def _in_thread(*argv: str) -> int:
    """Команда запускает свой цикл событий, поэтому в тесте идёт в отдельном потоке."""
    return await asyncio.to_thread(_run, *argv)


@pytest.mark.usefixtures("command_line")
async def test_create_admin_prints_temporary_password(
    portal: Portal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _in_thread("create-admin", "--login", "Owner", "--full-name", "Иванов И. И.") == 0
    output = capsys.readouterr().out
    password = output.strip().rsplit(" ", 1)[-1]
    async with portal.client() as client:
        response = await client.post(
            "/api/auth/login", json={"login": "owner", "password": password}
        )
    assert response.json()["step"] == "password_change"
    assert response.json()["user"]["role"] == "admin"
    rows = await portal.rows("SELECT event, actor_id, details FROM audit_log ORDER BY id LIMIT 1")
    assert (rows[0].event, rows[0].actor_id, rows[0].details) == (
        "user_created", None, {"via": "cli"},
    )  # fmt: skip


@pytest.mark.usefixtures("command_line")
async def test_create_admin_refuses_taken_and_malformed_login(
    portal: Portal, capsys: pytest.CaptureFixture[str]
) -> None:
    await portal.create_user("owner")
    assert await _in_thread("create-admin", "--login", "owner", "--full-name", "Имя") == 1
    assert "уже есть" in capsys.readouterr().err
    assert await _in_thread("create-admin", "--login", "x", "--full-name", "Имя") == 1
    assert "login" in capsys.readouterr().err
    assert len(await portal.rows("SELECT 1 FROM users")) == 1


@pytest.mark.usefixtures("command_line")
async def test_reset_second_factor_from_command_line(
    portal: Portal, capsys: pytest.CaptureFixture[str]
) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "owner", role="admin")
        assert await _in_thread("reset-second-factor", "--login", "OWNER") == 0
        assert (await client.get("/api/auth/session")).status_code == 401
        response = await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        assert response.json()["step"] == "second_factor_setup"
    assert (await portal.audit_events())[-1] == ("second_factor_reset", {"via": "cli"})

    # Команда снимает и блокировку логина.
    async with portal.client() as client:
        secret = (
            await client.post(
                "/api/auth/login", json={"login": account.login, "password": account.password}
            )
        ).json()
        assert secret["step"] == "second_factor_setup"
        await client.post("/api/auth/second-factor/setup")
        for _ in range(5):
            await client.post("/api/auth/login", json={"login": "owner", "password": "неверный"})
        assert await portal.rows("SELECT 1 FROM auth_throttle WHERE scope = 'login'") != []
        assert await _in_thread("reset-second-factor", "--login", "owner") == 0
        assert await portal.rows("SELECT 1 FROM auth_throttle WHERE scope = 'login'") == []
        assert len(await portal.rows("SELECT 1 FROM auth_throttle WHERE scope = 'ip'")) == 1
    capsys.readouterr()
    assert await _in_thread("reset-second-factor", "--login", "nobody") == 1
    assert capsys.readouterr().err.strip() == "Не найдено."


@pytest.mark.usefixtures("command_line")
async def test_audit_prints_filtered_journal(
    portal: Portal, capsys: pytest.CaptureFixture[str]
) -> None:
    async with portal.client() as client:
        await portal.onboard(client, "owner", role="admin")
        await client.post("/api/auth/login", json={"login": "owner", "password": "неверный"})
    capsys.readouterr()

    assert await _in_thread("audit") == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[1] for line in lines] == [
        "user_created", "password_changed", "login_succeeded", "login_failed",
    ]  # fmt: skip
    assert lines[0] == '2026-10-05T09:00:00Z\tuser_created\t-\towner\t-\t{"via": "cli"}'
    assert lines[3].endswith(
        'login_failed\t-\towner\t203.0.113.5\t{"reason": "invalid_credentials"}'
    )

    assert await _in_thread("audit", "--event", "login_failed", "--limit", "5") == 0
    assert len(capsys.readouterr().out.splitlines()) == 1
    assert await _in_thread("audit", "--limit", "2") == 0
    assert [line.split("\t")[1] for line in capsys.readouterr().out.splitlines()] == [
        "login_succeeded", "login_failed",
    ]  # fmt: skip
    assert await _in_thread("audit", "--since", "2026-10-06") == 0
    assert capsys.readouterr().out == ""


@pytest.mark.usefixtures("command_line")
async def test_migrate_is_idempotent(portal: Portal) -> None:
    assert await _in_thread("migrate") == 0
    assert (await portal.rows("SELECT version_num FROM alembic_version"))[0].version_num == "0004"


def test_missing_environment_is_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PORTAL_DB_PASSWORD", raising=False)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda env: settings_module.load_settings(env, CONFIG_DIR, Path("/nonexistent")),
    )
    assert _run("audit") == 1
    assert "PORTAL_DB_PASSWORD" in capsys.readouterr().err
