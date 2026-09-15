"""Тесты keys.py: формирование запросов к REST Bifrost и разбор ответов без сети."""

import argparse
import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

import keys

Handler = Callable[[httpx.Request], httpx.Response]


def virtual_key(**overrides: Any) -> dict[str, Any]:
    """VirtualKey в форме ответа Bifrost (обязательные поля из API reference)."""
    key: dict[str, Any] = {
        "id": "vk-1",
        "name": "app-x",
        "value": "sk-bf-secret",
        "is_active": True,
        "provider_configs": [],
        "mcp_configs": [],
        "calendar_aligned": False,
        "config_hash": "h",
        "created_at": "2026-09-15T00:00:00Z",
        "updated_at": "2026-09-15T00:00:00Z",
    }
    return key | overrides


def make_client(handler: Handler, auth: tuple[str, str] | None = None) -> httpx.Client:
    settings = keys.Settings(admin_url="http://127.0.0.1:8080", auth=auth)
    return keys.build_client(settings, transport=httpx.MockTransport(handler))


def body(request: httpx.Request) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(request.content)
    return parsed


# --- настройки ---------------------------------------------------------------


def test_settings_defaults_to_localhost_without_auth() -> None:
    settings = keys.load_settings({})
    assert settings == keys.Settings(admin_url="http://127.0.0.1:8080", auth=None)


def test_settings_reads_url_and_basic_auth() -> None:
    env = {
        "BIFROST_ADMIN_URL": "http://localhost:18080/",
        "BIFROST_ADMIN_USERNAME": "admin",
        "BIFROST_ADMIN_PASSWORD": "pw",
    }
    assert keys.load_settings(env) == keys.Settings("http://localhost:18080", ("admin", "pw"))


def test_settings_rejects_partial_auth() -> None:
    with pytest.raises(keys.BifrostApiError, match="задаются вместе"):
        keys.load_settings({"BIFROST_ADMIN_USERNAME": "admin"})


# --- create ------------------------------------------------------------------


def test_build_create_payload_with_both_limits() -> None:
    payload = keys.build_create_payload("app-x", "ERP", requests=100, tokens=50_000, period="1h")
    assert payload == {
        "name": "app-x",
        "description": "ERP",
        "is_active": True,
        "allow_all_providers": True,
        "rate_limit": {
            "request_max_limit": 100,
            "request_reset_duration": "1h",
            "token_max_limit": 50_000,
            "token_reset_duration": "1h",
        },
    }


def test_build_create_payload_only_requests_limit() -> None:
    payload = keys.build_create_payload("smoke", None, requests=3, tokens=None, period="1m")
    assert "description" not in payload
    assert payload["rate_limit"] == {"request_max_limit": 3, "request_reset_duration": "1m"}


def test_build_create_payload_without_limits() -> None:
    payload = keys.build_create_payload("unlimited", None, requests=None, tokens=None, period="1h")
    assert "rate_limit" not in payload


def test_create_key_posts_payload_with_basic_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"message": "created", "virtual_key": virtual_key()})

    payload = keys.build_create_payload("app-x", None, 10, None, "1h")
    with make_client(handler, auth=("admin", "pw")) as client:
        created = keys.create_key(client, payload)

    assert created["value"] == "sk-bf-secret"
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:8080/api/governance/virtual-keys"
    assert body(request) == payload
    expected_auth = "Basic " + base64.b64encode(b"admin:pw").decode()
    assert request.headers["Authorization"] == expected_auth


def test_create_key_rejects_response_without_virtual_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "created"})

    with make_client(handler) as client, pytest.raises(keys.BifrostApiError, match="virtual_key"):
        keys.create_key(client, {"name": "x"})


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize(
    ("auth", "fragment"),
    [(None, "задайте BIFROST_ADMIN_USERNAME"), (("admin", "wrong"), "проверьте BIFROST_ADMIN")],
)
def test_auth_errors_hint_admin_credentials(
    status: int, auth: tuple[str, str] | None, fragment: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "unauthorized"}})

    with (
        make_client(handler, auth=auth) as client,
        pytest.raises(keys.BifrostApiError, match=fragment),
    ):
        keys.create_key(client, {"name": "x"})


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, html="<!doctype html><title>Bifrost</title>"),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
def test_success_status_with_non_json_object_body_is_api_error(response: httpx.Response) -> None:
    with (
        make_client(lambda request: response) as client,
        pytest.raises(keys.BifrostApiError, match="ответ не JSON-объект"),
    ):
        keys.list_keys(client)


def test_bad_request_error_includes_bifrost_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        error = {"type": "invalid_request", "error": {"message": "invalid reset duration"}}
        return httpx.Response(400, json=error)

    with (
        make_client(handler) as client,
        pytest.raises(keys.BifrostApiError, match=r"HTTP 400.*invalid reset duration"),
    ):
        keys.create_key(client, {"name": "x"})


# --- list --------------------------------------------------------------------


def test_list_keys_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keys, "LIST_PAGE_SIZE", 2)
    all_keys = [virtual_key(id=f"vk-{i}") for i in range(3)]
    offsets: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/governance/virtual-keys"
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        offsets.append(request.url.params.get("offset"))
        page = all_keys[offset : offset + limit]
        return httpx.Response(
            200,
            json={
                "virtual_keys": page,
                "count": len(page),
                "total_count": len(all_keys),
                "limit": limit,
                "offset": offset,
            },
        )

    with make_client(handler) as client:
        result = keys.list_keys(client)

    assert [key["id"] for key in result] == ["vk-0", "vk-1", "vk-2"]
    assert offsets == ["0", "2"]


def test_list_keys_stops_on_empty_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"virtual_keys": [], "count": 0, "total_count": 5})

    with make_client(handler) as client:
        assert keys.list_keys(client) == []


def test_format_keys_table_shows_limits_and_hides_values() -> None:
    limited = virtual_key(
        rate_limit={
            "id": "rl",
            "request_max_limit": 100,
            "request_reset_duration": "1h",
            "request_current_usage": 7,
            "token_current_usage": 0,
        }
    )
    revoked = virtual_key(id="vk-2", name="old", is_active=False)
    lines = keys.format_keys_table([limited, revoked]).splitlines()

    assert lines[0].split() == ["ID", "NAME", "ACTIVE", "REQUESTS", "TOKENS"]
    assert lines[1].split() == ["vk-1", "app-x", "yes", "7/100", "за", "1h", "-"]
    assert lines[2].split() == ["vk-2", "old", "no", "-", "-"]
    assert all("sk-bf-secret" not in line for line in lines)


# --- revoke ------------------------------------------------------------------


def test_revoke_key_deactivates_via_put() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"message": "updated", "virtual_key": virtual_key(is_active=False)}
        )

    with make_client(handler) as client:
        revoked = keys.revoke_key(client, "vk-1")

    assert revoked["is_active"] is False
    assert seen[0].method == "PUT"
    assert seen[0].url.path == "/api/governance/virtual-keys/vk-1"
    assert body(seen[0]) == {"is_active": False}


def test_revoke_key_fails_if_still_active() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "updated", "virtual_key": virtual_key()})

    with make_client(handler) as client, pytest.raises(keys.BifrostApiError, match="активен"):
        keys.revoke_key(client, "vk-1")


def test_revoke_unknown_key_reports_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "virtual key not found"}})

    with make_client(handler) as client, pytest.raises(keys.BifrostApiError, match="HTTP 404"):
        keys.revoke_key(client, "missing")


# --- CLI ---------------------------------------------------------------------


def test_parse_args_create_defaults() -> None:
    args = keys.parse_args(["create", "--name", "app-x", "--requests", "60"])
    assert (args.command, args.name, args.requests, args.tokens, args.period) == (
        "create",
        "app-x",
        60,
        None,
        "1h",
    )


@pytest.mark.parametrize(
    "argv", [["create", "--name", "x", "--requests", "0"], ["create"], ["revoke"], []]
)
def test_parse_args_rejects_invalid(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        keys.parse_args(argv)


def test_run_command_create_prints_key(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "created", "virtual_key": virtual_key()})

    args = argparse.Namespace(
        command="create", name="app-x", description=None, requests=5, tokens=None, period="1h"
    )
    with make_client(handler) as client:
        keys.run_command(args, client)

    output = capsys.readouterr().out
    assert "vk-1" in output
    assert "sk-bf-secret" in output


def test_main_returns_1_on_html_response(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html>UI</html>")

    original = keys.build_client
    monkeypatch.setattr(
        keys, "build_client", lambda settings: original(settings, httpx.MockTransport(html))
    )
    for name in ("BIFROST_ADMIN_URL", "BIFROST_ADMIN_USERNAME", "BIFROST_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert keys.main(["list"]) == 1
    assert "ответ не JSON-объект" in caplog.text


def test_main_reports_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    original = keys.build_client
    monkeypatch.setattr(
        keys, "build_client", lambda settings: original(settings, httpx.MockTransport(refuse))
    )
    for name in ("BIFROST_ADMIN_URL", "BIFROST_ADMIN_USERNAME", "BIFROST_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert keys.main(["list"]) == 1
