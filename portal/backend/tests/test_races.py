"""Гонки записи учётной записи: параллельные действия не затирают друг друга.

Долгая проверка Argon2 идёт посреди транзакции входа и смены пароля; действие
администратора выполняется ровно в этот момент (`run_during`).
"""

import asyncio

import pytest

from portal.auth.domain import token_hash
from tests.support import NEW_PASSWORD, Portal, run_during

pytestmark = pytest.mark.anyio

OTHER_PASSWORD = "Совсем-другой-пароль-7"


async def test_block_during_login_wins_and_leaves_no_session(
    portal: Portal, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = await portal.create_user("ivanov")
    actor = await portal.actor()
    target = await portal.user_id("ivanov")
    run_during(
        monkeypatch,
        portal.container.auth._hasher,
        "verify",
        lambda: portal.container.admin.set_blocked(actor, target, blocked=True),
    )
    async with portal.client() as client:
        response = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_blocked"
    assert "set-cookie" not in response.headers
    assert (await portal.rows("SELECT is_blocked FROM users WHERE id = :id", id=target))[
        0
    ].is_blocked
    assert await portal.rows("SELECT 1 FROM sessions") == []


async def test_password_reset_during_login_invalidates_the_old_password(
    portal: Portal, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = await portal.create_user("ivanov")
    actor = await portal.actor()
    target = await portal.user_id("ivanov")
    issued: list[str] = []

    async def reset() -> None:
        _, password = await portal.container.admin.reset_password(actor, target)
        issued.append(password)

    run_during(monkeypatch, portal.container.auth._hasher, "verify", reset)
    async with portal.client() as client:
        stale = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        assert stale.status_code == 401
        assert await portal.rows("SELECT 1 FROM sessions") == []
        fresh = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": issued[0]}
        )
        assert fresh.json()["step"] == "password_change"


@pytest.mark.parametrize("action", ["reset_password", "change_role", "block"])
async def test_admin_action_during_voluntary_password_change_wins(
    portal: Portal, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
        actor = await portal.actor()
        target = await portal.user_id("ivanov")
        admin = portal.container.admin
        issued: list[str] = []

        async def act() -> None:
            if action == "reset_password":
                issued.append((await admin.reset_password(actor, target))[1])
            elif action == "change_role":
                await admin.update_user(actor, target, None, "admin")
            else:
                await admin.set_blocked(actor, target, blocked=True)

        run_during(monkeypatch, portal.container.auth._hasher, "hash", act)
        response = await client.post(
            "/api/auth/password",
            json={"new_password": OTHER_PASSWORD, "current_password": account.password},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    row = (await portal.rows("SELECT * FROM users WHERE id = :id", id=target))[0]
    hasher = portal.container.auth._hasher
    assert not hasher.verify(row.password_hash, OTHER_PASSWORD)
    if action == "reset_password":
        assert hasher.verify(row.password_hash, issued[0]) and row.must_change_password
    else:
        assert hasher.verify(row.password_hash, account.password)
        assert (row.role, row.is_blocked) == (
            ("admin", False) if action == "change_role" else ("employee", True)
        )
    assert await portal.rows("SELECT 1 FROM sessions WHERE user_id = :id", id=target) == []


async def test_password_reset_during_forced_change_wins(
    portal: Portal, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = await portal.create_user("ivanov")
    actor = await portal.actor()
    target = await portal.user_id("ivanov")
    issued: list[str] = []

    async def reset() -> None:
        issued.append((await portal.container.admin.reset_password(actor, target))[1])

    async with portal.client() as client:
        await client.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        run_during(monkeypatch, portal.container.auth._hasher, "hash", reset)
        response = await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "login_step_expired"
    row = (await portal.rows("SELECT * FROM users WHERE id = :id", id=target))[0]
    assert portal.container.auth._hasher.verify(row.password_hash, issued[0])
    assert row.must_change_password
    assert await portal.rows("SELECT 1 FROM sessions") == []


async def test_admin_edit_and_second_factor_confirm_keep_both_changes(portal: Portal) -> None:
    for attempt in range(5):
        login = f"user{attempt}"
        temporary = await portal.create_user(login)
        actor = await portal.actor(f"boss{attempt}")
        target = await portal.user_id(login)
        async with portal.client() as client:
            await client.post("/api/auth/login", json={"login": login, "password": temporary})
            await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
            secret = (await client.post("/api/auth/second-factor/setup")).json()["secret"]
            confirm, _ = await asyncio.gather(
                client.post("/api/auth/second-factor/confirm", json={"code": portal.code(secret)}),
                portal.container.admin.update_user(actor, target, "Новое имя", None),
            )
        assert confirm.status_code == 200
        row = (await portal.rows("SELECT * FROM users WHERE id = :id", id=target))[0]
        assert (row.totp_enabled, row.full_name) == (True, "Новое имя")
        assert row.totp_last_step is not None and row.last_login_at is not None


async def test_stale_user_write_touches_only_named_columns(portal: Portal) -> None:
    """Запись из устаревшей копии не возвращает прежние значения остальных полей."""
    async with portal.client() as client:
        temporary = await portal.create_user("ivanov")
        target = await portal.user_id("ivanov")
        async with portal.container.auth._uow_factory() as uow:
            stale = await uow.users.get(target)
        assert stale is not None

        await client.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
        secret = (await client.post("/api/auth/second-factor/setup")).json()["secret"]
        await client.post("/api/auth/second-factor/confirm", json={"code": portal.code(secret)})
        await portal.execute("UPDATE users SET is_blocked = true, role = 'admin'")

        stale.full_name = "Новое имя"
        async with portal.container.auth._uow_factory() as uow:
            await uow.users.update(stale, "full_name")
    row = (await portal.rows("SELECT * FROM users WHERE id = :id", id=target))[0]
    assert row.full_name == "Новое имя"
    assert (row.is_blocked, row.role, row.totp_enabled, row.must_change_password) == (
        True, "admin", True, False,
    )  # fmt: skip
    assert row.totp_secret is not None and row.last_login_at is not None


async def test_stale_session_touch_does_not_undo_second_factor(portal: Portal) -> None:
    """Параллельный запрос, обновляющий `last_seen_at`, не сбрасывает пройденный фактор."""
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        portal.clock.advance(seconds=30)
        response = await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        token = response.cookies["portal_session"]
        async with portal.container.auth._uow_factory() as uow:
            found = await uow.sessions.by_token_hash(token_hash(token))
        assert found is not None
        stale = found[0]
        await client.post("/api/auth/second-factor", json={"code": portal.code(account.secret)})

        stale.last_seen_at = portal.clock.now()
        async with portal.container.auth._uow_factory() as uow:
            await uow.sessions.update(stale, "last_seen_at")
        assert (await client.get("/api/auth/session")).json()["step"] == "ready"
