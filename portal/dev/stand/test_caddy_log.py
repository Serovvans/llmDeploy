"""Журнал ошибок Caddy не содержит строку запроса (docs/portal-design.md §8).

Caddy запускается с рабочим Caddyfile в отдельном проекте compose без остальных сервисов:
бэкенда там нет, и запрос к /api/* заканчивается ошибкой проксирования — ровно тот случай,
когда адрес запроса попадает в журнал. Стенд при этом не затрагивается.
"""

import ssl
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from stand.support import PROJECT, compose, container_log, docker, ok, wait_until

LOG_PROJECT = f"{PROJECT}-caddylog"
QUERY = (
    "file_name=%D0%94%D0%BE%D0%B3%D0%BE%D0%B2%D0%BE%D1%80-marker.mp3&scope=personal&tags=marker-tag"
)


@pytest.fixture
def caddy() -> Iterator[str]:
    try:
        yield ok(
            compose(
                "run",
                "-d",
                "--no-deps",
                "--publish",
                "127.0.0.1::443",
                "caddy",
                project=LOG_PROJECT,
            )
        )
    finally:
        compose("down", "--volumes", "--remove-orphans", project=LOG_PROJECT)


def test_proxy_error_is_logged_without_query_string(caddy: str) -> None:
    address = ok(docker("port", caddy, "443/tcp")).splitlines()[0]
    url = f"https://localhost:{address.rsplit(':', 1)[1]}/api/kb/documents/media?{QUERY}"
    # Сертификат выпускает собственный CA Caddy; проверяется журнал, а не TLS.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    statuses: list[int] = []

    def answered() -> bool:
        try:
            urllib.request.urlopen(url, context=context, timeout=5)
        except urllib.error.HTTPError as error:
            statuses.append(error.code)
            return True
        except OSError:
            return False
        return True

    wait_until(answered, "Caddy отвечает", timeout=60)
    assert statuses == [502]
    wait_until(
        lambda: "/api/kb/documents/media" in container_log(caddy),
        "ошибка записана в журнал",
        timeout=15,
    )
    log = container_log(caddy)
    for leaked in ("file_name", "marker", "scope=", "tags="):
        assert leaked not in log, log
