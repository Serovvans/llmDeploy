"""Общие правила API: формат ошибок, CSRF, кэширование, служебные маршруты (§1, §4)."""

import dataclasses
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest

from portal.core.app import create_app
from portal.core.logging import JsonFormatter
from tests.support import Portal

pytestmark = pytest.mark.anyio

LOGIN_BODY = {"login": "ivanov", "password": "пароль"}


async def test_changing_request_without_csrf_header_is_refused(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        for headers in ({"X-Portal-Csrf": ""}, {"X-Portal-Csrf": "0"}):
            response = await client.post(
                "/api/auth/login", json={"login": "ivanov", "password": temporary}, headers=headers
            )
            assert response.status_code == 403
            assert response.json() == {
                "error": {
                    "code": "csrf_check_failed",
                    "message": "Запрос отклонён. Обновите страницу и повторите.",
                }
            }
            assert "set-cookie" not in response.headers
        del client.headers["X-Portal-Csrf"]
        assert (await client.post("/api/auth/login", json=LOGIN_BODY)).status_code == 403
        assert (await client.post("/api/no-such-route")).status_code == 403
        assert (await client.get("/api/auth/session")).status_code == 401
    assert await portal.rows("SELECT 1 FROM sessions") == []
    assert await portal.rows("SELECT 1 FROM auth_throttle") == []


async def test_every_api_response_forbids_caching(portal: Portal) -> None:
    async with portal.client() as client:
        await portal.onboard(client, "ivanov")
        responses = [
            await client.get("/api/auth/session"),
            await client.get("/api/config"),
            await client.get("/api/no-such-route"),
            await client.post("/api/auth/login", json={"login": 1}),
            await client.post("/api/auth/login", json=LOGIN_BODY, headers={"X-Portal-Csrf": "0"}),
            await client.post("/api/auth/logout"),
        ]
    assert [response.headers.get("cache-control") for response in responses] == ["no-store"] * 6


async def test_unknown_routes_answer_not_found_in_common_format(portal: Portal) -> None:
    expected = {"error": {"code": "not_found", "message": "Не найдено."}}
    async with portal.client() as client:
        for method, path in (
            ("GET", "/api/governance/virtual-keys"),
            ("POST", "/api/governance/virtual-keys"),
            ("GET", "/api/unknown"),
            ("DELETE", "/api/auth/session"),
            ("GET", "/metrics"),
            ("GET", "/"),
        ):
            response = await client.request(method, path)
            assert response.status_code == 404, path
            assert response.json() == expected


async def test_framework_documentation_is_disabled(portal: Portal) -> None:
    async with portal.client() as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/api/docs", "/api/openapi.json"):
            assert (await client.get(path)).status_code == 404


async def test_unparsable_body_is_bad_request(portal: Portal) -> None:
    async with portal.client() as client:
        for content in (b"{not json", b"[1, 2]", b""):
            response = await client.post(
                "/api/auth/login", content=content, headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "bad_request"
            assert "fields" not in response.json()["error"]


async def test_validation_error_lists_one_entry_per_field(portal: Portal) -> None:
    async with portal.client() as client:
        response = await client.post(
            "/api/auth/login", json={"login": 5, "extra": True, "another": 1}
        )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Проверьте заполнение полей."
    assert "details" not in error
    assert {(item["field"], item["code"]) for item in error["fields"]} == {
        ("login", "invalid_format"),
        ("password", "required"),
        ("extra", "invalid_format"),
        ("another", "invalid_format"),
    }
    assert all(item["message"] for item in error["fields"])


async def test_error_messages_are_russian_without_codes(portal: Portal) -> None:
    async with portal.client() as client:
        response = await client.post("/api/auth/login", json=LOGIN_BODY)
    message = response.json()["error"]["message"]
    assert message == "Неверный логин или пароль."
    assert "fields" not in response.json()["error"] and "details" not in response.json()["error"]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (RuntimeError("секретные подробности"), 500, "internal_error"),
        (ConnectionRefusedError("portal-db"), 503, "service_unavailable"),
    ],
)
async def test_unhandled_failures_use_common_format(
    portal: Portal, error: Exception, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    class Failing:
        async def authenticate(self, token: str | None) -> Any:
            raise error

        async def session_exists(self, session_id: UUID) -> bool:
            raise error

    portal.app = create_app(dataclasses.replace(portal.container, authenticator=Failing()))
    async with portal.client() as client:
        response = await client.get("/api/auth/session")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "секретные" not in response.text
    assert response.headers["cache-control"] == "no-store"
    formatter = JsonFormatter()
    output = "\n".join(formatter.format(record) for record in caplog.records)
    assert "секретные подробности" not in output and "portal-db" not in output
    failure = next(record for record in caplog.records if record.getMessage() == "request failed")
    assert failure.__dict__["error_type"] == type(error).__name__
    assert failure.exc_info is None
    assert any("test_api_format.py" in place for place in failure.__dict__["trace"])


async def test_healthz_reports_database_state(portal: Portal) -> None:
    async with portal.client() as client:
        response = await client.get("/healthz")
    assert response.status_code == 200


async def test_config_is_available_on_any_login_step(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as client:
        await client.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        response = await client.get("/api/config")
    assert response.status_code == 200
    config = response.json()
    assert set(config) == {"password", "dialogs", "chat", "kb", "docparse", "sql"}
    assert config["password"] == {"min_length": 12, "max_length": 128}
    assert config["dialogs"] == {"message_max_chars": 32000}
    assert config["chat"] == {
        "attachment_max_bytes": 20971520,
        "attachment_max_pages": 200,
        "attachment_extensions": [".jpg", ".jpeg", ".png", ".pdf", ".docx", ".txt", ".md"],
        "max_attachments": 10,
        "max_images": 8,
    }
    assert config["kb"] == {
        "document_max_bytes": 52428800,
        "document_max_pages": 500,
        "document_extensions": [".pdf", ".docx", ".txt", ".md", ".jpg", ".jpeg", ".png"],
    }
    assert set(config["docparse"]) == {
        "document_max_bytes", "max_pages", "document_extensions", "templates",
    }  # fmt: skip
    assert config["docparse"]["max_pages"] == 40
    templates = config["docparse"]["templates"]
    assert [template["id"] for template in templates] == ["egrn", "lease", "decree", "free"]
    assert all(set(item) == {"id", "title", "description", "free_form"} for item in templates)
    assert [template["free_form"] for template in templates] == [False, False, False, True]
    assert config["sql"] == {
        "dialects": [{"id": "postgres", "title": "PostgreSQL + PostGIS"}],
        "default_dialect": "postgres",
        "schema_max_chars": 50000,
    }


async def test_oversized_body_is_refused_before_session_check(portal: Portal) -> None:
    limit = portal.container.settings.server.json_body_max_bytes
    assert limit == 1048576
    expected = {
        "error": {
            "code": "request_too_large",
            "message": "Слишком большой запрос.",
            "details": {"max_bytes": limit},
        }
    }
    huge = b'{"login": "ivanov", "password": "' + b"x" * limit + b'"}'

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(huge), 65536):
            yield huge[start : start + 65536]

    json_headers = {"Content-Type": "application/json"}
    async with portal.client() as client:
        declared = await client.post("/api/auth/login", content=huge, headers=json_headers)
        streamed = await client.post("/api/auth/login", content=chunks(), headers=json_headers)
        assert "content-length" not in streamed.request.headers
        protected = await client.patch(
            "/api/admin/users/not-a-uuid", content=huge, headers=json_headers
        )
        for response in (declared, streamed, protected):
            assert response.status_code == 413
            assert response.json() == expected
            assert response.headers["cache-control"] == "no-store"

        del client.headers["X-Portal-Csrf"]
        no_csrf = await client.post("/api/auth/login", content=huge, headers=json_headers)
        assert no_csrf.json()["error"]["code"] == "csrf_check_failed"
    assert await portal.rows("SELECT 1 FROM auth_throttle") == []
    assert await portal.rows("SELECT 1 FROM audit_log") == []


async def test_body_at_the_limit_is_accepted(portal: Portal) -> None:
    limit = portal.container.settings.server.json_body_max_bytes
    prefix, suffix = b'{"login": "ivanov", "password": "', b'"}'
    body = prefix + b"x" * (limit - len(prefix) - len(suffix)) + suffix
    assert len(body) == limit
    async with portal.client() as client:
        response = await client.post(
            "/api/auth/login", content=body, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 401
