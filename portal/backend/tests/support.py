"""Общие помощники тестов: стенд приложения, подменные часы, шаги входа."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pyotp
import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from portal.core.container import Container
from portal.core.ports import CurrentUser

NEW_PASSWORD = "Надёжный-пароль-2026"
CLIENT_IP = "203.0.113.5"


def run_during(
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    method: str,
    action: Callable[[], Coroutine[Any, Any, object]],
) -> None:
    """Выполнить `action` целиком в момент первого вызова `target.method`.

    Метод (проверка или расчёт хеша Argon2) идёт в рабочем потоке, пока сценарий ждёт
    его посреди своей транзакции; действие выполняется в цикле событий теста и успевает
    зафиксироваться до того, как сценарий продолжит. Так гонка воспроизводится точно.
    """
    loop = asyncio.get_running_loop()
    original = getattr(target, method)
    fired: list[bool] = []

    def wrapper(*args: Any) -> Any:
        if not fired:
            fired.append(True)
            asyncio.run_coroutine_threadsafe(action(), loop).result(timeout=30)
        return original(*args)

    monkeypatch.setattr(target, method, wrapper)


class FakeClock:
    """Часы, которые идут только по команде теста."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)


@dataclass(frozen=True)
class Account:
    """Учётная запись, прошедшая первый вход."""

    login: str
    password: str
    secret: str
    backup_codes: list[str]


@dataclass
class Portal:
    """Приложение на тестовой базе с подменными часами."""

    app: FastAPI
    container: Container
    clock: FakeClock

    def client(self, ip: str = CLIENT_IP, forwarded_for: str | None = None) -> httpx.AsyncClient:
        """Клиент-«браузер»: свой набор cookie и заголовок защиты от CSRF."""
        headers = {"X-Portal-Csrf": "1"}
        if forwarded_for is not None:
            headers["X-Forwarded-For"] = forwarded_for
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(ip, 50000)),
            base_url="https://portal.test",
            headers=headers,
        )

    def code(self, secret: str, steps: int = 0) -> str:
        """Код приложения-аутентификатора на текущее время часов со сдвигом в шагах."""
        return pyotp.TOTP(secret).at(self.clock.now() + timedelta(seconds=30 * steps))

    async def create_user(self, login: str, role: str = "employee") -> str:
        """Создать учётную запись как командой на ВМ; вернуть временный пароль."""
        _, password = await self.container.admin.create_user(
            None,
            f"Пользователь {login}",
            login,
            role,  # type: ignore[arg-type]
        )
        return password

    async def onboard(
        self, client: httpx.AsyncClient, login: str, role: str = "employee"
    ) -> Account:
        """Первый вход: временный пароль → смена пароля → настройка второго фактора."""
        temporary = await self.create_user(login, role)
        response = await client.post(
            "/api/auth/login", json={"login": login, "password": temporary}
        )
        assert response.json()["step"] == "password_change", response.text
        response = await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
        assert response.json()["step"] == "second_factor_setup", response.text
        secret = (await client.post("/api/auth/second-factor/setup")).json()["secret"]
        response = await client.post(
            "/api/auth/second-factor/confirm", json={"code": self.code(secret)}
        )
        assert response.json()["session"]["step"] == "ready", response.text
        return Account(login, NEW_PASSWORD, secret, response.json()["backup_codes"])

    async def sign_in(self, client: httpx.AsyncClient, account: Account) -> None:
        """Обычный вход: пароль и свежий код (часы сдвигаются на шаг — код одноразовый)."""
        self.clock.advance(seconds=30)
        response = await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        assert response.json()["step"] == "second_factor", response.text
        response = await client.post(
            "/api/auth/second-factor", json={"code": self.code(account.secret)}
        )
        assert response.json()["session"]["step"] == "ready", response.text

    async def actor(self, login: str = "boss") -> CurrentUser:
        """Администратор, от имени которого тест вызывает сценарии администрирования."""
        await self.create_user(login, "admin")
        row = (await self.rows("SELECT id FROM users WHERE login = :login", login=login))[0]
        return CurrentUser(id=row.id, role="admin", full_name="Администратор", ip=None)

    async def user_id(self, login: str) -> UUID:
        """Идентификатор учётной записи по логину."""
        return cast(
            UUID, (await self.rows("SELECT id FROM users WHERE login = :login", login=login))[0].id
        )

    async def rows(self, query: str, **params: Any) -> list[Any]:
        """Выполнить запрос к тестовой базе напрямую."""
        async with self.container.engine.connect() as connection:
            return list((await connection.execute(sa.text(query), params)).all())

    async def execute(self, statement: str, **params: Any) -> None:
        """Изменить данные тестовой базы напрямую."""
        async with self.container.engine.begin() as connection:
            await connection.execute(sa.text(statement), params)

    async def audit_events(self) -> list[tuple[str, dict[str, Any]]]:
        """События журнала аудита по порядку."""
        rows = await self.rows("SELECT event, details FROM audit_log ORDER BY id")
        return [(row.event, row.details) for row in rows]
