"""Срок жизни и гашение сессий (docs/portal-api.md §2.4, §2.6)."""

import pytest

from tests.support import NEW_PASSWORD, Portal

pytestmark = pytest.mark.anyio

OTHER_PASSWORD = "Совсем-другой-пароль-7"


async def test_request_without_cookie_is_unauthenticated(portal: Portal) -> None:
    async with portal.client() as client:
        for path in ("/api/auth/session", "/api/config", "/api/admin/users"):
            response = await client.get(path)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "unauthenticated"
            assert "set-cookie" not in response.headers
        assert (await client.post("/api/auth/logout")).status_code == 401


async def test_forged_cookie_is_rejected_and_cleared(portal: Portal) -> None:
    async with portal.client() as client:
        client.cookies.set("portal_session", "A" * 43, domain="portal.test", path="/api")
        response = await client.get("/api/auth/session")
    assert response.status_code == 401
    assert "portal_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_session_expires_by_idle_time(portal: Portal) -> None:
    async with portal.client() as client:
        await portal.onboard(client, "ivanov")
        portal.clock.advance(minutes=59)
        assert (await client.get("/api/auth/session")).status_code == 200
        portal.clock.advance(minutes=59)
        assert (await client.get("/api/auth/session")).status_code == 200
        portal.clock.advance(minutes=61)
        response = await client.get("/api/auth/session")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert "Max-Age=0" in response.headers["set-cookie"]
    assert await portal.rows("SELECT 1 FROM sessions") == []


async def test_session_expires_by_absolute_time_despite_activity(portal: Portal) -> None:
    async with portal.client() as client:
        await portal.onboard(client, "ivanov")
        for _ in range(23):
            portal.clock.advance(minutes=30)
            assert (await client.get("/api/auth/session")).status_code == 200
        portal.clock.advance(minutes=30)
        assert (await client.get("/api/auth/session")).status_code == 401


async def test_last_seen_is_written_at_most_once_a_minute(portal: Portal) -> None:
    async with portal.client() as client:
        await portal.onboard(client, "ivanov")
        before = (await portal.rows("SELECT last_seen_at FROM sessions"))[0].last_seen_at
        portal.clock.advance(seconds=59)
        await client.get("/api/auth/session")
        assert (await portal.rows("SELECT last_seen_at FROM sessions"))[0].last_seen_at == before
        portal.clock.advance(seconds=1)
        await client.get("/api/auth/session")
        assert (await portal.rows("SELECT last_seen_at FROM sessions"))[0].last_seen_at > before


async def test_logout_deletes_session_and_cookie(portal: Portal) -> None:
    async with portal.client() as client:
        await portal.onboard(client, "ivanov")
        response = await client.post("/api/auth/logout")
        assert response.status_code == 204
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert (await client.get("/api/auth/session")).status_code == 401
    assert await portal.rows("SELECT 1 FROM sessions") == []


async def test_forced_password_change_issues_inherited_session(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        first = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        old_token = first.cookies["portal_session"]
        portal.clock.advance(minutes=5)
        changed = await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
        new_token = changed.cookies["portal_session"]
        assert new_token != old_token
        # Срок незавершённого входа отсчитывается от исходного входа: 20 − 5 минут.
        assert "Max-Age=900" in changed.headers["set-cookie"]
        assert len(await portal.rows("SELECT 1 FROM sessions")) == 1

        portal.clock.advance(minutes=15)
        expired = await client.post("/api/auth/second-factor/setup")
        assert expired.json()["error"]["code"] == "login_step_expired"


async def test_forced_change_after_admin_reset_keeps_second_factor_passed(portal: Portal) -> None:
    async with portal.client() as admin, portal.client() as client:
        await portal.onboard(admin, "admin", role="admin")
        account = await portal.onboard(client, "ivanov")
        users = (await admin.get("/api/admin/users")).json()["items"]
        target = next(user["id"] for user in users if user["login"] == "ivanov")
        reset = await admin.post(f"/api/admin/users/{target}/reset-password")
        temporary = reset.json()["temporary_password"]

        portal.clock.advance(seconds=30)
        response = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        assert response.json() == {"step": "second_factor", "user": None}
        response = await client.post(
            "/api/auth/second-factor", json={"code": portal.code(account.secret)}
        )
        assert response.json()["session"]["step"] == "password_change"
        response = await client.post("/api/auth/password", json={"new_password": OTHER_PASSWORD})
        assert response.status_code == 200
        assert response.json()["step"] == "ready"
        assert f"Max-Age={12 * 3600}" in response.headers["set-cookie"]


async def test_new_password_rules(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        await client.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        cases = {
            "короткий-11": "password_too_short",
            "Qwertyuiop123": "password_too_common",
            temporary: "password_same_as_old",
            "я" * 129: "too_long",
        }
        for password, code in cases.items():
            response = await client.post("/api/auth/password", json={"new_password": password})
            assert response.status_code == 422
            error = response.json()["error"]
            assert error["code"] == "validation_error"
            assert [(item["field"], item["code"]) for item in error["fields"]] == [
                ("new_password", code)
            ]
            assert error["fields"][0]["message"]
        assert (await client.get("/api/auth/session")).json()["step"] == "password_change"


async def test_voluntary_change_requires_current_password(portal: Portal) -> None:
    async with portal.client() as client:
        await portal.onboard(client, "ivanov")
        for body, code in (
            ({"new_password": OTHER_PASSWORD}, "required"),
            (
                {"new_password": OTHER_PASSWORD, "current_password": "не тот"},
                "current_password_invalid",
            ),
        ):
            response = await client.post("/api/auth/password", json=body)
            assert response.status_code == 422
            fields = response.json()["error"]["fields"]
            assert [(item["field"], item["code"]) for item in fields] == [
                ("current_password", code)
            ]


async def test_voluntary_change_ends_all_sessions(portal: Portal) -> None:
    async with portal.client() as first, portal.client() as second:
        account = await portal.onboard(first, "ivanov")
        await portal.sign_in(second, account)
        response = await first.post(
            "/api/auth/password",
            json={"new_password": OTHER_PASSWORD, "current_password": account.password},
        )
        assert response.status_code == 204
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert (await first.get("/api/auth/session")).status_code == 401
        assert (await second.get("/api/auth/session")).status_code == 401

        portal.clock.advance(seconds=30)
        old = await first.post(
            "/api/auth/login", json={"login": "ivanov", "password": account.password}
        )
        assert old.status_code == 401
        new = await first.post(
            "/api/auth/login", json={"login": "ivanov", "password": OTHER_PASSWORD}
        )
        assert new.json()["step"] == "second_factor"


async def test_expired_sessions_are_removed_at_next_login(portal: Portal) -> None:
    async with portal.client() as first, portal.client() as second:
        account = await portal.onboard(first, "ivanov")
        portal.clock.advance(hours=13)
        await second.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
    assert len(await portal.rows("SELECT 1 FROM sessions")) == 1


async def test_blocked_user_session_is_refused_even_if_row_survived(portal: Portal) -> None:
    """Сессия, созданная входом одновременно с блокировкой, не даёт доступа."""
    async with portal.client() as client:
        await portal.onboard(client, "ivanov")
        await portal.execute("UPDATE users SET is_blocked = true")
        assert (await client.get("/api/auth/session")).status_code == 401
    assert await portal.rows("SELECT 1 FROM sessions") == []


async def test_password_change_is_audited_without_password_values(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
        await client.post(
            "/api/auth/password",
            json={"new_password": "короткий", "current_password": account.password},
        )
        response = await client.post(
            "/api/auth/password",
            json={"new_password": OTHER_PASSWORD, "current_password": account.password},
        )
        assert response.status_code == 204
    rows = await portal.rows(
        "SELECT a.details, actor.login AS actor, subject.login AS subject, host(a.ip) AS ip "
        "FROM audit_log a JOIN users actor ON actor.id = a.actor_id "
        "JOIN users subject ON subject.id = a.subject_user_id "
        "WHERE a.event = 'password_changed' ORDER BY a.id"
    )
    assert [tuple(row) for row in rows] == [
        ({"forced": True}, "ivanov", "ivanov", "203.0.113.5"),
        ({"forced": False}, "ivanov", "ivanov", "203.0.113.5"),
    ]
    events = [event for event, _ in await portal.audit_events()]
    assert events.index("password_changed") < events.index("login_succeeded")
    dump = str(await portal.rows("SELECT * FROM audit_log"))
    assert NEW_PASSWORD not in dump and OTHER_PASSWORD not in dump
