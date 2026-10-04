"""Ограничение перебора по логину и по адресу (docs/portal-api.md §2.5, критерий 2)."""

import asyncio

import httpx
import pytest

from tests.support import Portal

pytestmark = pytest.mark.anyio

WRONG = {"password": "неверный-пароль"}


async def _fail_login(client: httpx.AsyncClient, login: str, times: int) -> httpx.Response:
    response = None
    for _ in range(times):
        response = await client.post("/api/auth/login", json={"login": login, **WRONG})
    assert response is not None
    return response


async def test_login_is_locked_after_consecutive_failures(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        last = await _fail_login(client, "ivanov", 5)
        assert last.status_code == 401

        locked = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        assert locked.status_code == 429
        assert locked.json()["error"]["code"] == "login_locked"
        assert locked.json()["error"]["details"] == {"retry_after_seconds": 60}
        assert locked.headers["retry-after"] == "60"

        portal.clock.advance(seconds=59)
        still = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        assert still.json()["error"]["details"] == {"retry_after_seconds": 1}

        portal.clock.advance(seconds=1)
        opened = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        assert opened.status_code == 200


async def test_lock_grows_with_each_failure_after_it_ends(portal: Portal) -> None:
    await portal.create_user("ivanov")
    async with portal.client(ip="203.0.113.1") as client:
        await _fail_login(client, "ivanov", 5)
        previous = 60
        for expected in (120, 240, 480, 960, 1920, 3600, 3600, 3600):
            portal.clock.advance(seconds=previous)  # прежняя блокировка только что закончилась
            previous = expected
            failed = await _fail_login(client, "ivanov", 1)
            assert failed.status_code == 401
            locked = await _fail_login(client, "ivanov", 1)
            assert locked.json()["error"]["details"] == {"retry_after_seconds": expected}


async def test_unknown_login_is_locked_the_same_way(portal: Portal) -> None:
    async with portal.client() as client:
        await _fail_login(client, "nobody", 5)
        locked = await _fail_login(client, "nobody", 1)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "login_locked"
    keys = await portal.rows("SELECT key FROM auth_throttle WHERE scope = 'login'")
    assert len(keys) == 1 and len(keys[0].key) == 64 and "nobody" not in keys[0].key


async def test_counter_resets_after_quiet_period(portal: Portal) -> None:
    await portal.create_user("ivanov")
    async with portal.client() as client:
        await _fail_login(client, "ivanov", 4)
        portal.clock.advance(minutes=1440)
        await _fail_login(client, "ivanov", 4)
        assert (await _fail_login(client, "ivanov", 1)).status_code == 401
        assert (await _fail_login(client, "ivanov", 1)).status_code == 429


async def test_correct_password_alone_does_not_reset_counter(portal: Portal) -> None:
    """Знающий пароль не может перебирать коды без ограничения."""
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    credentials = {"login": account.login, "password": account.password}
    wrong = "000000" if portal.code(account.secret) != "000000" else "111111"
    async with portal.client() as client:
        for _ in range(4):
            assert (await client.post("/api/auth/login", json=credentials)).status_code == 200
            response = await client.post("/api/auth/second-factor", json={"code": wrong})
            assert response.json()["error"]["code"] == "invalid_code"
        assert (await client.post("/api/auth/login", json=credentials)).status_code == 200
        # Неудача, вызвавшая блокировку, отвечает о блокировке, а не о коде.
        response = await client.post("/api/auth/second-factor", json={"code": wrong})
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "login_locked"
        assert response.json()["error"]["details"] == {"retry_after_seconds": 60}
        assert response.headers["retry-after"] == "60"
        assert (await portal.audit_events())[-1] == (
            "login_failed", {"reason": "invalid_code", "lock_seconds": 60},
        )  # fmt: skip

        # Сессия шага удалена.
        expired = await client.post("/api/auth/second-factor", json={"code": wrong})
        assert expired.status_code == 401
        assert expired.json()["error"]["code"] == "login_step_expired"
        locked = await client.post("/api/auth/login", json=credentials)
        assert locked.status_code == 429
        assert locked.json()["error"]["code"] == "login_locked"


async def test_locked_login_does_not_check_code_and_drops_step_session(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client, portal.client(ip="203.0.113.9") as attacker:
        portal.clock.advance(seconds=30)
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        await _fail_login(attacker, "ivanov", 5)
        response = await client.post(
            "/api/auth/second-factor", json={"code": portal.code(account.secret)}
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "login_locked"
        assert (await client.get("/api/auth/session")).status_code == 401


async def test_wrong_backup_code_counts_as_failure(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    credentials = {"login": account.login, "password": account.password}
    async with portal.client() as client:
        for _ in range(5):
            await client.post("/api/auth/login", json=credentials)
            await client.post("/api/auth/second-factor", json={"backup_code": "ZZZZ-ZZZZ"})
        assert (await client.post("/api/auth/login", json=credentials)).status_code == 429
    reasons = [details.get("reason") for event, details in await portal.audit_events()]
    # Отказ login_locked в журнал не пишется; о блокировке говорит вызвавшая её попытка.
    assert reasons.count("invalid_backup_code") == 5 and "login_locked" not in reasons
    events = await portal.audit_events()
    assert events[-1] == ("login_failed", {"reason": "invalid_backup_code", "lock_seconds": 60})
    assert all("lock_seconds" not in details for _, details in events[:-1])


async def test_completed_login_resets_counter(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        await _fail_login(client, "ivanov", 4)
        await portal.sign_in(client, account)
        assert await portal.rows("SELECT 1 FROM auth_throttle WHERE scope = 'login'") == []
        await _fail_login(client, "ivanov", 4)
        assert (await _fail_login(client, "ivanov", 1)).status_code == 401


async def test_address_is_limited_within_window(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client, portal.client(ip="203.0.113.77") as other:
        for number in range(20):
            await _fail_login(client, f"user{number:02d}", 1)
        limited = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "too_many_attempts"
        assert limited.json()["error"]["details"] == {"retry_after_seconds": 300}
        assert limited.headers["retry-after"] == "300"

        from_other_address = await other.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        assert from_other_address.status_code == 200

        portal.clock.advance(seconds=300)
        after_window = await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": temporary}
        )
        assert after_window.status_code == 200
    # Отказы too_many_attempts в журнал не пишутся и счётчики не увеличивают.
    events = await portal.audit_events()
    assert [details.get("reason") for _, details in events[1:]] == ["invalid_credentials"] * 20
    counters = await portal.rows(
        "SELECT failures FROM auth_throttle WHERE scope = 'ip' AND key = '203.0.113.5'"
    )
    assert [row.failures for row in counters] == [20]


async def test_address_limit_applies_to_second_factor(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        portal.clock.advance(seconds=30)
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        for number in range(20):
            await _fail_login(client, f"user{number:02d}", 1)
        response = await client.post(
            "/api/auth/second-factor", json={"code": portal.code(account.secret)}
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "too_many_attempts"


async def test_client_address_is_last_forwarded_element(portal: Portal) -> None:
    """Значения, присланные клиентом левее, не позволяют обойти ограничение по адресу."""
    for number in range(20):
        spoofed = f"198.51.100.{number}, 203.0.113.50"
        async with portal.client(ip="172.18.0.2", forwarded_for=spoofed) as client:
            await _fail_login(client, f"user{number:02d}", 1)
    async with portal.client(ip="172.18.0.2", forwarded_for="10.9.9.9, 203.0.113.50") as client:
        assert (await _fail_login(client, "someone", 1)).status_code == 429
    async with portal.client(ip="172.18.0.2", forwarded_for="203.0.113.51") as client:
        assert (await _fail_login(client, "someone", 1)).status_code == 401
    addresses = await portal.rows("SELECT key FROM auth_throttle WHERE scope = 'ip' ORDER BY key")
    assert [row.key for row in addresses] == ["203.0.113.50", "203.0.113.51"]


async def test_stale_counters_are_removed_on_write(portal: Portal) -> None:
    async with portal.client() as client:
        await _fail_login(client, "first", 1)
        portal.clock.advance(minutes=1441)
        await _fail_login(client, "second", 1)
    rows = await portal.rows("SELECT failures FROM auth_throttle WHERE scope = 'login'")
    assert [row.failures for row in rows] == [1]


async def test_address_from_repeated_forwarded_header_lines(portal: Portal) -> None:
    headers = [("X-Forwarded-For", "198.51.100.1"), ("X-Forwarded-For", "203.0.113.60")]
    async with portal.client(ip="172.18.0.2") as client:
        await client.post(
            "/api/auth/login", json={"login": "someone", **WRONG}, headers=httpx.Headers(headers)
        )
    addresses = await portal.rows("SELECT key FROM auth_throttle WHERE scope = 'ip'")
    assert [row.key for row in addresses] == ["203.0.113.60"]


async def test_parallel_guesses_cannot_outrun_the_lock(portal: Portal) -> None:
    """Попытки с одним логином идут по очереди: параллельность не даёт лишних проверок."""
    await portal.create_user("ivanov")
    async with portal.client() as client:
        responses = await asyncio.gather(
            *(client.post("/api/auth/login", json={"login": "ivanov", **WRONG}) for _ in range(12))
        )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [401] * 5 + [429] * 7


async def test_same_code_in_parallel_is_accepted_once(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    credentials = {"login": account.login, "password": account.password}
    async with portal.client() as first, portal.client() as second:
        portal.clock.advance(seconds=30)
        await first.post("/api/auth/login", json=credentials)
        await second.post("/api/auth/login", json=credentials)
        code = {"code": portal.code(account.secret)}
        responses = await asyncio.gather(
            first.post("/api/auth/second-factor", json=code),
            second.post("/api/auth/second-factor", json=code),
        )
    assert sorted(response.status_code for response in responses) == [200, 422]
    rejected = next(response for response in responses if response.status_code == 422)
    assert rejected.json()["error"]["code"] == "code_already_used"


async def test_counter_survives_the_longest_lock(portal: Portal) -> None:
    """После самой длинной блокировки нарастание не начинается заново (§2.5)."""
    await portal.create_user("ivanov")
    async with portal.client(ip="203.0.113.1") as client:
        await _fail_login(client, "ivanov", 5)
        await portal.execute(
            "UPDATE auth_throttle SET failures = 20, locked_until = :until WHERE scope = 'login'",
            until=portal.clock.now(),
        )
        portal.clock.advance(minutes=1439)
        assert (await _fail_login(client, "ivanov", 1)).status_code == 401
        locked = await _fail_login(client, "ivanov", 1)
    assert locked.json()["error"]["details"] == {"retry_after_seconds": 3600}


async def test_overlong_password_is_not_hashed_but_counts_as_failure(
    portal: Portal, monkeypatch: pytest.MonkeyPatch
) -> None:
    await portal.create_user("ivanov")
    hasher = portal.container.auth._hasher
    calls: list[str] = []
    original = hasher.verify

    def spy(password_hash: str, password: str) -> bool:
        calls.append(password)
        return original(password_hash, password)

    monkeypatch.setattr(hasher, "verify", spy)
    overlong = "я" * 129
    async with portal.client() as client:
        known = await client.post("/api/auth/login", json={"login": "ivanov", "password": overlong})
        unknown = await client.post(
            "/api/auth/login", json={"login": "nobody", "password": overlong}
        )
        assert known.status_code == unknown.status_code == 401
        assert known.json() == unknown.json()
        assert known.json()["error"]["code"] == "invalid_credentials"
        assert calls == []

        edge = await client.post("/api/auth/login", json={"login": "ivanov", "password": "я" * 128})
        assert edge.status_code == 401 and len(calls) == 1
        for _ in range(3):
            await client.post("/api/auth/login", json={"login": "ivanov", "password": overlong})
        assert (await _fail_login(client, "ivanov", 1)).status_code == 429
    failures = await portal.rows("SELECT failures FROM auth_throttle WHERE scope = 'ip'")
    assert failures[0].failures == 6


async def test_wrong_current_password_counts_and_locks_password_route(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
        new_password = "Совсем-другой-пароль-7"
        for attempt in ("не тот", "я" * 129, "не тот", "не тот"):
            response = await client.post(
                "/api/auth/password",
                json={"new_password": new_password, "current_password": attempt},
            )
            assert response.status_code == 422
            fields = response.json()["error"]["fields"]
            assert [(item["field"], item["code"]) for item in fields] == [
                ("current_password", "current_password_invalid")
            ]

        # Пятая неудача сама вызывает блокировку и отвечает о ней, а не о пароле.
        fifth = await client.post(
            "/api/auth/password", json={"new_password": new_password, "current_password": "не тот"}
        )
        assert fifth.status_code == 429
        assert fifth.json()["error"]["code"] == "login_locked"
        assert fifth.json()["error"]["details"] == {"retry_after_seconds": 60}
        assert fifth.headers["retry-after"] == "60"

        # Логин заблокирован: верный текущий пароль не проверяется, пароль не меняется.
        locked = await client.post(
            "/api/auth/password",
            json={"new_password": new_password, "current_password": account.password},
        )
        assert locked.status_code == 429
        assert locked.json()["error"]["code"] == "login_locked"
        assert locked.json()["error"]["details"] == {"retry_after_seconds": 60}
        assert locked.headers["retry-after"] == "60"
        # Сессия шага ready при этом остаётся.
        assert (await client.get("/api/auth/session")).status_code == 200

        portal.clock.advance(seconds=60)
        changed = await client.post(
            "/api/auth/password",
            json={"new_password": new_password, "current_password": account.password},
        )
        assert changed.status_code == 204
    reasons = [details.get("reason") for event, details in await portal.audit_events()]
    assert reasons.count("invalid_current_password") == 5
    assert "login_locked" not in reasons
    failed = [details for event, details in await portal.audit_events() if event == "login_failed"]
    assert failed[-1] == {"reason": "invalid_current_password", "lock_seconds": 60}
    rows = await portal.rows("SELECT scope, failures FROM auth_throttle ORDER BY scope")
    assert [tuple(row) for row in rows] == [("ip", 5), ("login", 5)]


async def test_missing_current_password_is_not_a_failure(portal: Portal) -> None:
    async with portal.client() as client:
        await portal.onboard(client, "ivanov")
        response = await client.post(
            "/api/auth/password", json={"new_password": "Новый-пароль-777"}
        )
        assert response.status_code == 422
    assert await portal.rows("SELECT 1 FROM auth_throttle") == []


async def test_reused_code_is_audited_with_its_own_reason(portal: Portal) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    async with portal.client() as client:
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        await client.post("/api/auth/second-factor", json={"code": portal.code(account.secret)})
    assert (await portal.audit_events())[-1] == ("login_failed", {"reason": "code_already_used"})


async def test_locked_and_limited_refusals_leave_no_audit_records(portal: Portal) -> None:
    await portal.create_user("ivanov")
    async with portal.client() as client:
        await _fail_login(client, "ivanov", 5)
        before = len(await portal.audit_events())
        for _ in range(3):
            assert (await _fail_login(client, "ivanov", 1)).status_code == 429
    events = await portal.audit_events()
    assert len(events) == before
    assert events[-1] == ("login_failed", {"reason": "invalid_credentials", "lock_seconds": 60})
    rows = await portal.rows("SELECT scope, failures FROM auth_throttle ORDER BY scope")
    assert [tuple(row) for row in rows] == [("ip", 5), ("login", 5)]


async def test_blocked_account_attempts_count_by_address_only(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    await portal.execute("UPDATE users SET is_blocked = true")
    credentials = {"login": "ivanov", "password": temporary}
    async with portal.client() as client:
        for _ in range(20):
            response = await client.post("/api/auth/login", json=credentials)
            assert response.json()["error"]["code"] == "account_blocked"
        limited = await client.post("/api/auth/login", json=credentials)
        assert limited.json()["error"]["code"] == "too_many_attempts"
    rows = await portal.rows("SELECT scope, failures FROM auth_throttle")
    assert [tuple(row) for row in rows] == [("ip", 20)]
    reasons = [details.get("reason") for _, details in await portal.audit_events()]
    assert reasons.count("account_blocked") == 20


@pytest.mark.parametrize(
    ("body", "reason"),
    [({"backup_code": "ZZZZ-ZZZZ"}, "invalid_backup_code"), ({"code": None}, "code_already_used")],
)
async def test_locking_failure_on_second_factor_reports_lock_with_real_reason(
    portal: Portal, body: dict[str, str | None], reason: str
) -> None:
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
    if body.get("code", "") is None:
        body = {"code": portal.code(account.secret)}  # код, уже принятый при настройке
    async with portal.client() as client:
        await _fail_login(client, "ivanov", 4)
        await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        response = await client.post("/api/auth/second-factor", json=body)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "login_locked"
        assert response.json()["error"]["details"] == {"retry_after_seconds": 60}
        assert (await client.get("/api/auth/session")).status_code == 401
    assert (await portal.audit_events())[-1] == (
        "login_failed", {"reason": reason, "lock_seconds": 60},
    )  # fmt: skip


async def test_locking_failure_on_login_still_answers_invalid_credentials(portal: Portal) -> None:
    await portal.create_user("ivanov")
    async with portal.client() as client:
        fifth = await _fail_login(client, "ivanov", 5)
    assert fifth.status_code == 401
    assert fifth.json()["error"]["code"] == "invalid_credentials"


async def test_parallel_failures_with_stale_counters_do_not_deadlock(portal: Portal) -> None:
    """Попутная чистка устаревших счётчиков не ждёт строк, занятых другими попытками."""
    logins = [f"user{number:02d}" for number in range(8)]
    for _ in range(5):
        async with portal.client() as client:
            for login in logins:
                await _fail_login(client, login, 1)
            portal.clock.advance(minutes=1441)  # все счётчики, включая адрес, устарели
            responses = await asyncio.gather(
                *(
                    client.post("/api/auth/login", json={"login": login, **WRONG})
                    for login in logins
                )
            )
        assert [response.status_code for response in responses] == [401] * 8
        rows = await portal.rows("SELECT scope, failures FROM auth_throttle ORDER BY scope, key")
        assert [tuple(row) for row in rows] == [("ip", 8)] + [("login", 1)] * 8
        portal.clock.advance(minutes=1441)
        await portal.execute("TRUNCATE auth_throttle")


async def test_setup_confirm_ignores_lockout_and_never_counts(portal: Portal) -> None:
    """Неверный код при настройке — не неудача; блокировка логина маршрут не затрагивает."""
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client, portal.client(ip="203.0.113.9") as other:
        await client.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        await client.post("/api/auth/password", json={"new_password": "Надёжный-пароль-2026"})
        secret = (await client.post("/api/auth/second-factor/setup")).json()["secret"]
        wrong = "000000" if portal.code(secret) != "000000" else "111111"
        for _ in range(7):
            response = await client.post("/api/auth/second-factor/confirm", json={"code": wrong})
            assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_code")
        assert await portal.rows("SELECT 1 FROM auth_throttle") == []
        assert "login_failed" not in [event for event, _ in await portal.audit_events()]

        await _fail_login(other, "ivanov", 5)  # логин заблокирован чужими попытками
        assert (await _fail_login(other, "ivanov", 1)).status_code == 429
        response = await client.post("/api/auth/second-factor/confirm", json={"code": wrong})
        assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_code")
        confirmed = await client.post(
            "/api/auth/second-factor/confirm", json={"code": portal.code(secret)}
        )
        assert confirmed.status_code == 200
        assert (await client.get("/api/auth/session")).json()["step"] == "ready"
