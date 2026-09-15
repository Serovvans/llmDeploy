"""Тесты smoke_test.py: формирование запросов и разбор ответов без сети."""

import base64
import json
import ssl
import struct
import zlib
from collections.abc import Callable, Iterator
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


def test_settings_accepts_existing_ca_cert(tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy")
    env = {"LLM_HOSTNAME": "h", "LLM_API_KEY": "k", "LLM_CA_CERT": str(ca)}
    assert st.load_settings(env).ca_cert == str(ca)


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
