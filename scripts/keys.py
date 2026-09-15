"""Управление virtual keys Bifrost через REST API, docs/design.md §3.3, §7, §9.

Команды:
    create --name NAME [--description TEXT] [--requests N] [--tokens N] [--period 1h]
        Создать ключ с лимитами запросов и/или токенов за период (по умолчанию час)
        и вывести его значение ``sk-bf-...``.
    list
        Показать ключи: id, имя, активность, расход/лимит запросов и токенов.
    revoke VK_ID
        Отозвать ключ: деактивировать (``is_active: false``). Доступ пропадает сразу,
        ключ остаётся в Bifrost для аудита и может быть включён обратно в UI.

API (сверено с https://docs.getbifrost.ai/features/governance/virtual-keys и
API reference ``/api-reference/governance/*`` на 2026-09-15; перепроверить на ВМ
с закреплённой версией Bifrost):
    POST /api/governance/virtual-keys        -> {"message", "virtual_key": VirtualKey}
    GET  /api/governance/virtual-keys        -> {"virtual_keys", "count", "total_count", ...}
    PUT  /api/governance/virtual-keys/{id}   -> {"message", "virtual_key": VirtualKey}
Ключ создаётся с ``allow_all_providers: true``: провайдер в конфигурации один (vLLM),
и ключ не привязывается к его имени.

Окружение:
    BIFROST_ADMIN_URL       адрес API управления; по умолчанию http://127.0.0.1:8080
                            (с рабочей станции — через SSH-туннель
                            ``ssh -L 8080:127.0.0.1:8080 <вм>``).
    BIFROST_ADMIN_USERNAME  логин admin-auth Bifrost (``governance.auth_config``). Нужен:
                            admin-auth включена (§3.3), без него API отвечает 401.
    BIFROST_ADMIN_PASSWORD  пароль admin-auth Bifrost; значения — в deploy/.env на ВМ.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from api_errors import describe_error

logger = logging.getLogger("keys")

DEFAULT_ADMIN_URL = "http://127.0.0.1:8080"
VIRTUAL_KEYS_PATH = "/api/governance/virtual-keys"
REQUEST_TIMEOUT_S = 30.0
LIST_PAGE_SIZE = 100

JsonObject = dict[str, Any]


class BifrostApiError(Exception):
    """Ошибка обращения к API управления Bifrost."""


@dataclass(frozen=True)
class Settings:
    """Параметры подключения к API управления Bifrost."""

    admin_url: str
    auth: tuple[str, str] | None


def load_settings(env: Mapping[str, str]) -> Settings:
    """Прочитать настройки из переменных окружения."""
    admin_url = env.get("BIFROST_ADMIN_URL", "").strip() or DEFAULT_ADMIN_URL
    username = env.get("BIFROST_ADMIN_USERNAME", "")
    password = env.get("BIFROST_ADMIN_PASSWORD", "")
    if bool(username) != bool(password):
        raise BifrostApiError("BIFROST_ADMIN_USERNAME и BIFROST_ADMIN_PASSWORD задаются вместе")
    auth = (username, password) if username else None
    return Settings(admin_url=admin_url.rstrip("/"), auth=auth)


def build_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Создать HTTP-клиент к API управления.

    ``transport`` позволяет подменить сетевой слой (в тестах — ``httpx.MockTransport``).
    """
    return httpx.Client(
        base_url=settings.admin_url,
        auth=httpx.BasicAuth(*settings.auth) if settings.auth else None,
        timeout=REQUEST_TIMEOUT_S,
        transport=transport,
    )


def build_create_payload(
    name: str,
    description: str | None,
    requests: int | None,
    tokens: int | None,
    period: str,
) -> JsonObject:
    """Собрать тело POST /api/governance/virtual-keys."""
    payload: JsonObject = {"name": name, "is_active": True, "allow_all_providers": True}
    if description:
        payload["description"] = description
    rate_limit: JsonObject = {}
    if requests is not None:
        rate_limit |= {"request_max_limit": requests, "request_reset_duration": period}
    if tokens is not None:
        rate_limit |= {"token_max_limit": tokens, "token_reset_duration": period}
    if rate_limit:
        payload["rate_limit"] = rate_limit
    return payload


def _request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> JsonObject:
    response = client.request(method, path, **kwargs)
    if not response.is_success:
        raise BifrostApiError(
            f"{method} {path}: {describe_error(response)}{_auth_hint(client, response)}"
        )
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = None
    if not isinstance(body, dict):
        raise BifrostApiError(
            f"{method} {path}: HTTP {response.status_code}, ответ не JSON-объект (неверный "
            f"BIFROST_ADMIN_URL или путь API в этой версии Bifrost?): {response.text[:200]}"
        )
    return body


def _auth_hint(client: httpx.Client, response: httpx.Response) -> str:
    if response.status_code not in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return ""
    if client.auth is None:
        return (
            " (admin-auth Bifrost включена: задайте BIFROST_ADMIN_USERNAME и "
            "BIFROST_ADMIN_PASSWORD из deploy/.env)"
        )
    return " (проверьте BIFROST_ADMIN_USERNAME и BIFROST_ADMIN_PASSWORD — см. deploy/.env)"


def _virtual_key(body: JsonObject) -> JsonObject:
    virtual_key = body.get("virtual_key")
    if not isinstance(virtual_key, dict):
        raise BifrostApiError(f"в ответе нет virtual_key: {body}")
    return virtual_key


def create_key(client: httpx.Client, payload: JsonObject) -> JsonObject:
    """Создать ключ и вернуть объект VirtualKey."""
    return _virtual_key(_request(client, "POST", VIRTUAL_KEYS_PATH, json=payload))


def list_keys(client: httpx.Client) -> list[JsonObject]:
    """Вернуть все ключи, пройдя по страницам ответа."""
    keys: list[JsonObject] = []
    while True:
        params = {"limit": LIST_PAGE_SIZE, "offset": len(keys)}
        body = _request(client, "GET", VIRTUAL_KEYS_PATH, params=params)
        page = body.get("virtual_keys") or []
        keys.extend(page)
        if not page or len(keys) >= body.get("total_count", len(keys)):
            return keys


def revoke_key(client: httpx.Client, vk_id: str) -> JsonObject:
    """Деактивировать ключ и вернуть обновлённый VirtualKey."""
    body = _request(client, "PUT", f"{VIRTUAL_KEYS_PATH}/{vk_id}", json={"is_active": False})
    virtual_key = _virtual_key(body)
    if virtual_key.get("is_active") is not False:
        raise BifrostApiError(f"ключ {vk_id} после обновления всё ещё активен")
    return virtual_key


def _limit_cell(rate_limit: JsonObject | None, kind: str) -> str:
    if not rate_limit or rate_limit.get(f"{kind}_max_limit") is None:
        return "-"
    usage = rate_limit.get(f"{kind}_current_usage", 0)
    return (
        f"{usage}/{rate_limit[f'{kind}_max_limit']} за {rate_limit.get(f'{kind}_reset_duration')}"
    )


def format_keys_table(keys: Sequence[JsonObject]) -> str:
    """Отформатировать список ключей в текстовую таблицу (без значений ключей)."""
    header = ("ID", "NAME", "ACTIVE", "REQUESTS", "TOKENS")
    rows = [header] + [
        (
            str(key.get("id", "")),
            str(key.get("name", "")),
            "yes" if key.get("is_active") else "no",
            _limit_cell(key.get("rate_limit"), "request"),
            _limit_cell(key.get("rate_limit"), "token"),
        )
        for key in keys
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("должно быть целое >= 1")
    return number


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="создать ключ")
    create.add_argument("--name", required=True, help="имя ключа (приложение или сотрудник)")
    create.add_argument("--description", help="описание")
    create.add_argument("--requests", type=_positive_int, help="лимит запросов за период")
    create.add_argument("--tokens", type=_positive_int, help="лимит токенов за период")
    create.add_argument("--period", default="1h", help="период сброса лимитов: 1m, 1h, 1d, ...")

    commands.add_parser("list", help="показать ключи")

    revoke = commands.add_parser("revoke", help="деактивировать ключ")
    revoke.add_argument("vk_id", help="ID ключа (см. list)")
    return parser.parse_args(argv)


def run_command(args: argparse.Namespace, client: httpx.Client) -> None:
    """Выполнить команду CLI и вывести результат в stdout."""
    if args.command == "create":
        payload = build_create_payload(
            args.name, args.description, args.requests, args.tokens, args.period
        )
        if "rate_limit" not in payload:
            logger.warning("ключ %s создаётся без лимитов запросов и токенов", args.name)
        key = create_key(client, payload)
        print(f"id:    {key.get('id')}\nname:  {key.get('name')}\nkey:   {key.get('value')}")
    elif args.command == "list":
        print(format_keys_table(list_keys(client)))
    elif args.command == "revoke":
        key = revoke_key(client, args.vk_id)
        print(f"ключ {key.get('id')} ({key.get('name')}) деактивирован")


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        settings = load_settings(os.environ)
    except BifrostApiError as exc:
        logger.error("конфигурация: %s", exc)
        return 1
    try:
        with build_client(settings) as client:
            run_command(args, client)
    except BifrostApiError as exc:
        logger.error("%s", exc)
        return 1
    except httpx.TransportError as exc:
        logger.error(
            "нет связи с %s: %s. Bifrost запущен? С рабочей станции нужен SSH-туннель: "
            "ssh -L 8080:127.0.0.1:8080 <вм>",
            settings.admin_url,
            exc,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
