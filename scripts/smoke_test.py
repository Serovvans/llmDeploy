"""Смоук-тест API LLM-сервиса (Caddy → Bifrost → vLLM), docs/design.md §9, §12.

С флагом ``--direct`` проверяется только модель, напрямую на ``http://127.0.0.1:8000/v1``
с ключом ``VLLM_API_KEY``: это первый этап развёртывания, когда Caddy и Bifrost ещё не
подняты (§3.7). Проверка закрытых путей при этом пропускается — её обеспечивает Caddy.

Проверки через клиентский эндпоинт с ключом Bifrost, модель ``default``:
текст; ``chat_template_kwargs: {"enable_thinking": false}`` доходит до vLLM через Bifrost
(в ответе нет рассуждений); изображение; ``response_format: json_schema`` (ответ валидируется
по схеме); tool call. Отрицательные проверки (лимит ключа не расходуют): запрос без ключа
и с неверным ключом получает 401/403; ``/api/governance/virtual-keys``, ``/metrics`` и ``/``
через 443 отдают 404. Caddy проксирует в Bifrost только четыре пути —
``/v1/chat/completions``, ``/v1/completions``, ``/v1/embeddings``, ``/v1/models``
(docs/design.md §3.3); остальное под ``/v1`` — 404 при любом ``SITE_MODE``.

При ``SITE_MODE=portal`` (третий этап, docs/portal-design.md §3, §3.2) вместо «всё, кроме
``/v1/*``, — 404» проверяется портал: ``/`` и ``/login`` отдают страницу с заголовками
безопасности; ``/api/governance/virtual-keys`` — 404 от бэкенда портала; ``/metrics``,
``/healthz``, ``/docs``, ``/redoc``, ``/openapi.json`` — 404; ``GET /api/auth/session`` без
cookie — 401; ``POST`` без ``X-Portal-Csrf`` — 403.

С флагом ``--site-only`` выполняются только проверки обвязки (запрос без ключа и то, что
отдаёт сайт): ключ не нужен, к модели запросов нет. С ``--compose-file ФАЙЛ`` добавляется
проверка, что все сервисы включённых профилей запущены и здоровы (``docker compose ps``;
только на машине, где работает стек).

С флагом ``--check-rate-limit N`` выполняется только проверка лимита: до N запросов подряд,
ожидается HTTP 429 от Bifrost. Для неё нужен отдельный ключ с малым лимитом запросов
(например, ``keys.py create --name smoke-limit --requests 3``).

Qwen3.8 (§3.1): мышление включено по умолчанию с ``reasoning_effort=xhigh``, значение
``high`` даёт HTTP 500. Поэтому все запросы явно отправляются с ``reasoning_effort=low``.

Окружение:
    LLM_BASE_URL  проверяемый эндпоинт; по умолчанию ``https://${LLM_HOSTNAME}/v1``,
                  а с ``--direct`` — ``http://127.0.0.1:8000/v1``. Через проброшенный порт
                  (docs/portal-design.md §3.2) — ``https://llm.<домен>:18443/v1``: корень
                  сайта для проверок обвязки берётся из этого же адреса, вместе с портом.
    LLM_HOSTNAME  DNS-имя сервиса, если LLM_BASE_URL не задан.
    LLM_API_KEY   ключ Bifrost ``sk-bf-...``; с ``--direct`` — VLLM_API_KEY (обязателен,
                  кроме ``--site-only``).
    SITE_MODE     что сайт отдаёт помимо ``/v1/*``: ``api`` (по умолчанию) или ``portal``;
                  та же переменная, что в ``deploy/.env``.
    LLM_CA_CERT   путь к PEM корпоративного CA для проверки TLS. Для сертификата
                  корпоративного CA фактически обязателен: httpx доверяет только набору
                  certifi, а не системному хранилищу ОС. Альтернатива — SSL_CERT_FILE.
                  При ``TLS_MODE=internal`` — ``deploy/caddy-root.crt``; адресом может быть
                  и IP (``LLM_HOSTNAME=192.168.25.8``).
    TLS_MODE      та же переменная, что в ``deploy/.env``. При ``internal`` и незаданном
                  LLM_CA_CERT берётся ``deploy/caddy-root.crt`` этого клона, если он есть.
                  При других значениях файл не используется: после перехода на публичный
                  сертификат он остаётся на диске, но проверять по нему уже нельзя.

Код выхода: 0 — все проверки прошли, 1 — хотя бы одна не прошла или ошибка конфигурации.
"""

import argparse
import base64
import json
import logging
import os
import ssl
import struct
import subprocess
import sys
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from api_errors import describe_error

logger = logging.getLogger("smoke_test")

MODEL = "default"
REASONING_EFFORT = "low"
CHAT_PATH = "chat/completions"
# Первый запрос после старта vLLM (компиляция, прогрев) бывает долгим.
REQUEST_TIMEOUT_S = 300.0
MAX_TOKENS = 2048
TEXT_PROMPT = "Сколько будет 2+2? Ответь только числом."
INVALID_API_KEY = "sk-bf-invalid-smoke-test"
# Порт vLLM на ВМ открыт только на loopback (§3.7, §8).
DIRECT_BASE_URL = "http://127.0.0.1:8000/v1"
# Корень собственного CA Caddy: его выгружает install.sh при TLS_MODE=internal.
INTERNAL_CA_FILE = Path(__file__).resolve().parent.parent / "deploy" / "caddy-root.crt"
# Через 443 Caddy отдаёт только четыре пути API (docs/design.md §3.3); остальное,
# включая API управления Bifrost, — 404.
CLOSED_PATHS = ("/api/governance/virtual-keys", "/metrics", "/")
# Под /v1 закрыто всё, кроме тех четырёх путей, при любом SITE_MODE: Bifrost принимал
# /v1/async/* и /v1/mcp/* без ключа, а под неизвестными путями отдавал админ-интерфейс.
V1_CLOSED_ROUTES = (
    ("GET", "/v1/unknown-route"),
    ("GET", "/v1/mcp/tools"),
    ("GET", "/v1/skills"),
    ("GET", "/v1"),
    ("GET", "/v1/"),
    ("POST", "/v1/async/chat/completions"),
    ("POST", "/v1/mcp/tool/execute"),
)
SITE_MODES = ("api", "portal")
# Не /api/*: уходят в статику портала, а не в бэкенд и не в Bifrost (docs/portal-api.md §1.1).
PORTAL_CLOSED_PATHS = ("/metrics", "/healthz", "/docs", "/redoc", "/openapi.json")
# /api/* принадлежит порталу: админ-API Bifrost и документации там нет — 404 от бэкенда.
PORTAL_UNKNOWN_API_PATHS = ("/api/governance/virtual-keys", "/api/docs", "/api/openapi.json")
PORTAL_PAGE_PATHS = ("/", "/login")
PORTAL_SESSION_PATH = "/api/auth/session"
# Любой POST /api/*: без заголовка X-Portal-Csrf бэкенд отказывает раньше, чем смотрит сессию.
PORTAL_CSRF_PROBE_PATH = "/api/auth/logout"
COMPOSE_TIMEOUT_S = 60
# vLLM отдаёт рассуждения в reasoning_content (старые версии) или reasoning (новые).
REASONING_FIELDS = ("reasoning_content", "reasoning")
TLS_HINT = (
    "ошибка TLS: задайте/проверьте LLM_CA_CERT (PEM корпоративного CA) или SSL_CERT_FILE — "
    "httpx не использует системное хранилище сертификатов"
)

JsonObject = dict[str, Any]

CITY_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "country": {"type": "string"},
        "population_millions": {"type": "number"},
    },
    "required": ["city", "country", "population_millions"],
    "additionalProperties": False,
}

WEATHER_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}

_JSON_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
}


class SmokeTestError(Exception):
    """Проверка не прошла или конфигурация некорректна."""


@dataclass(frozen=True)
class Settings:
    """Параметры подключения к клиентскому эндпоинту."""

    base_url: str
    api_key: str
    ca_cert: str | None
    site_mode: str = "api"


def load_settings(
    env: Mapping[str, str], *, direct: bool = False, site_only: bool = False
) -> Settings:
    """Прочитать настройки из переменных окружения.

    ``direct`` — проверка vLLM напрямую; ``site_only`` — без запросов к модели, ключ не нужен.
    """
    base_url = env.get("LLM_BASE_URL", "").strip()
    if not base_url:
        hostname = env.get("LLM_HOSTNAME", "").strip()
        if direct:
            base_url = DIRECT_BASE_URL
        elif hostname:
            base_url = f"https://{hostname}/v1"
        else:
            raise SmokeTestError("задайте LLM_BASE_URL или LLM_HOSTNAME")
    api_key = env.get("LLM_API_KEY", "").strip()
    if not api_key and not site_only:
        raise SmokeTestError(
            "задайте LLM_API_KEY (ключ Bifrost sk-bf-..., с --direct — VLLM_API_KEY)"
        )
    ca_cert = env.get("LLM_CA_CERT", "").strip() or None
    if ca_cert and not os.path.isfile(ca_cert):
        raise SmokeTestError(f"LLM_CA_CERT указывает на несуществующий файл: {ca_cert}")
    if not ca_cert and env.get("TLS_MODE", "").strip() == "internal" and INTERNAL_CA_FILE.is_file():
        ca_cert = str(INTERNAL_CA_FILE)
    site_mode = env.get("SITE_MODE", "").strip() or "api"
    if site_mode not in SITE_MODES:
        raise SmokeTestError(f"SITE_MODE={site_mode!r}: ожидается api или portal")
    return Settings(
        base_url=base_url.rstrip("/"), api_key=api_key, ca_cert=ca_cert, site_mode=site_mode
    )


def site_root(base_url: str) -> str:
    """Вернуть корень сайта из клиентского эндпоинта: ``https://host/v1`` → ``https://host``."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def build_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Создать HTTP-клиент с ключом и, если задан, корпоративным CA.

    ``transport`` позволяет подменить сетевой слой (в тестах — ``httpx.MockTransport``).
    """
    verify: ssl.SSLContext | bool = (
        ssl.create_default_context(cafile=settings.ca_cert) if settings.ca_cert else True
    )
    return httpx.Client(
        base_url=settings.base_url,
        headers={"Authorization": f"Bearer {settings.api_key}"},
        verify=verify,
        timeout=REQUEST_TIMEOUT_S,
        transport=transport,
    )


def make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Сгенерировать однотонный PNG (8 бит, RGB) средствами стандартной библиотеки."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width  # 0 — фильтр строки «None»
    pixels = zlib.compress(row * height)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def validate_schema(value: Any, schema: JsonObject, path: str = "$") -> list[str]:
    """Проверить значение по JSON Schema; вернуть список нарушений (пустой — валидно).

    Поддерживается подмножество, используемое в этом тесте: ``type``, ``properties``,
    ``required``, ``additionalProperties: false``, ``items``.
    """
    expected = schema.get("type")
    if expected is not None and not _JSON_TYPES[expected](value):
        return [f"{path}: ожидался тип {expected}, получено {type(value).__name__}"]

    errors: list[str] = []
    if isinstance(value, dict):
        properties: JsonObject = schema.get("properties", {})
        errors += [
            f"{path}: нет поля {key!r}" for key in schema.get("required", []) if key not in value
        ]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: лишнее поле {key!r}" for key in value if key not in properties]
        for key, subschema in properties.items():
            if key in value:
                errors += validate_schema(value[key], subschema, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += validate_schema(item, schema["items"], f"{path}[{index}]")
    return errors


def chat_payload(messages: list[JsonObject], **extra: Any) -> JsonObject:
    """Собрать тело запроса chat/completions с моделью и уровнем мышления проекта."""
    return {
        "model": MODEL,
        "messages": messages,
        "reasoning_effort": REASONING_EFFORT,
        "max_tokens": MAX_TOKENS,
        **extra,
    }


def text_payload() -> JsonObject:
    """Запрос для проверки текстовой генерации."""
    return chat_payload([{"role": "user", "content": TEXT_PROMPT}])


def thinking_disabled_payload() -> JsonObject:
    """Текстовый запрос с ``chat_template_kwargs: {"enable_thinking": false}``."""
    return chat_payload(
        [{"role": "user", "content": TEXT_PROMPT}],
        chat_template_kwargs={"enable_thinking": False},
    )


def image_payload(png: bytes) -> JsonObject:
    """Запрос для проверки зрения: изображение как data URL."""
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    content = [
        {"type": "text", "text": "Какого цвета это изображение? Ответь одним словом."},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    return chat_payload([{"role": "user", "content": content}])


def json_schema_payload() -> JsonObject:
    """Запрос structured output с ``response_format: json_schema``."""
    return chat_payload(
        [{"role": "user", "content": "Дай сведения о столице Франции."}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "city_info", "schema": CITY_SCHEMA, "strict": True},
        },
    )


def tool_call_payload() -> JsonObject:
    """Запрос, в ответ на который модель должна вызвать инструмент ``get_weather``."""
    tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Текущая погода в указанном городе",
            "parameters": WEATHER_TOOL_PARAMETERS,
        },
    }
    return chat_payload(
        [{"role": "user", "content": "Какая сейчас погода в Москве? Используй инструмент."}],
        tools=[tool],
        tool_choice="auto",
    )


def minimal_payload() -> JsonObject:
    """Минимальный по стоимости запрос: для проверок лимита и авторизации."""
    return chat_payload([{"role": "user", "content": "ok"}], max_tokens=1)


def complete(client: httpx.Client, payload: JsonObject) -> JsonObject:
    """Отправить chat/completions и вернуть ``choices[0].message``; ошибки — SmokeTestError."""
    response = client.post(CHAT_PATH, json=payload)
    if response.status_code != httpx.codes.OK:
        raise SmokeTestError(describe_error(response))
    try:
        choice = response.json()["choices"][0]
        message: JsonObject = choice["message"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise SmokeTestError(f"неожиданная форма ответа: {response.text[:500]}") from exc
    if choice.get("finish_reason") == "length":
        raise SmokeTestError(
            f"ответ обрезан по max_tokens={payload['max_tokens']} (мышление съело бюджет токенов?)"
        )
    return message


def _content(message: JsonObject) -> str:
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise SmokeTestError(f"пустой content в ответе: {message}")
    return content.strip()


def _expect_four(answer: str) -> str:
    if "4" not in answer:
        raise SmokeTestError(f"ожидался ответ «4», получено: {answer!r}")
    return answer


def check_text(client: httpx.Client) -> str:
    """Текст: модель отвечает на простой вопрос."""
    return _expect_four(_content(complete(client, text_payload())))


def check_thinking_disabled(client: httpx.Client) -> str:
    """``enable_thinking: false`` доходит до vLLM: в ответе нет рассуждений (§13)."""
    message = complete(client, thinking_disabled_payload())
    present = [field for field in REASONING_FIELDS if message.get(field)]
    if present:
        raise SmokeTestError(
            f"в ответе есть {', '.join(present)} при enable_thinking=false: Bifrost, вероятно, "
            "отбрасывает chat_template_kwargs; клиентам рекомендовать reasoning_effort=low"
        )
    return _expect_four(_content(message))


def check_image(client: httpx.Client) -> str:
    """Изображение: модель распознаёт цвет однотонной красной картинки."""
    png = make_png(64, 64, (220, 20, 20))
    answer = _content(complete(client, image_payload(png)))
    if not any(word in answer.lower() for word in ("красн", "red")):
        raise SmokeTestError(f"ожидался «красный», получено: {answer!r}")
    return answer


def check_json_schema(client: httpx.Client) -> str:
    """Structured output: ответ — JSON, валидный по CITY_SCHEMA."""
    answer = _content(complete(client, json_schema_payload()))
    try:
        data = json.loads(answer)
    except json.JSONDecodeError as exc:
        raise SmokeTestError(f"ответ не JSON: {answer!r}") from exc
    errors = validate_schema(data, CITY_SCHEMA)
    if errors:
        raise SmokeTestError("ответ не соответствует схеме: " + "; ".join(errors))
    return answer


def check_tool_call(client: httpx.Client) -> str:
    """Tool call: модель вызывает get_weather с аргументами по схеме."""
    message = complete(client, tool_call_payload())
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        content = message.get("content")
        raise SmokeTestError(f"нет tool_calls (проверьте --tool-call-parser vLLM): {content!r}")
    function = tool_calls[0].get("function", {})
    if function.get("name") != "get_weather":
        raise SmokeTestError(f"вызван не тот инструмент: {function.get('name')!r}")
    try:
        arguments = json.loads(function.get("arguments", ""))
    except json.JSONDecodeError as exc:
        raise SmokeTestError(f"аргументы не JSON: {function.get('arguments')!r}") from exc
    errors = validate_schema(arguments, WEATHER_TOOL_PARAMETERS)
    if errors:
        raise SmokeTestError("аргументы не соответствуют схеме: " + "; ".join(errors))
    return f"get_weather({function['arguments']})"


def check_rate_limit(client: httpx.Client, attempts: int) -> str:
    """Лимит ключа: в пределах ``attempts`` запросов Bifrost должен ответить HTTP 429."""
    payload = minimal_payload()
    for attempt in range(1, attempts + 1):
        response = client.post(CHAT_PATH, json=payload)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return f"запрос {attempt}: {describe_error(response)}"
        if response.status_code != httpx.codes.OK:
            raise SmokeTestError(f"запрос {attempt}: {describe_error(response)}")
        logger.info("rate-limit: запрос %d/%d принят", attempt, attempts)
    raise SmokeTestError(
        f"лимит не сработал за {attempts} запросов: у ключа нет лимита или он больше {attempts}"
    )


def send_with_key(
    client: httpx.Client, method: str, url: str, api_key: str | None, body: JsonObject | None = None
) -> httpx.Response:
    """Отправить запрос с указанным ключом вместо ключа клиента; ``None`` — без Authorization."""
    request = client.build_request(method, url, json=body)
    if api_key is None:
        del request.headers["Authorization"]
    else:
        request.headers["Authorization"] = f"Bearer {api_key}"
    return client.send(request)


def check_key_rejected(client: httpx.Client, api_key: str | None) -> str:
    """Запрос без ключа или с неверным ключом Bifrost отклоняет с 401/403."""
    response = send_with_key(client, "POST", CHAT_PATH, api_key, minimal_payload())
    if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return describe_error(response)
    if response.is_success:
        raise SmokeTestError(
            f"запрос принят (HTTP {response.status_code}): проверьте "
            "client.enforce_auth_on_inference в deploy/bifrost/config.json "
            "(с --direct — VLLM_API_KEY в окружении контейнера vllm)"
        )
    raise SmokeTestError(f"ожидался 401/403, получено: {describe_error(response)}")


def check_closed_paths(client: httpx.Client, root: str) -> str:
    """Всё, кроме путей API под ``/v1``, через 443 отдаёт 404 (запросы без ключа)."""
    unexpected = []
    for path in CLOSED_PATHS:
        response = send_with_key(client, "GET", root + path, api_key=None)
        if response.status_code != httpx.codes.NOT_FOUND:
            unexpected.append(f"{path} → HTTP {response.status_code}")
    if unexpected:
        raise SmokeTestError(
            "ожидался 404 (Caddy должен проксировать только четыре пути API под /v1): "
            + ", ".join(unexpected)
        )
    return "404: " + ", ".join(CLOSED_PATHS)


def check_v1_closed_routes(client: httpx.Client, root: str) -> str:
    """Под ``/v1`` всё, кроме четырёх путей API, отдаёт 404 от Caddy (запросы без ключа)."""
    routes = [f"{method} {path}" for method, path in V1_CLOSED_ROUTES]
    unexpected = []
    for (method, path), route in zip(V1_CLOSED_ROUTES, routes, strict=True):
        body: JsonObject | None = {} if method == "POST" else None
        response = send_with_key(client, method, root + path, api_key=None, body=body)
        if response.status_code != httpx.codes.NOT_FOUND:
            unexpected.append(f"{route} → HTTP {response.status_code}")
    if unexpected:
        raise SmokeTestError(
            "ожидался 404 (Caddy должен проксировать только /v1/chat/completions, "
            "/v1/completions, /v1/embeddings и /v1/models): " + ", ".join(unexpected)
        )
    return "404: " + ", ".join(routes)


def _get_without_key(client: httpx.Client, url: str) -> httpx.Response:
    return send_with_key(client, "GET", url, api_key=None)


def portal_error_code(response: httpx.Response) -> str | None:
    """Вернуть ``error.code`` из ответа бэкенда портала (docs/portal-api.md §1.3) или ``None``."""
    try:
        body = response.json()
    except json.JSONDecodeError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _describe_portal(response: httpx.Response) -> str:
    code = portal_error_code(response)
    return f"HTTP {response.status_code}" + (f" {code}" if code else "")


def missing_security_headers(headers: Mapping[str, str]) -> list[str]:
    """Вернуть, каких заголовков безопасности из docs/portal-design.md §3.2 нет в ответе."""
    csp = headers.get("Content-Security-Policy", "")
    frames_denied = "frame-ancestors 'none'" in csp or headers.get("X-Frame-Options") == "DENY"
    present = {
        "Strict-Transport-Security": "max-age=" in headers.get("Strict-Transport-Security", ""),
        "Content-Security-Policy (default-src 'self')": "default-src 'self'" in csp,
        "запрет встраивания во фреймы": frames_denied,
        "X-Content-Type-Options: nosniff": headers.get("X-Content-Type-Options") == "nosniff",
        "Referrer-Policy": bool(headers.get("Referrer-Policy")),
    }
    return [name for name, ok in present.items() if not ok]


def check_portal_page(client: httpx.Client, root: str) -> str:
    """Портал: ``/`` и страница входа отдают HTML с заголовками безопасности (ПД §3.2)."""
    problems = []
    for path in PORTAL_PAGE_PATHS:
        response = _get_without_key(client, root + path)
        if response.status_code != httpx.codes.OK:
            problems.append(f"{path} → HTTP {response.status_code}")
        elif "text/html" not in response.headers.get("Content-Type", ""):
            problems.append(f"{path} → не HTML: {response.headers.get('Content-Type')!r}")
        elif missing := missing_security_headers(response.headers):
            problems.append(f"{path} → нет заголовков: {', '.join(missing)}")
    if problems:
        raise SmokeTestError(
            "ожидалась страница портала (SITE_MODE=portal в deploy/.env, сервис portal-web): "
            + "; ".join(problems)
        )
    return "200 и заголовки безопасности: " + ", ".join(PORTAL_PAGE_PATHS)


def check_portal_closed_paths(client: httpx.Client, root: str) -> str:
    """Портал: админ-API Bifrost, ``/metrics``, документация и служебные маршруты — 404."""
    unexpected = []
    for path in PORTAL_CLOSED_PATHS:
        response = _get_without_key(client, root + path)
        if response.status_code != httpx.codes.NOT_FOUND:
            unexpected.append(f"{path} → HTTP {response.status_code}")
    for path in PORTAL_UNKNOWN_API_PATHS:
        response = _get_without_key(client, root + path)
        # Код ошибки в формате бэкенда отличает ответ портала от ответа Bifrost.
        if (
            response.status_code != httpx.codes.NOT_FOUND
            or portal_error_code(response) != "not_found"
        ):
            unexpected.append(f"{path} → {_describe_portal(response)}, ожидался 404 not_found")
    if unexpected:
        raise SmokeTestError("через 443 доступно лишнее: " + ", ".join(unexpected))
    return "404: " + ", ".join(PORTAL_CLOSED_PATHS + PORTAL_UNKNOWN_API_PATHS)


def check_portal_session_required(client: httpx.Client, root: str) -> str:
    """Портал: без cookie сессии ``GET /api/auth/session`` — 401."""
    response = _get_without_key(client, root + PORTAL_SESSION_PATH)
    if response.status_code != httpx.codes.UNAUTHORIZED:
        raise SmokeTestError(
            f"{PORTAL_SESSION_PATH} без cookie: ожидался 401, получено {_describe_portal(response)}"
        )
    return _describe_portal(response)


def check_portal_csrf_required(client: httpx.Client, root: str) -> str:
    """Портал: ``POST /api/*`` без заголовка ``X-Portal-Csrf`` — 403 (docs/portal-api.md §2.2)."""
    response = send_with_key(client, "POST", root + PORTAL_CSRF_PROBE_PATH, api_key=None)
    if (
        response.status_code != httpx.codes.FORBIDDEN
        or portal_error_code(response) != "csrf_check_failed"
    ):
        raise SmokeTestError(
            f"POST {PORTAL_CSRF_PROBE_PATH} без X-Portal-Csrf: ожидался 403 csrf_check_failed, "
            f"получено {_describe_portal(response)}"
        )
    return _describe_portal(response)


ComposeRunner = Callable[[str, Sequence[str]], str]


def run_compose(compose_file: str, args: Sequence[str]) -> str:
    """Выполнить ``docker compose -f ФАЙЛ ...`` и вернуть stdout; ошибка — SmokeTestError."""
    command = ["docker", "compose", "-f", compose_file, *args]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=COMPOSE_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeTestError(f"не удалось выполнить {' '.join(command)}: {exc}") from exc
    if result.returncode != 0:
        raise SmokeTestError(f"{' '.join(command)}: {result.stderr.strip()[:300]}")
    return result.stdout


def parse_compose_ps(output: str) -> dict[str, tuple[str, str]]:
    """Разобрать ``docker compose ps --format json``: сервис → (состояние, здоровье).

    Compose до 2.21 печатает один JSON-массив, новее — по объекту на строку.
    """
    text = output.strip()
    try:
        rows = (
            json.loads(text)
            if text.startswith("[")
            else [json.loads(line) for line in text.splitlines()]
        )
    except json.JSONDecodeError as exc:
        raise SmokeTestError(f"неожиданный вывод docker compose ps: {text[:200]}") from exc
    return {row["Service"]: (row["State"], row["Health"]) for row in rows} if text else {}


def unhealthy_services(expected: Sequence[str], ps_output: str) -> list[str]:
    """Вернуть описания сервисов из ``expected``, которые не запущены или не здоровы."""
    states = parse_compose_ps(ps_output)
    problems = []
    for service in expected:
        state, health = states.get(service, ("не запущен", ""))
        if (state, health) != ("running", "healthy"):
            problems.append(f"{service}: {state} {health}".rstrip())
    return problems


def check_services(compose_file: str, runner: ComposeRunner = run_compose) -> str:
    """Все сервисы включённых профилей запущены и здоровы; ``runner`` подменяется в тестах."""
    expected = sorted(runner(compose_file, ["config", "--services"]).split())
    problems = unhealthy_services(expected, runner(compose_file, ["ps", "--format", "json"]))
    if problems:
        raise SmokeTestError(
            "не все сервисы здоровы (docker compose ps, logs <сервис>): " + "; ".join(problems)
        )
    return "healthy: " + ", ".join(expected)


def _is_tls_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def failure_message(exc: Exception) -> str:
    """Текст ошибки проверки; для ошибок TLS — с подсказкой про LLM_CA_CERT."""
    return f"{exc} ({TLS_HINT})" if _is_tls_error(exc) else str(exc)


def run_checks(checks: Sequence[tuple[str, Callable[[], str]]]) -> bool:
    """Выполнить проверки по очереди, вывести PASS/FAIL по каждой; True — все прошли."""
    ok = True
    for name, check in checks:
        logger.info("%s: выполняется", name)
        try:
            detail = check()
        except (SmokeTestError, httpx.HTTPError) as exc:
            ok = False
            print(f"FAIL  {name}: {failure_message(exc)}")
        else:
            print(f"PASS  {name}: {detail[:200]}")
    return ok


Check = tuple[str, Callable[[], str]]


def build_site_checks(client: httpx.Client, root: str, site_mode: str) -> list[Check]:
    """Проверки того, что сайт отдаёт помимо путей API: всё закрыто (``api``) или портал."""
    v1_closed: Check = ("v1_closed_routes", lambda: check_v1_closed_routes(client, root))
    if site_mode != "portal":
        return [v1_closed, ("closed_paths", lambda: check_closed_paths(client, root))]
    return [
        v1_closed,
        ("portal_page", lambda: check_portal_page(client, root)),
        ("portal_closed_paths", lambda: check_portal_closed_paths(client, root)),
        ("portal_session_required", lambda: check_portal_session_required(client, root)),
        ("portal_csrf_required", lambda: check_portal_csrf_required(client, root)),
    ]


def build_checks(
    client: httpx.Client,
    root: str,
    *,
    direct: bool,
    site_mode: str = "api",
    site_only: bool = False,
    compose_file: str | None = None,
) -> list[Check]:
    """Собрать список проверок.

    ``direct`` — без проверок сайта (их обеспечивает Caddy); ``site_only`` — без запросов к
    модели; ``compose_file`` — добавить проверку здоровья сервисов.
    """
    checks: list[Check] = []
    if not site_only:
        checks += [
            ("text", lambda: check_text(client)),
            ("thinking_disabled", lambda: check_thinking_disabled(client)),
            ("image", lambda: check_image(client)),
            ("json_schema", lambda: check_json_schema(client)),
            ("tool_call", lambda: check_tool_call(client)),
        ]
    checks.append(("no_key", lambda: check_key_rejected(client, None)))
    if not site_only:
        checks.append(("invalid_key", lambda: check_key_rejected(client, INVALID_API_KEY)))
    if not direct:
        checks += build_site_checks(client, root, site_mode)
    if compose_file is not None:
        checks.append(("services", lambda: check_services(compose_file)))
    return checks


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="проверять vLLM напрямую (первый этап развёртывания, без Caddy и Bifrost): "
        f"эндпоинт по умолчанию {DIRECT_BASE_URL}, ключ — VLLM_API_KEY",
    )
    parser.add_argument(
        "--check-rate-limit",
        type=int,
        metavar="N",
        help="выполнить только проверку лимита: до N запросов, ожидается HTTP 429",
    )
    parser.add_argument(
        "--site-only",
        action="store_true",
        help="только обвязка, без запросов к модели: запрос без ключа и то, что отдаёт сайт "
        "(по SITE_MODE); LLM_API_KEY не нужен",
    )
    parser.add_argument(
        "--compose-file",
        metavar="ФАЙЛ",
        help="проверить ещё, что все сервисы включённых профилей этого compose-файла здоровы",
    )
    args = parser.parse_args(argv)
    if args.check_rate_limit is not None and args.check_rate_limit < 1:
        parser.error("--check-rate-limit должен быть >= 1")
    if args.site_only and (args.direct or args.check_rate_limit is not None):
        parser.error("--site-only не сочетается с --direct и --check-rate-limit")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        settings = load_settings(os.environ, direct=args.direct, site_only=args.site_only)
    except SmokeTestError as exc:
        logger.error("конфигурация: %s", exc)
        return 1

    mode = "vLLM напрямую" if args.direct else "через Caddy и Bifrost"
    logger.info(
        "эндпоинт: %s (%s), модель: %s, сайт: %s",
        settings.base_url,
        mode,
        MODEL,
        settings.site_mode,
    )
    try:
        client = build_client(settings)
    except ssl.SSLError as exc:
        logger.error("LLM_CA_CERT не читается как PEM-сертификат: %s", exc)
        return 1
    root = site_root(settings.base_url)
    with client:
        checks: list[Check]
        if args.check_rate_limit is not None:
            attempts: int = args.check_rate_limit
            checks = [("rate-limit", lambda: check_rate_limit(client, attempts))]
        else:
            checks = build_checks(
                client,
                root,
                direct=args.direct,
                site_mode=settings.site_mode,
                site_only=args.site_only,
                compose_file=args.compose_file,
            )
        ok = run_checks(checks)
    print("ИТОГ: PASS" if ok else "ИТОГ: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
