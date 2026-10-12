"""Доступ к проекту compose стенда из проверок."""

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
# Имя проекта берётся из файла стенда, как в scripts/dev_stand.sh: COMPOSE_PROJECT_NAME из
# окружения иначе увёл бы проверки, которые роняют сервер песочницы, в рабочий стек.
_name = re.search(r"^name: *(\S+)", COMPOSE_FILE.read_text(encoding="utf-8"), re.MULTILINE)
assert _name is not None, "в compose стенда нет строки name:"
PROJECT = _name.group(1)


def docker(
    *args: str, stdin: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    """Запустить docker; код возврата проверяет вызывающий."""
    return subprocess.run(
        ["docker", *args], input=stdin, capture_output=True, text=True, timeout=timeout, check=False
    )


def compose(
    *args: str, project: str = PROJECT, stdin: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    """Запустить docker compose в проекте стенда либо в отдельном проекте на том же файле."""
    return docker(
        "compose", "-p", project, "-f", str(COMPOSE_FILE), *args, stdin=stdin, timeout=timeout
    )


def ok(result: subprocess.CompletedProcess[str]) -> str:
    """Вывод успешной команды."""
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def container_id(service: str) -> str:
    """Контейнер сервиса стенда."""
    found = ok(compose("ps", "-q", service))
    assert found, f"сервис {service} не запущен: сначала make dev-up"
    return found


def container_log(container: str) -> str:
    """Журнал контейнера целиком: оба потока, включая записи до перезапусков."""
    result = docker("logs", container)
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


def wait_until(condition: Callable[[], bool], what: str, timeout: float = 90) -> None:
    """Дождаться условия; срок истёк — ошибка проверки."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"не дождались: {what}"
        time.sleep(1)
