"""Тесты smoke_test.py: формирование запросов и разбор ответов без сети."""

import base64
import json
import ssl
import struct
import zlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

import smoke_test as st


def chat_response(message: dict[str, Any], finish_reason: str = "stop") -> httpx.Response:
    body = {"choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}
    return httpx.Response(200, json=body)


def text_response(content: str) -> httpx.Response:
    return chat_response({"role": "assistant", "content": content})


class Recorder:
    """Подменяет сеть: запоминает запросы и отдаёт заранее заданные ответы по очереди."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._responses: Iterator[httpx.Response] = iter(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return next(self._responses)

    def client(self) -> httpx.Client:
        settings = st.Settings(
            base_url="https://llm.example/v1", api_key="sk-bf-test", ca_cert=None
        )
        return st.build_client(settings, transport=httpx.MockTransport(self))

    def payload(self, index: int = 0) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.requests[index].content)
        return body


def run_check(check: Callable[[httpx.Client], str], *responses: httpx.Response) -> Recorder:
    recorder = Recorder(list(responses))
    with recorder.client() as client:
        check(client)
    return recorder


# --- настройки ---------------------------------------------------------------


def test_settings_base_url_derived_from_hostname() -> None:
    settings = st.load_settings({"LLM_HOSTNAME": "llm.corp.local", "LLM_API_KEY": "sk-bf-1"})
    assert settings.base_url == "https://llm.corp.local/v1"
    assert settings.ca_cert is None


def test_settings_explicit_base_url_wins_and_trailing_slash_stripped() -> None:
    env = {"LLM_BASE_URL": "https://x/v1/", "LLM_HOSTNAME": "ignored", "LLM_API_KEY": "sk-bf-1"}
    assert st.load_settings(env).base_url == "https://x/v1"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LLM_API_KEY": "sk-bf-1"}, "LLM_BASE_URL"),
        ({"LLM_HOSTNAME": "h"}, "LLM_API_KEY"),
        (
            {"LLM_HOSTNAME": "h", "LLM_API_KEY": "k", "LLM_CA_CERT": "/nonexistent.pem"},
            "LLM_CA_CERT",
        ),
    ],
)
def test_settings_errors(env: dict[str, str], message: str) -> None:
    with pytest.raises(st.SmokeTestError, match=message):
        st.load_settings(env)


def test_settings_direct_mode_defaults_to_local_vllm() -> None:
    settings = st.load_settings({"LLM_API_KEY": "vllm-key"}, direct=True)
    assert settings.base_url == st.DIRECT_BASE_URL


def test_settings_accepts_existing_ca_cert(tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy")
    env = {"LLM_HOSTNAME": "h", "LLM_API_KEY": "k", "LLM_CA_CERT": str(ca)}
    assert st.load_settings(env).ca_cert == str(ca)


@pytest.mark.parametrize(("tls_mode", "used"), [("internal", True), ("corp", False), ("", False)])
def test_caddy_root_is_default_ca_only_for_internal_tls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tls_mode: str, used: bool
) -> None:
    """После перехода на публичный сертификат caddy-root.crt остаётся на диске."""
    root = tmp_path / "caddy-root.crt"
    root.write_text("pem")
    monkeypatch.setattr(st, "INTERNAL_CA_FILE", root)
    env = {"LLM_HOSTNAME": "h", "LLM_API_KEY": "k", "TLS_MODE": tls_mode}
    assert st.load_settings(env).ca_cert == (str(root) if used else None)


def test_explicit_ca_wins_over_caddy_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "caddy-root.crt"
    explicit = tmp_path / "corp.pem"
    for path in (root, explicit):
        path.write_text("pem")
    monkeypatch.setattr(st, "INTERNAL_CA_FILE", root)
    env = {"LLM_HOSTNAME": "h", "LLM_API_KEY": "k", "TLS_MODE": "internal"}
    assert st.load_settings({**env, "LLM_CA_CERT": str(explicit)}).ca_cert == str(explicit)


@pytest.mark.parametrize(
    ("base_url", "root"),
    [
        ("https://llm.corp.local/v1", "https://llm.corp.local"),
        ("https://llm.corp.local:8443/v1", "https://llm.corp.local:8443"),
        ("http://127.0.0.1/v1", "http://127.0.0.1"),
    ],
)
def test_site_root(base_url: str, root: str) -> None:
    assert st.site_root(base_url) == root


# --- PNG ---------------------------------------------------------------------


def test_make_png_is_valid() -> None:
    png = st.make_png(3, 2, (255, 0, 10))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

    chunks: dict[bytes, bytes] = {}
    offset = 8
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", png[offset + 8 + length : offset + 12 + length])
        assert crc == zlib.crc32(kind + data)
        chunks[kind] = data
        offset += 12 + length

    assert list(chunks) == [b"IHDR", b"IDAT", b"IEND"]
    assert struct.unpack(">IIBBBBB", chunks[b"IHDR"]) == (3, 2, 8, 2, 0, 0, 0)
    assert zlib.decompress(chunks[b"IDAT"]) == (b"\x00" + b"\xff\x00\x0a" * 3) * 2


# --- валидация по схеме ------------------------------------------------------


def test_validate_schema_accepts_valid_object() -> None:
    data = {"city": "Париж", "country": "Франция", "population_millions": 2}
    assert st.validate_schema(data, st.CITY_SCHEMA) == []


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"city": "Париж", "country": "Франция"}, "нет поля 'population_millions'"),
        ({"city": "Париж", "country": "Франция", "population_millions": "2"}, "тип number"),
        ({"city": "Париж", "country": "Франция", "population_millions": True}, "тип number"),
        ({"city": "П", "country": "Ф", "population_millions": 2.1, "x": 1}, "лишнее поле 'x'"),
        (["not", "object"], "тип object"),
    ],
)
def test_validate_schema_reports_violations(data: Any, fragment: str) -> None:
    errors = st.validate_schema(data, st.CITY_SCHEMA)
    assert any(fragment in error for error in errors), errors


def test_validate_schema_checks_array_items() -> None:
    schema = {"type": "array", "items": {"type": "integer"}}
    assert st.validate_schema([1, 2], schema) == []
    assert st.validate_schema([1, 2.5], schema) == ["$[1]: ожидался тип integer, получено float"]


# --- общие свойства запросов -------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        st.text_payload(),
        st.thinking_disabled_payload(),
        st.image_payload(st.make_png(1, 1, (0, 0, 0))),
        st.json_schema_payload(),
        st.tool_call_payload(),
        st.minimal_payload(),
    ],
)
def test_payloads_use_default_model_and_low_reasoning(payload: dict[str, Any]) -> None:
    assert payload["model"] == "default"
    # §3.1: high у Qwen3.8 даёт HTTP 500, xhigh (по умолчанию) слишком долог для смоук-теста.
    assert payload["reasoning_effort"] == "low"


def test_request_goes_to_chat_completions_with_bearer_key() -> None:
    recorder = run_check(st.check_text, text_response("4"))
    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://llm.example/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-bf-test"


# --- текст -------------------------------------------------------------------


def test_check_text_passes() -> None:
    with Recorder([text_response(" 4 ")]).client() as client:
        assert st.check_text(client) == "4"


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (text_response("пять"), "ожидался ответ «4»"),
        (chat_response({"role": "assistant", "content": None}), "пустой content"),
        (chat_response({"content": "2+2="}, finish_reason="length"), "обрезан по max_tokens"),
        (
            httpx.Response(500, json={"error": {"message": "boom", "type": "InternalError"}}),
            r"HTTP 500 \[InternalError\] boom",
        ),
        (httpx.Response(200, json={"unexpected": True}), "неожиданная форма ответа"),
    ],
)
def test_check_text_failures(response: httpx.Response, fragment: str) -> None:
    with (
        Recorder([response]).client() as client,
        pytest.raises(st.SmokeTestError, match=fragment),
    ):
        st.check_text(client)


# --- enable_thinking: false -------------------------------------------------


def test_check_thinking_disabled_sends_chat_template_kwargs_and_passes() -> None:
    response = chat_response({"role": "assistant", "content": "4", "reasoning_content": None})
    recorder = run_check(st.check_thinking_disabled, response)
    assert recorder.payload()["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_check_thinking_disabled_fails_when_reasoning_present(field: str) -> None:
    response = chat_response({"role": "assistant", "content": "4", field: "Считаю: 2+2..."})
    with (
        Recorder([response]).client() as client,
        pytest.raises(st.SmokeTestError, match=f"{field}.*chat_template_kwargs"),
    ):
        st.check_thinking_disabled(client)


# --- изображение -------------------------------------------------------------


def test_check_image_sends_png_data_url_and_accepts_red() -> None:
    recorder = run_check(st.check_image, text_response("Красный."))
    content = recorder.payload()["messages"][0]["content"]
    image_url = next(part["image_url"]["url"] for part in content if part["type"] == "image_url")
    prefix = "data:image/png;base64,"
    assert image_url.startswith(prefix)
    assert base64.b64decode(image_url.removeprefix(prefix)).startswith(b"\x89PNG")


def test_check_image_rejects_wrong_color() -> None:
    with (
        Recorder([text_response("Синий")]).client() as client,
        pytest.raises(st.SmokeTestError, match="красный"),
    ):
        st.check_image(client)


# --- json_schema -------------------------------------------------------------


def test_check_json_schema_sends_response_format_and_validates() -> None:
    answer = json.dumps({"city": "Париж", "country": "Франция", "population_millions": 2.1})
    recorder = run_check(st.check_json_schema, text_response(answer))
    response_format = recorder.payload()["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == st.CITY_SCHEMA


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("Париж, Франция", "ответ не JSON"),
        ('{"city": "Париж"}', "не соответствует схеме"),
    ],
)
def test_check_json_schema_failures(content: str, fragment: str) -> None:
    with (
        Recorder([text_response(content)]).client() as client,
        pytest.raises(st.SmokeTestError, match=fragment),
    ):
        st.check_json_schema(client)


# --- tool call ---------------------------------------------------------------


def tool_call_response(name: str, arguments: str) -> httpx.Response:
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }
    message = {"role": "assistant", "content": None, "tool_calls": [tool_call]}
    return chat_response(message, finish_reason="tool_calls")


def test_check_tool_call_passes_and_sends_tools() -> None:
    recorder = run_check(
        st.check_tool_call, tool_call_response("get_weather", '{"city": "Москва"}')
    )
    payload = recorder.payload()
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "get_weather"


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (text_response("В Москве солнечно"), "нет tool_calls"),
        (tool_call_response("get_time", '{"city": "Москва"}'), "не тот инструмент"),
        (tool_call_response("get_weather", "{city: Москва"), "аргументы не JSON"),
        (tool_call_response("get_weather", '{"town": "Москва"}'), "не соответствуют схеме"),
    ],
)
def test_check_tool_call_failures(response: httpx.Response, fragment: str) -> None:
    with (
        Recorder([response]).client() as client,
        pytest.raises(st.SmokeTestError, match=fragment),
    ):
        st.check_tool_call(client)


# --- отрицательные проверки -------------------------------------------------


def test_check_key_rejected_sends_no_authorization_header() -> None:
    recorder = run_check(
        lambda client: st.check_key_rejected(client, None),
        httpx.Response(401, json={"type": "virtual_key_required", "error": {"message": "vk"}}),
    )
    request = recorder.requests[0]
    assert "Authorization" not in request.headers
    assert request.url.path == "/v1/chat/completions"
    assert recorder.payload()["max_tokens"] == 1


def test_check_key_rejected_sends_invalid_key() -> None:
    recorder = run_check(
        lambda client: st.check_key_rejected(client, st.INVALID_API_KEY),
        httpx.Response(403, json={"type": "access_blocked", "error": {"message": "no"}}),
    )
    assert recorder.requests[0].headers["Authorization"] == f"Bearer {st.INVALID_API_KEY}"


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (text_response("4"), "enforce_auth_on_inference"),
        (httpx.Response(500, text="boom"), "ожидался 401/403"),
    ],
)
def test_check_key_rejected_failures(response: httpx.Response, fragment: str) -> None:
    with (
        Recorder([response]).client() as client,
        pytest.raises(st.SmokeTestError, match=fragment),
    ):
        st.check_key_rejected(client, None)


def test_check_closed_paths_passes_on_404_without_key() -> None:
    recorder = run_check(
        lambda client: st.check_closed_paths(client, "https://llm.example"),
        *[httpx.Response(404) for _ in st.CLOSED_PATHS],
    )
    urls = [str(request.url) for request in recorder.requests]
    assert urls == [
        "https://llm.example/api/governance/virtual-keys",
        "https://llm.example/metrics",
        "https://llm.example/",
    ]
    assert all("Authorization" not in request.headers for request in recorder.requests)


def test_check_closed_paths_reports_open_paths() -> None:
    responses = [httpx.Response(404), httpx.Response(200), httpx.Response(401)]
    with (
        Recorder(responses).client() as client,
        pytest.raises(st.SmokeTestError, match=r"/metrics → HTTP 200, / → HTTP 401"),
    ):
        st.check_closed_paths(client, "https://llm.example")


def test_check_v1_closed_routes_sends_each_route_without_key() -> None:
    recorder = run_check(
        lambda client: st.check_v1_closed_routes(client, "https://llm.example"),
        *[httpx.Response(404) for _ in st.V1_CLOSED_ROUTES],
    )
    sent = [(request.method, request.url.raw_path.decode()) for request in recorder.requests]
    assert sent == [
        ("GET", "/v1/unknown-route"),
        ("GET", "/v1/mcp/tools"),
        ("GET", "/v1/skills"),
        ("GET", "/v1"),
        ("GET", "/v1/"),
        ("POST", "/v1/async/chat/completions"),
        ("POST", "/v1/mcp/tool/execute"),
    ]
    assert all("Authorization" not in request.headers for request in recorder.requests)


@pytest.mark.parametrize(
    ("index", "status", "fragment"),
    [
        # Bifrost отдаёт под неизвестным путём админ-интерфейс.
        (0, 200, "GET /v1/unknown-route → HTTP 200"),
        # Запрос дошёл до Bifrost, а не остановлен Caddy.
        (5, 401, "POST /v1/async/chat/completions → HTTP 401"),
        (6, 200, "POST /v1/mcp/tool/execute → HTTP 200"),
    ],
)
def test_check_v1_closed_routes_reports_routes_reaching_bifrost(
    index: int, status: int, fragment: str
) -> None:
    responses = [httpx.Response(404) for _ in st.V1_CLOSED_ROUTES]
    responses[index] = httpx.Response(status)
    with (
        Recorder(responses).client() as client,
        pytest.raises(st.SmokeTestError, match=fragment),
    ):
        st.check_v1_closed_routes(client, "https://llm.example")


# --- лимит ключа -------------------------------------------------------------


def limited_response() -> httpx.Response:
    body = {
        "type": "request_limited",
        "status_code": 429,
        "error": {"message": "Rate limit exceeded: request limit exceeded (3/3, resets every 1h)"},
    }
    return httpx.Response(429, json=body)


def test_check_rate_limit_passes_on_429() -> None:
    recorder = Recorder([text_response("ok"), text_response("ok"), limited_response()])
    with recorder.client() as client:
        detail = st.check_rate_limit(client, attempts=5)
    assert len(recorder.requests) == 3
    assert "запрос 3" in detail
    assert "request_limited" in detail
    assert recorder.payload()["max_tokens"] == 1


def test_check_rate_limit_fails_when_limit_never_hit() -> None:
    with (
        Recorder([text_response("ok")] * 2).client() as client,
        pytest.raises(st.SmokeTestError, match="лимит не сработал за 2"),
    ):
        st.check_rate_limit(client, attempts=2)


def test_check_rate_limit_fails_on_other_error() -> None:
    with (
        Recorder([httpx.Response(401, json={"error": {"message": "bad key"}})]).client() as client,
        pytest.raises(st.SmokeTestError, match="HTTP 401"),
    ):
        st.check_rate_limit(client, attempts=3)


# --- прогон и CLI ------------------------------------------------------------


def test_run_checks_reports_each_result(capsys: pytest.CaptureFixture[str]) -> None:
    def failing() -> str:
        raise st.SmokeTestError("сломано")

    assert st.run_checks([("ok", lambda: "детали"), ("bad", failing)]) is False
    output = capsys.readouterr().out
    assert "PASS  ok: детали" in output
    assert "FAIL  bad: сломано" in output


def tls_error() -> httpx.ConnectError:
    try:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as exc:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]") from exc
    except httpx.ConnectError as connect_error:
        return connect_error


def test_failure_message_adds_ca_hint_for_tls_errors() -> None:
    assert "LLM_CA_CERT" in st.failure_message(tls_error())
    assert st.failure_message(st.SmokeTestError("HTTP 500")) == "HTTP 500"


def test_run_checks_prints_tls_hint(capsys: pytest.CaptureFixture[str]) -> None:
    def failing() -> str:
        raise tls_error()

    assert st.run_checks([("text", failing)]) is False
    assert "SSL_CERT_FILE" in capsys.readouterr().out


def test_main_returns_1_on_unreadable_ca_cert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("not a certificate")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-bf-test")
    monkeypatch.setenv("LLM_CA_CERT", str(ca))
    assert st.main([]) == 1
    assert "не читается как PEM" in caplog.text


def test_main_returns_1_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_BASE_URL", "LLM_HOSTNAME", "LLM_API_KEY", "LLM_CA_CERT"):
        monkeypatch.delenv(name, raising=False)
    assert st.main([]) == 1


def test_parse_args_rejects_non_positive_attempts() -> None:
    with pytest.raises(SystemExit):
        st.parse_args(["--check-rate-limit", "0"])


def _check_names(*, direct: bool, **options: Any) -> list[str]:
    with Recorder([]).client() as client:
        checks = st.build_checks(client, "https://llm.example", direct=direct, **options)
        return [name for name, _ in checks]


def test_build_checks_includes_closed_paths_through_gateway() -> None:
    assert "closed_paths" in _check_names(direct=False)


def test_build_checks_skips_caddy_only_check_in_direct_mode() -> None:
    names = _check_names(direct=True)
    caddy_only = {"closed_paths", "v1_closed_routes"}
    assert not caddy_only & set(names)
    assert names == [name for name in _check_names(direct=False) if name not in caddy_only]


# --- портал (SITE_MODE=portal) ------------------------------------------------

ROOT = "https://llm.example"
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def page_response(**overrides: str) -> httpx.Response:
    headers = {"Content-Type": "text/html; charset=utf-8", **SECURITY_HEADERS, **overrides}
    return httpx.Response(200, headers=headers, text="<!doctype html>")


def portal_error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "текст"}})


@pytest.mark.parametrize(("value", "expected"), [(None, "api"), ("", "api"), ("portal", "portal")])
def test_settings_site_mode(value: str | None, expected: str) -> None:
    env = {"LLM_BASE_URL": "https://llm.example/v1", "LLM_API_KEY": "sk-bf-test"}
    if value is not None:
        env["SITE_MODE"] = value
    assert st.load_settings(env).site_mode == expected


def test_settings_rejects_unknown_site_mode() -> None:
    env = {"LLM_BASE_URL": "https://llm.example/v1", "LLM_API_KEY": "k", "SITE_MODE": "both"}
    with pytest.raises(st.SmokeTestError, match="SITE_MODE"):
        st.load_settings(env)


def test_settings_site_only_needs_no_key_and_accepts_ip_hostname() -> None:
    settings = st.load_settings({"LLM_HOSTNAME": "192.168.25.8"}, site_only=True)
    assert settings.base_url == "https://192.168.25.8/v1"
    assert settings.api_key == ""


def test_missing_security_headers_accepts_full_set_and_csp_frame_ban() -> None:
    assert st.missing_security_headers(SECURITY_HEADERS) == []
    csp_only = {
        **SECURITY_HEADERS,
        "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    }
    del csp_only["X-Frame-Options"]
    assert st.missing_security_headers(csp_only) == []


@pytest.mark.parametrize(
    ("header", "value", "fragment"),
    [
        ("Strict-Transport-Security", "", "Strict-Transport-Security"),
        ("Content-Security-Policy", "default-src *", "Content-Security-Policy"),
        ("X-Frame-Options", "SAMEORIGIN", "фреймы"),
        ("X-Content-Type-Options", "", "nosniff"),
        ("Referrer-Policy", "", "Referrer-Policy"),
    ],
)
def test_missing_security_headers_reports_each(header: str, value: str, fragment: str) -> None:
    missing = st.missing_security_headers({**SECURITY_HEADERS, header: value})
    assert len(missing) == 1
    assert fragment in missing[0]


def test_check_portal_page_requests_root_and_login_without_key() -> None:
    recorder = run_check(
        lambda client: st.check_portal_page(client, ROOT), page_response(), page_response()
    )
    assert [request.url.path for request in recorder.requests] == ["/", "/login"]
    assert all("Authorization" not in request.headers for request in recorder.requests)


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (httpx.Response(404), "/ → HTTP 404"),
        (httpx.Response(200, json={}), "не HTML"),
        (page_response(**{"Referrer-Policy": ""}), "нет заголовков: Referrer-Policy"),
    ],
)
def test_check_portal_page_failures(response: httpx.Response, fragment: str) -> None:
    with (
        Recorder([response, page_response()]).client() as client,
        pytest.raises(st.SmokeTestError, match=fragment),
    ):
        st.check_portal_page(client, ROOT)


def closed_path_responses() -> list[httpx.Response]:
    static = [httpx.Response(404, text="<!doctype html>") for _ in st.PORTAL_CLOSED_PATHS]
    api = [portal_error(404, "not_found") for _ in st.PORTAL_UNKNOWN_API_PATHS]
    return static + api


def test_check_portal_closed_paths_passes() -> None:
    recorder = run_check(
        lambda client: st.check_portal_closed_paths(client, ROOT), *closed_path_responses()
    )
    paths = [request.url.path for request in recorder.requests]
    assert paths == [*st.PORTAL_CLOSED_PATHS, *st.PORTAL_UNKNOWN_API_PATHS]
    assert {"/metrics", "/healthz", "/docs", "/openapi.json"} <= set(paths)
    assert "/api/governance/virtual-keys" in paths
    assert all("Authorization" not in request.headers for request in recorder.requests)


def test_check_portal_closed_paths_reports_open_static_path() -> None:
    responses = closed_path_responses()
    responses[0] = httpx.Response(200, text="# HELP")
    with (
        Recorder(responses).client() as client,
        pytest.raises(st.SmokeTestError, match="/metrics → HTTP 200"),
    ):
        st.check_portal_closed_paths(client, ROOT)


@pytest.mark.parametrize(
    "bifrost_response",
    [
        httpx.Response(200, json={"virtual_keys": []}),
        # 404 не от портала: формат ошибки Bifrost, а не бэкенда.
        httpx.Response(404, json={"error": {"message": "not found"}}),
        httpx.Response(404, text="404 page not found"),
    ],
)
def test_check_portal_closed_paths_requires_backend_404(bifrost_response: httpx.Response) -> None:
    responses = closed_path_responses()
    responses[len(st.PORTAL_CLOSED_PATHS)] = bifrost_response
    with (
        Recorder(responses).client() as client,
        pytest.raises(st.SmokeTestError, match="/api/governance/virtual-keys"),
    ):
        st.check_portal_closed_paths(client, ROOT)


def test_check_portal_session_required() -> None:
    recorder = run_check(
        lambda client: st.check_portal_session_required(client, ROOT),
        portal_error(401, "unauthenticated"),
    )
    request = recorder.requests[0]
    assert (request.method, request.url.path) == ("GET", "/api/auth/session")
    assert "Cookie" not in request.headers
    with (
        Recorder([httpx.Response(200, json={"step": "done"})]).client() as client,
        pytest.raises(st.SmokeTestError, match="ожидался 401"),
    ):
        st.check_portal_session_required(client, ROOT)


def test_check_portal_csrf_required_sends_post_without_header() -> None:
    recorder = run_check(
        lambda client: st.check_portal_csrf_required(client, ROOT),
        portal_error(403, "csrf_check_failed"),
    )
    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url.path.startswith("/api/")
    assert "X-Portal-Csrf" not in request.headers
    assert "Authorization" not in request.headers


@pytest.mark.parametrize(
    "response",
    [httpx.Response(204), portal_error(401, "unauthenticated"), portal_error(403, "forbidden")],
)
def test_check_portal_csrf_required_failures(response: httpx.Response) -> None:
    with (
        Recorder([response]).client() as client,
        pytest.raises(st.SmokeTestError, match="csrf_check_failed"),
    ):
        st.check_portal_csrf_required(client, ROOT)


# --- здоровье сервисов --------------------------------------------------------


def ps_row(service: str, state: str = "running", health: str = "healthy") -> str:
    return json.dumps({"Service": service, "State": state, "Health": health, "Name": "x"})


def test_parse_compose_ps_reads_lines_and_legacy_array() -> None:
    lines = "\n".join([ps_row("caddy"), ps_row("portal-worker", health="starting")])
    expected = {"caddy": ("running", "healthy"), "portal-worker": ("running", "starting")}
    assert st.parse_compose_ps(lines + "\n") == expected
    assert st.parse_compose_ps("[" + lines.replace("\n", ",") + "]") == expected
    assert st.parse_compose_ps("") == {}


def test_parse_compose_ps_rejects_garbage() -> None:
    with pytest.raises(st.SmokeTestError, match="неожиданный вывод"):
        st.parse_compose_ps("no configuration file provided")


def test_unhealthy_services_reports_missing_stopped_and_unhealthy() -> None:
    output = "\n".join(
        [
            ps_row("caddy"),
            ps_row("portal-api", health="unhealthy"),
            ps_row("portal-db", state="exited", health=""),
        ]
    )
    problems = st.unhealthy_services(["caddy", "portal-api", "portal-db", "portal-worker"], output)
    assert problems == [
        "portal-api: running unhealthy",
        "portal-db: exited",
        "portal-worker: не запущен",
    ]


def test_check_services_asks_compose_for_enabled_services() -> None:
    calls: list[tuple[str, list[str]]] = []

    def runner(compose_file: str, args: Sequence[str]) -> str:
        calls.append((compose_file, list(args)))
        if args[0] == "config":
            return "portal-worker\ncaddy\n"
        return "\n".join([ps_row("caddy"), ps_row("portal-worker")])

    assert st.check_services("compose.yml", runner) == "healthy: caddy, portal-worker"
    assert calls == [
        ("compose.yml", ["config", "--services"]),
        ("compose.yml", ["ps", "--format", "json"]),
    ]


def test_check_services_fails_when_worker_is_down() -> None:
    def runner(compose_file: str, args: Sequence[str]) -> str:
        return "caddy\nportal-worker\n" if args[0] == "config" else ps_row("caddy")

    with pytest.raises(st.SmokeTestError, match="portal-worker: не запущен"):
        st.check_services("compose.yml", runner)


def test_run_compose_reports_missing_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(st.SmokeTestError, match="не удалось выполнить docker compose"):
        st.run_compose("compose.yml", ["ps"])


# --- состав проверок ----------------------------------------------------------

MODEL_CHECKS = ["text", "thinking_disabled", "image", "json_schema", "tool_call"]
PORTAL_CHECKS = [
    "v1_closed_routes",
    "portal_page",
    "portal_closed_paths",
    "portal_session_required",
    "portal_csrf_required",
]


API_CHECKS = ["v1_closed_routes", "closed_paths"]


def test_build_checks_api_mode_is_unchanged() -> None:
    assert _check_names(direct=False) == [*MODEL_CHECKS, "no_key", "invalid_key", *API_CHECKS]


def test_build_checks_portal_mode_replaces_closed_paths() -> None:
    names = _check_names(direct=False, site_mode="portal")
    assert names == [*MODEL_CHECKS, "no_key", "invalid_key", *PORTAL_CHECKS]


def test_build_checks_site_only_makes_no_model_requests() -> None:
    assert _check_names(direct=False, site_only=True) == ["no_key", *API_CHECKS]
    names = _check_names(direct=False, site_only=True, site_mode="portal", compose_file="c.yml")
    assert names == ["no_key", *PORTAL_CHECKS, "services"]


def test_build_checks_direct_mode_ignores_site_mode() -> None:
    assert _check_names(direct=True, site_mode="portal") == _check_names(direct=True)


@pytest.mark.parametrize("extra", [["--direct"], ["--check-rate-limit", "3"]])
def test_parse_args_rejects_site_only_combinations(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        st.parse_args(["--site-only", *extra])


def test_main_site_only_runs_without_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("LLM_HOSTNAME", "LLM_API_KEY", "LLM_CA_CERT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("SITE_MODE", "portal")
    responses = [
        httpx.Response(401, json={"error": {"message": "vk"}}),
        *[httpx.Response(404) for _ in st.V1_CLOSED_ROUTES],
        page_response(),
        page_response(),
        *closed_path_responses(),
        portal_error(401, "unauthenticated"),
        portal_error(403, "csrf_check_failed"),
    ]
    transport = httpx.MockTransport(Recorder(responses))
    real_build_client = st.build_client
    monkeypatch.setattr(st, "build_client", lambda settings: real_build_client(settings, transport))
    assert st.main(["--site-only"]) == 0
    assert "ИТОГ: PASS" in capsys.readouterr().out
