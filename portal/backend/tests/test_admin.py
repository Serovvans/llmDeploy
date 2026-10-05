"""Администрирование учётных записей (docs/portal-api.md §3, критерий приёмки 2)."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from tests.support import Account, Portal

pytestmark = pytest.mark.anyio

USERS = "/api/admin/users"


@pytest.fixture
async def admin(portal: Portal) -> AsyncIterator[httpx.AsyncClient]:
    async with portal.client() as client:
        await portal.onboard(client, "admin", role="admin")
        yield client


async def _create(admin: httpx.AsyncClient, login: str, role: str = "employee") -> dict[str, str]:
    response = await admin.post(
        USERS, json={"full_name": f"Сотрудник {login}", "login": login, "role": role}
    )
    assert response.status_code == 201, response.text
    user: dict[str, str] = response.json()["user"]
    return user


async def _employee(portal: Portal, client: httpx.AsyncClient, login: str = "petrova") -> Account:
    return await portal.onboard(client, login)


async def _listed(admin: httpx.AsyncClient, login: str) -> dict[str, str | None]:
    items = (await admin.get(USERS, params={"q": login})).json()["items"]
    found: dict[str, str | None] = next(item for item in items if item["login"] == login)
    return found


async def _id_of(admin: httpx.AsyncClient, login: str) -> str:
    found = (await _listed(admin, login))["id"]
    assert found is not None
    return found


async def _fail_login(client: httpx.AsyncClient, login: str, times: int = 5) -> None:
    """Неудачные входы; пять подряд закрывают вход для логина на минуту (§2.5)."""
    for _ in range(times):
        await client.post("/api/auth/login", json={"login": login, "password": "неверный"})


async def _login_throttle(portal: Portal) -> list[int]:
    rows = await portal.rows(
        "SELECT failures FROM auth_throttle WHERE scope = 'login' ORDER BY failures"
    )
    return [row.failures for row in rows]


async def test_create_user_returns_temporary_password_once(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    response = await admin.post(
        USERS,
        json={"full_name": "  Петрова Анна Сергеевна ", "login": "Petrova", "role": "employee"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["user"] == {
        "id": body["user"]["id"],
        "login": "petrova",
        "full_name": "Петрова Анна Сергеевна",
        "role": "employee",
        "state": "never_logged_in",
        "login_locked_until": None,
        "second_factor_configured": False,
        "is_me": False,
        "created_at": "2026-10-05T09:00:00Z",
    }
    password = body["temporary_password"]
    groups = password.split("-")
    assert all(len(group) == 4 for group in groups) and len("".join(groups)) >= 12

    async with portal.client() as client:
        login = await client.post(
            "/api/auth/login", json={"login": "petrova", "password": password}
        )
        assert login.json()["step"] == "password_change"
    assert password not in str(await portal.rows("SELECT * FROM users"))
    assert ("user_created", {}) in await portal.audit_events()


async def test_create_user_validation(admin: httpx.AsyncClient) -> None:
    await _create(admin, "petrova")
    taken = await admin.post(
        USERS, json={"full_name": "Другая", "login": "PETROVA", "role": "employee"}
    )
    assert taken.status_code == 409
    assert taken.json()["error"]["code"] == "login_taken"

    cases = [
        ({"full_name": "Имя", "login": "ab", "role": "employee"}, ("login", "invalid_format")),
        ({"full_name": "Имя", "login": "-abc", "role": "employee"}, ("login", "invalid_format")),
        ({"full_name": "Имя", "login": "иванов", "role": "employee"}, ("login", "invalid_format")),
        ({"full_name": "Имя", "login": "a" * 33, "role": "employee"}, ("login", "invalid_format")),
        ({"full_name": "Имя", "login": "sidorov", "role": "owner"}, ("role", "unknown_value")),
        ({"full_name": "  ", "login": "sidorov", "role": "employee"}, ("full_name", "required")),
        (
            {"full_name": "я" * 201, "login": "sidorov", "role": "employee"},
            ("full_name", "too_long"),
        ),
        ({"login": "sidorov", "role": "employee"}, ("full_name", "required")),
    ]
    for body, expected in cases:
        response = await admin.post(USERS, json=body)
        assert response.status_code == 422, body
        fields = response.json()["error"]["fields"]
        assert [(item["field"], item["code"]) for item in fields] == [expected]


async def test_list_users_pages_search_and_sort(admin: httpx.AsyncClient) -> None:
    for login in ("borisov", "andreev", "vasiliev"):
        await _create(admin, login)
    page = (await admin.get(USERS)).json()
    assert page["page"] == 1 and page["page_size"] == 50 and page["total"] == 4
    assert [item["login"] for item in page["items"]] == ["admin", "andreev", "borisov", "vasiliev"]
    assert [item["is_me"] for item in page["items"]] == [True, False, False, False]
    assert page["items"][0]["state"] == "active"

    second = (await admin.get(USERS, params={"page": 2, "page_size": 3, "order": "desc"})).json()
    assert [item["login"] for item in second["items"]] == ["admin"]
    assert second["total"] == 4

    assert (await admin.get(USERS, params={"q": "BORIS"})).json()["total"] == 1
    assert (await admin.get(USERS, params={"q": "сотрудник"})).json()["total"] == 3
    assert (await admin.get(USERS, params={"q": "%"})).json()["total"] == 0

    for params in ({"page": 0}, {"page_size": 101}, {"sort": "login"}, {"order": "up"}):
        response = await admin.get(USERS, params=params)
        assert response.status_code == 422
        assert response.json()["error"]["fields"][0]["field"] == next(iter(params))


async def test_employee_cannot_use_admin_routes(portal: Portal, admin: httpx.AsyncClient) -> None:
    async with portal.client() as client:
        await _employee(portal, client)
        target = await _id_of(admin, "admin")
        requests = [
            ("GET", USERS, None),
            ("POST", USERS, {"full_name": "Имя", "login": "sidorov", "role": "admin"}),
            ("PATCH", f"{USERS}/{target}", {"role": "employee"}),
            ("POST", f"{USERS}/{target}/reset-password", None),
            ("POST", f"{USERS}/{target}/reset-second-factor", None),
            ("POST", f"{USERS}/{target}/unlock-login", None),
            ("POST", f"{USERS}/{target}/block", None),
            ("POST", f"{USERS}/{target}/unblock", None),
        ]
        for method, path, body in requests:
            response = await client.request(method, path, json=body)
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "forbidden"
    assert (await admin.get("/api/auth/session")).status_code == 200


async def test_admin_cannot_act_on_own_account(admin: httpx.AsyncClient) -> None:
    me = await _id_of(admin, "admin")
    requests = [
        ("PATCH", f"{USERS}/{me}", {"full_name": "Новое имя"}),
        ("PATCH", f"{USERS}/{me}", {"role": "employee"}),
        ("POST", f"{USERS}/{me}/reset-password", None),
        ("POST", f"{USERS}/{me}/reset-second-factor", None),
        ("POST", f"{USERS}/{me}/unlock-login", None),
        ("POST", f"{USERS}/{me}/block", None),
    ]
    for method, path, body in requests:
        response = await admin.request(method, path, json=body)
        assert response.status_code == 409, path
        assert response.json()["error"]["code"] == "cannot_modify_self"
    session = (await admin.get("/api/auth/session")).json()
    assert session["user"]["role"] == "admin" and session["user"]["full_name"] != "Новое имя"


async def test_unknown_user_is_not_found(admin: httpx.AsyncClient) -> None:
    for target in (str(uuid4()), "not-a-uuid"):
        for method, suffix, body in (
            ("PATCH", "", {"role": "admin"}),
            ("POST", "/reset-password", None),
            ("POST", "/reset-second-factor", None),
            ("POST", "/unlock-login", None),
            ("POST", "/block", None),
            ("POST", "/unblock", None),
        ):
            response = await admin.request(method, f"{USERS}/{target}{suffix}", json=body)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"


async def test_block_ends_sessions_immediately_and_unblock_restores(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    async with portal.client() as client:
        account = await _employee(portal, client)
        target = await _id_of(admin, "petrova")
        blocked = await admin.post(f"{USERS}/{target}/block")
        assert blocked.status_code == 200 and blocked.json()["state"] == "blocked"

        assert (await client.get("/api/auth/session")).status_code == 401
        portal.clock.advance(seconds=30)
        denied = await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        assert denied.json()["error"]["code"] == "account_blocked"

        again = await admin.post(f"{USERS}/{target}/block")
        assert again.status_code == 200

        unblocked = await admin.post(f"{USERS}/{target}/unblock")
        assert unblocked.json()["state"] == "active"
        await portal.sign_in(client, account)
    events = [event for event, _ in await portal.audit_events()]
    assert events.count("user_blocked") == 1 and events.count("user_unblocked") == 1


async def test_reset_password_ends_sessions_and_forces_change(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    async with portal.client() as client:
        account = await _employee(portal, client)
        target = await _id_of(admin, "petrova")
        reset = await admin.post(f"{USERS}/{target}/reset-password")
        assert reset.status_code == 200
        assert (await client.get("/api/auth/session")).status_code == 401

        portal.clock.advance(seconds=30)
        old = await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        assert old.status_code == 401
        new = await client.post(
            "/api/auth/login",
            json={"login": account.login, "password": reset.json()["temporary_password"]},
        )
        # Сначала второй фактор: временный пароль в чужих руках не даёт сменить пароль.
        assert new.json()["step"] == "second_factor"
    assert "password_reset" in [event for event, _ in await portal.audit_events()]


async def test_reset_second_factor_removes_secret_codes_and_sessions(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    async with portal.client() as client:
        account = await _employee(portal, client)
        target = await _id_of(admin, "petrova")
        reset = await admin.post(f"{USERS}/{target}/reset-second-factor")
        assert reset.status_code == 200
        assert reset.json()["second_factor_configured"] is False
        assert (await client.get("/api/auth/session")).status_code == 401
        assert await portal.rows("SELECT 1 FROM backup_codes") != []  # коды администратора
        row = (await portal.rows("SELECT * FROM users WHERE login = 'petrova'"))[0]
        assert row.totp_secret is None and row.totp_enabled is False and row.totp_last_step is None
        assert await portal.rows("SELECT 1 FROM backup_codes WHERE user_id = :id", id=row.id) == []

        login = await client.post(
            "/api/auth/login", json={"login": account.login, "password": account.password}
        )
        assert login.json()["step"] == "second_factor_setup"
        new_secret = (await client.post("/api/auth/second-factor/setup")).json()["secret"]
        assert new_secret != account.secret

        repeated = await admin.post(f"{USERS}/{target}/reset-second-factor")
        assert repeated.status_code == 200
    events = [event for event, _ in await portal.audit_events()]
    assert events.count("second_factor_reset") == 2  # второй сброс стёр начатую настройку

    # Сброс у пользователя без ключа ничего не меняет и в журнал не пишется.
    untouched = await _create(admin, "sidorov")
    assert (await admin.post(f"{USERS}/{untouched['id']}/reset-second-factor")).status_code == 200
    events = [event for event, _ in await portal.audit_events()]
    assert events.count("second_factor_reset") == 2


async def test_update_changes_name_without_touching_sessions(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    async with portal.client() as client:
        await _employee(portal, client)
        target = await _id_of(admin, "petrova")
        response = await admin.patch(f"{USERS}/{target}", json={"full_name": "Петрова Анна"})
        assert response.status_code == 200 and response.json()["full_name"] == "Петрова Анна"
        session = await client.get("/api/auth/session")
        assert session.json()["user"]["full_name"] == "Петрова Анна"
    events = await portal.audit_events()
    assert events[-1] == ("user_updated", {"fields": ["full_name"]})
    assert "Петрова Анна" not in str(await portal.rows("SELECT details FROM audit_log"))


async def test_update_role_ends_sessions_and_is_audited(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    async with portal.client() as client:
        account = await _employee(portal, client)
        target = await _id_of(admin, "petrova")
        response = await admin.patch(
            f"{USERS}/{target}", json={"role": "admin", "full_name": "Петрова А. С."}
        )
        assert response.json()["role"] == "admin"
        assert (await client.get("/api/auth/session")).status_code == 401
        await portal.sign_in(client, account)
        assert (await client.get(USERS)).status_code == 200
    assert (await portal.audit_events())[-2] == (
        "user_updated",
        {"fields": ["full_name", "role"], "role_before": "employee", "role_after": "admin"},
    )


async def test_update_without_changes_is_silent(portal: Portal, admin: httpx.AsyncClient) -> None:
    async with portal.client() as client:
        await _employee(portal, client)
        target = await _id_of(admin, "petrova")
        current = (await admin.get(USERS, params={"q": "petrova"})).json()["items"][0]
        response = await admin.patch(
            f"{USERS}/{target}", json={"role": "employee", "full_name": current["full_name"]}
        )
        assert response.status_code == 200 and response.json() == current
        assert (await client.get("/api/auth/session")).status_code == 200
    assert "user_updated" not in [event for event, _ in await portal.audit_events()]


async def test_update_rejects_empty_body_and_foreign_fields(admin: httpx.AsyncClient) -> None:
    target = (await _create(admin, "petrova"))["id"]
    for body in (
        {},
        {"login": "new-login"},
        {"full_name": "Имя", "is_blocked": True},
        {"role": "x"},
    ):
        response = await admin.patch(f"{USERS}/{target}", json=body)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "validation_error"
        assert response.json()["error"]["fields"]
    assert (await admin.get(USERS, params={"q": "petrova"})).json()["items"][0][
        "login"
    ] == "petrova"


async def test_admin_actions_record_actor_and_address(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    target = (await _create(admin, "petrova"))["id"]
    await admin.post(f"{USERS}/{target}/block")
    rows = await portal.rows(
        "SELECT a.event, actor.login AS actor, subject.login AS subject, host(a.ip) AS ip "
        "FROM audit_log a JOIN users actor ON actor.id = a.actor_id "
        "JOIN users subject ON subject.id = a.subject_user_id "
        "WHERE a.event IN ('user_created', 'user_blocked') AND a.actor_id IS NOT NULL ORDER BY a.id"
    )
    assert [tuple(row) for row in rows] == [
        ("user_created", "admin", "petrova", "203.0.113.5"),
        ("user_blocked", "admin", "petrova", "203.0.113.5"),
    ]


async def test_session_and_role_are_checked_before_the_resource(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    """Порядок §1.2: сессия → роль → ресурс, в том числе для пути с не-UUID."""
    async with portal.client() as anonymous, portal.client() as employee:
        await _employee(portal, employee)
        for target in ("not-a-uuid", str(uuid4())):
            for method, suffix, body in (
                ("PATCH", "", {"role": "admin"}),
                ("POST", "/block", None),
                ("POST", "/reset-password", None),
                ("POST", "/unlock-login", None),
            ):
                path = f"{USERS}/{target}{suffix}"
                refused = await anonymous.request(method, path, json=body)
                assert (refused.status_code, refused.json()["error"]["code"]) == (
                    401, "unauthenticated",
                )  # fmt: skip
                denied = await employee.request(method, path, json=body)
                assert (denied.status_code, denied.json()["error"]["code"]) == (403, "forbidden")
    assert (await admin.post(f"{USERS}/not-a-uuid/block")).status_code == 404


@pytest.mark.parametrize("route", ["reset-password", "reset-second-factor"])
async def test_admin_reset_unlocks_login_but_not_address(
    portal: Portal, admin: httpx.AsyncClient, route: str
) -> None:
    async with portal.client(ip="203.0.113.40") as client:
        account = await _employee(portal, client)
        target = await _id_of(admin, "petrova")
        for _ in range(5):
            await client.post("/api/auth/login", json={"login": "petrova", "password": "неверный"})
        credentials = {"login": account.login, "password": account.password}
        assert (await client.post("/api/auth/login", json=credentials)).status_code == 429

        reset = await admin.post(f"{USERS}/{target}/{route}")
        assert reset.status_code == 200
        if route == "reset-password":
            credentials["password"] = reset.json()["temporary_password"]
        assert (await client.post("/api/auth/login", json=credentials)).status_code == 200
    rows = await portal.rows("SELECT scope, key, failures FROM auth_throttle")
    assert [tuple(row) for row in rows] == [("ip", "203.0.113.40", 5)]


LOCKED_UNTIL = "2026-10-05T09:01:00Z"
ATTACKER_IP = "203.0.113.40"


async def test_login_lock_is_shown_while_it_lasts_whatever_the_state(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    async with portal.client(ip=ATTACKER_IP) as client:
        await _fail_login(client, "petrova")  # счётчик ведётся и для логина, которого ещё нет
        created = await _create(admin, "petrova")
        target = created["id"]
        assert (created["state"], created["login_locked_until"]) == (
            "never_logged_in", LOCKED_UNTIL,
        )  # fmt: skip
        await _fail_login(client, "admin", times=4)  # только счётчик — не блокировка

        page = (await admin.get(USERS)).json()["items"]
        assert [(item["login"], item["login_locked_until"]) for item in page] == [
            ("admin", None), ("petrova", LOCKED_UNTIL),
        ]  # fmt: skip

        answers = [
            await admin.post(f"{USERS}/{target}/block"),
            await admin.patch(f"{USERS}/{target}", json={"full_name": "Новое имя"}),
            await admin.post(f"{USERS}/{target}/reset-second-factor"),  # ключа нет: без изменений
        ]
        assert [(a.json()["state"], a.json()["login_locked_until"]) for a in answers] == [
            ("blocked", LOCKED_UNTIL)
        ] * 3
        unblocked = (await admin.post(f"{USERS}/{target}/unblock")).json()
        assert (unblocked["state"], unblocked["login_locked_until"]) == (
            "never_logged_in", LOCKED_UNTIL,
        )  # fmt: skip

        portal.clock.advance(seconds=60)
        assert (await _listed(admin, "petrova"))["login_locked_until"] is None  # срок вышел
        assert await _login_throttle(portal) == [4, 5]

        await _fail_login(client, "petrova", times=1)
        assert (await _listed(admin, "petrova"))["login_locked_until"] == "2026-10-05T09:03:00Z"
        reset = (await admin.post(f"{USERS}/{target}/reset-password")).json()
        assert reset["user"]["login_locked_until"] is None
    assert "failures" not in str(page) and ATTACKER_IP not in str(page)


async def test_unlock_login_lets_the_user_in_with_the_same_password_and_code(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    async with portal.client() as working, portal.client(ip=ATTACKER_IP) as client:
        account = await _employee(portal, working)
        target = await _id_of(admin, "petrova")
        await _fail_login(client, "petrova")
        credentials = {"login": account.login, "password": account.password}
        assert (await client.post("/api/auth/login", json=credentials)).status_code == 429
        before = await portal.rows("SELECT * FROM users WHERE login = 'petrova'")

        response = await admin.post(f"{USERS}/{target}/unlock-login")
        assert response.status_code == 200
        assert response.json() == {
            "user": {**await _listed(admin, "petrova"), "login_locked_until": None},
            "unlocked": True,
        }
        assert response.json()["user"]["state"] == "active"
        rows = await portal.rows("SELECT scope, key, failures FROM auth_throttle")
        assert [tuple(row) for row in rows] == [("ip", ATTACKER_IP, 5)]
        assert await portal.rows("SELECT * FROM users WHERE login = 'petrova'") == before
        assert len(await portal.rows("SELECT 1 FROM backup_codes WHERE used_at IS NULL")) == 20

        assert (await working.get("/api/auth/session")).status_code == 200  # сессия жива
        await portal.sign_in(client, account)  # прежние пароль и код, до конца срока блокировки
    rows = await portal.rows(
        "SELECT actor.login AS actor, subject.login AS subject, host(a.ip) AS ip, a.details "
        "FROM audit_log a JOIN users actor ON actor.id = a.actor_id "
        "JOIN users subject ON subject.id = a.subject_user_id WHERE a.event = 'login_unlocked'"
    )
    assert [tuple(row) for row in rows] == [("admin", "petrova", "203.0.113.5", {})]


async def test_unlock_login_without_a_lock_changes_nothing(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    target = (await _create(admin, "petrova"))["id"]
    unlock = f"{USERS}/{target}/unlock-login"

    async def unlocked() -> bool:
        response = await admin.post(unlock)
        assert response.status_code == 200
        assert response.json()["user"]["login_locked_until"] is None
        result: bool = response.json()["unlocked"]
        return result

    async with portal.client(ip=ATTACKER_IP) as client:
        assert await unlocked() is False  # строки нет
        await _fail_login(client, "petrova", times=4)
        assert await unlocked() is False  # только счётчик: он не тронут
        assert await _login_throttle(portal) == [4]

        await _fail_login(client, "petrova", times=1)
        portal.clock.advance(seconds=60)
        assert await unlocked() is False  # срок вышел
        assert await _login_throttle(portal) == [5]

        # У заблокированной администратором записи снимается только блокировка входа.
        await _fail_login(client, "petrova", times=1)
        await admin.post(f"{USERS}/{target}/block")
        response = await admin.post(unlock)
        assert (response.json()["unlocked"], response.json()["user"]["state"]) == (True, "blocked")
        assert await _login_throttle(portal) == []
        assert await unlocked() is False  # повторный вызов
    events = [event for event, _ in await portal.audit_events()]
    assert events.count("login_unlocked") == 1


async def test_unlock_login_refuses_own_account_and_forged_requests(
    portal: Portal, admin: httpx.AsyncClient
) -> None:
    me = await _id_of(admin, "admin")
    target = (await _create(admin, "petrova"))["id"]
    async with portal.client(ip=ATTACKER_IP) as client:
        await _fail_login(client, "admin")
        await _fail_login(client, "petrova")
    assert (await _listed(admin, "admin"))["login_locked_until"] == LOCKED_UNTIL

    own = await admin.post(f"{USERS}/{me}/unlock-login")
    assert (own.status_code, own.json()["error"]["code"]) == (409, "cannot_modify_self")
    forged = await admin.post(f"{USERS}/{target}/unlock-login", headers={"X-Portal-Csrf": ""})
    assert (forged.status_code, forged.json()["error"]["code"]) == (403, "csrf_check_failed")
    assert await _login_throttle(portal) == [5, 5]
    assert "login_unlocked" not in [event for event, _ in await portal.audit_events()]
