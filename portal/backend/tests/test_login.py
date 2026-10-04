"""Вход, шаги входа и второй фактор (docs/portal-api.md §2.3–2.4, критерий приёмки 1)."""

import asyncio

import pytest

from tests.support import NEW_PASSWORD, Portal

pytestmark = pytest.mark.anyio


async def test_first_login_walks_through_all_steps(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        response = await client.post(
            "/api/auth/login", json={"login": "Ivanov", "password": temporary}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["step"] == "password_change"
        assert body["user"]["login"] == "ivanov"
        assert body["user"]["second_factor_configured"] is False
        assert body["user"]["backup_codes"] is None

        response = await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
        assert response.status_code == 200
        assert response.json()["step"] == "second_factor_setup"

        setup = (await client.post("/api/auth/second-factor/setup")).json()
        assert len(setup["secret"]) == 32
        assert setup["qr"]["size"] >= 21
        assert setup["qr"]["path"].startswith("M0 0h7v1h-7z")
        again = (await client.post("/api/auth/second-factor/setup")).json()
        assert again == setup

        response = await client.post(
            "/api/auth/second-factor/confirm", json={"code": portal.code(setup["secret"])}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session"]["step"] == "ready"
        assert body["session"]["user"]["backup_codes"] == {"remaining": 10, "total": 10}
        assert len(body["backup_codes"]) == 10
        assert all(len(code) == 9 and code[4] == "-" for code in body["backup_codes"])

        session = (await client.get("/api/auth/session")).json()
        assert session["step"] == "ready"
        assert session["user"]["second_factor_configured"] is True


async def test_session_cookie_attributes(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        response = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("portal_session=")
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/api", "Max-Age=1200"):
        assert attribute in cookie
    assert "Domain" not in cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    assert len(token) == 43
    stored = await portal.rows("SELECT token_hash FROM sessions")
    assert token.encode() not in bytes(stored[0].token_hash)


async def test_cookie_lifetime_extends_when_login_completes(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        portal.clock.advance(seconds=30)
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        portal.clock.advance(seconds=10)
        response = await client.post(
            "/api/auth/second-factor", json={"code": portal.code(account.secret)}
        )
    assert f"Max-Age={12 * 3600 - 10}" in response.headers["set-cookie"]


async def test_login_requires_second_factor_and_hides_user_until_passed(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        portal.clock.advance(seconds=30)
        response = await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        assert response.json() == {"step": "second_factor", "user": None}
        assert (await client.get("/api/auth/session")).json() == {
            "step": "second_factor",
            "user": None,
        }

        gated = await client.get("/api/admin/users")
        assert gated.status_code == 403
        assert gated.json()["error"]["code"] == "login_step_required"
        assert gated.json()["error"]["details"] == {"step": "second_factor"}
        assert (await client.get("/api/config")).status_code == 200

        response = await client.post(
            "/api/auth/second-factor", json={"code": portal.code(account.secret)}
        )
        assert response.status_code == 200
        assert response.json()["backup_code_used"] is False
        assert response.json()["session"]["user"]["login"] == "ivanov"


async def test_wrong_code_is_rejected(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        portal.clock.advance(seconds=30)
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        wrong = "000000" if portal.code(account.secret) != "000000" else "111111"
        for code in (wrong, "12345", "abcdef"):
            response = await client.post("/api/auth/second-factor", json={"code": code})
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_code"
        assert (await client.get("/api/auth/session")).json()["step"] == "second_factor"


async def test_reused_code_is_rejected(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    used_code = portal.code(account.secret)
    async with portal.client() as client:
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        response = await client.post("/api/auth/second-factor", json={"code": used_code})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "code_already_used"

        portal.clock.advance(seconds=30)
        fresh = portal.code(account.secret)
        assert (
            await client.post("/api/auth/second-factor", json={"code": fresh})
        ).status_code == 200
    async with portal.client() as client:
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        response = await client.post("/api/auth/second-factor", json={"code": fresh})
        assert response.json()["error"]["code"] == "code_already_used"


async def test_code_window_accepts_neighbour_step_and_rejects_stale(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        portal.clock.advance(seconds=120)
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        stale = await client.post(
            "/api/auth/second-factor", json={"code": portal.code(account.secret, steps=-2)}
        )
        assert stale.json()["error"]["code"] == "invalid_code"
        future = await client.post(
            "/api/auth/second-factor", json={"code": portal.code(account.secret, steps=2)}
        )
        assert future.json()["error"]["code"] == "invalid_code"
        neighbour = await client.post(
            "/api/auth/second-factor", json={"code": portal.code(account.secret, steps=-1)}
        )
        assert neighbour.status_code == 200


async def test_code_with_spaces_is_accepted(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        portal.clock.advance(seconds=30)
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        code = portal.code(account.secret)
        response = await client.post(
            "/api/auth/second-factor", json={"code": f"{code[:3]} {code[3:]}"}
        )
        assert response.status_code == 200


async def test_backup_code_works_exactly_once(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    backup = account.backup_codes[0]
    async with portal.client() as client:
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        response = await client.post(
            "/api/auth/second-factor", json={"backup_code": backup.lower().replace("-", " ")}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["backup_code_used"] is True
        assert body["session"]["user"]["backup_codes"] == {"remaining": 9, "total": 10}
    async with portal.client() as client:
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        response = await client.post("/api/auth/second-factor", json={"backup_code": backup})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_backup_code"
        response = await client.post("/api/auth/second-factor", json={"backup_code": "ZZZZ-ZZZZ"})
        assert response.json()["error"]["code"] == "invalid_backup_code"


async def test_backup_codes_are_stored_only_as_hmac(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    rows = await portal.rows("SELECT code_hmac FROM backup_codes")
    assert len(rows) == 10
    plain = {code.replace("-", "").encode() for code in account.backup_codes}
    assert all(len(row.code_hmac) == 32 and bytes(row.code_hmac) not in plain for row in rows)


async def test_totp_secret_is_encrypted_at_rest(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    stored = bytes((await portal.rows("SELECT totp_secret FROM users"))[0].totp_secret)
    assert account.secret.encode() not in stored
    assert len(stored) == 12 + 32 + 16


async def test_second_factor_body_needs_exactly_one_field(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        for body in ({}, {"code": "123456", "backup_code": "AAAA-AAAA"}):
            response = await client.post("/api/auth/second-factor", json=body)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "validation_error"
    # Отказ в разборе тела попыткой перебора не считается.
    assert await portal.rows("SELECT 1 FROM auth_throttle") == []


async def test_unknown_login_and_wrong_password_answer_identically(portal: Portal) -> None:
    await portal.create_user("ivanov")
    async with portal.client() as client:
        unknown = await client.post(
            "/api/auth/login", json={"login": "nobody", "password": "что-нибудь-длинное"}
        )
        wrong = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": "что-нибудь-длинное"}
        )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert unknown.json()["error"]["code"] == "invalid_credentials"
    assert "set-cookie" not in unknown.headers and "set-cookie" not in wrong.headers


async def test_unknown_login_still_runs_password_hash(
    portal: Portal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Для неизвестного логина Argon2 проверяется на заглушечном хеше — время ответа то же."""
    hasher = portal.container.auth._hasher
    calls: list[str] = []
    original = hasher.verify

    def spy(password_hash: str, password: str) -> bool:
        calls.append(password_hash)
        return original(password_hash, password)

    monkeypatch.setattr(hasher, "verify", spy)
    async with portal.client() as client:
        await client.post("/api/auth/login", json={"login": "nobody", "password": "пароль"})
    assert len(calls) == 1 and calls[0].startswith("$argon2id$")


async def test_blocked_account_is_revealed_only_with_correct_password(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    await portal.execute("UPDATE users SET is_blocked = true")
    async with portal.client() as client:
        wrong = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": "неверный-пароль"}
        )
        assert wrong.status_code == 401
        assert wrong.json()["error"]["code"] == "invalid_credentials"
        right = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        assert right.status_code == 403
        assert right.json()["error"]["code"] == "account_blocked"
        assert "set-cookie" not in right.headers


async def test_step_routes_without_session_report_expired_step(portal: Portal) -> None:
    async with portal.client() as client:
        for path, body in (
            ("/api/auth/second-factor", {"code": "123456"}),
            ("/api/auth/second-factor/setup", None),
            ("/api/auth/second-factor/confirm", {"code": "123456"}),
        ):
            response = await client.post(path, json=body)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "login_step_expired"
        response = await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
        assert response.json()["error"]["code"] == "unauthenticated"


async def test_unfinished_login_expires_after_step_ttl(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        await client.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        portal.clock.advance(minutes=20)
        response = await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "login_step_expired"
        assert "Max-Age=0" in response.headers["set-cookie"]
    assert await portal.rows("SELECT 1 FROM sessions") == []


async def test_wrong_step_is_reported_with_current_step(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        await client.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        response = await client.post("/api/auth/second-factor/setup")
        assert response.status_code == 403
        assert response.json()["error"] == {
            "code": "login_step_required",
            "message": "Сначала завершите вход.",
            "details": {"step": "password_change"},
        }


async def test_confirm_without_setup_and_with_wrong_code(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        await client.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
        response = await client.post("/api/auth/second-factor/confirm", json={"code": "123456"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "setup_not_started"

        secret = (await client.post("/api/auth/second-factor/setup")).json()["secret"]
        wrong = "000000" if portal.code(secret) != "000000" else "111111"
        response = await client.post("/api/auth/second-factor/confirm", json={"code": wrong})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_code"
        assert (await client.get("/api/auth/session")).json()["step"] == "second_factor_setup"


async def test_audit_records_logins_without_typed_login(portal: Portal) -> None:
    async with portal.client() as client:
        await portal.onboard(client, "ivanov")
        await client.post(
            "/api/auth/login", json={"login": "Секрет-в-поле-логина", "password": "x"}
        )
    events = await portal.audit_events()
    assert [event for event, _ in events] == [
        "user_created", "password_changed", "login_succeeded", "login_failed",
    ]  # fmt: skip
    assert events[0][1] == {"via": "cli"}
    assert events[3][1] == {"reason": "invalid_credentials"}
    rows = await portal.rows("SELECT subject_user_id, ip::text AS ip FROM audit_log ORDER BY id")
    assert rows[3].subject_user_id is None
    assert rows[3].ip == "203.0.113.5/32"
    dump = str(await portal.rows("SELECT * FROM audit_log")) + str(
        await portal.rows("SELECT * FROM auth_throttle")
    )
    assert "Секрет" not in dump and "секрет" not in dump


async def test_parallel_setup_requests_get_one_secret_and_one_code_set(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        await client.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})
        setups = await asyncio.gather(
            *(client.post("/api/auth/second-factor/setup") for _ in range(4))
        )
        secrets = {response.json()["secret"] for response in setups}
        assert len(secrets) == 1

        code = {"code": portal.code(secrets.pop())}
        confirms = await asyncio.gather(
            client.post("/api/auth/second-factor/confirm", json=code),
            client.post("/api/auth/second-factor/confirm", json=code),
        )
    assert sorted(response.status_code for response in confirms) == [200, 403]
    assert len(await portal.rows("SELECT 1 FROM backup_codes")) == 10
