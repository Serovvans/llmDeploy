"""Сервис code-sandbox на поднятом стенде: сеть, секреты, журнал (CLAUDE.md, «Код», правило 2).

Требования — docs/portal-api.md §7.5.12 и §13.5: песочница без сети и без секретов
портала, в её журнале только числа — ни путей, ни кода, ни сообщений компилятора.
Поведение клиента портала (очередь, сроки, разбор ответа) проверяют тесты бэкенда.

Запуск: `uv run pytest stand` при поднятом стенде (make dev-up).
"""

import json

import pytest

from stand.support import PROJECT, compose, container_id, container_log, docker, ok

# Секреты `.env` стенда: ни имён, ни значений в окружении песочницы быть не должно.
SECRETS = (
    "PORTAL_DB_PASSWORD",
    "PORTAL_SECRET_KEY",
    "PORTAL_LLM_API_KEY",
    "QDRANT_API_KEY",
    "SQL_SANDBOX_PASSWORD",
    "SQL_SANDBOX_CONTROL_PASSWORD",
)
MARKER = "MarkerCodeSandbox7f3a"
# Сборка от имени portal-api: путь, код и сообщение компилятора содержат маркер.
COMPILE = f"""
import json, urllib.request
body = {{
    "assembly_name": "MarkerProject",
    "files": [{{"path": "{MARKER}/{MARKER}.cs",
               "content": "namespace MarkerProject {{ class A {{ int B() => {MARKER}; }} }}"}}],
    "packages": [],
}}
request = urllib.request.Request(
    "http://code-sandbox:8080/compile", data=json.dumps(body).encode(),
    headers={{"Content-Type": "application/json"}},
)
with urllib.request.urlopen(request, timeout=120) as response:
    print(response.read().decode())
"""


def sandbox_sh(script: str) -> int:
    return compose("exec", "-T", "code-sandbox", "bash", "-c", script).returncode


@pytest.fixture(scope="module")
def sandbox() -> str:
    return container_id("code-sandbox")


def test_only_build_network_and_no_host_ports(sandbox: str) -> None:
    settings = json.loads(ok(docker("inspect", "-f", "{{json .NetworkSettings}}", sandbox)))
    assert list(settings["Networks"]) == [f"{PROJECT}_code-sandbox"]
    assert not any(settings["Ports"].values())


def test_no_outbound_connections(sandbox: str) -> None:
    def address(service: str) -> str:
        template = "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"
        return ok(docker("inspect", "-f", template, container_id(service))).split()[0]

    def connects(host: str, port: int) -> bool:
        return sandbox_sh(f"timeout 5 bash -c 'exec 3<>/dev/tcp/{host}/{port}'") == 0

    # Сама проба работает, а portal-api до песочницы достаёт — иначе отказы ниже ничего
    # не значили бы.
    assert connects("127.0.0.1", 8080)
    reach = "import socket; socket.create_connection(('code-sandbox', 8080), 5)"
    assert compose("exec", "-T", "portal-api", "python", "-c", reach).returncode == 0

    for name in ("portal-db", "bifrost", "qdrant", "api.nuget.org", "example.com"):
        assert sandbox_sh(f"getent hosts {name}") != 0, name
    # По адресу, а не только по имени: имя чужой сети не разрешается и при открытом пути.
    assert not connects(address("portal-db"), 5432)
    assert not connects(address("bifrost"), 8080)
    assert not connects("1.1.1.1", 443)


def test_environment_has_no_portal_secrets(sandbox: str) -> None:
    environment = json.loads(ok(docker("inspect", "-f", "{{json .Config.Env}}", sandbox)))
    names = {item.partition("=")[0] for item in environment}
    assert not names & set(SECRETS)
    api_environment = json.loads(
        ok(docker("inspect", "-f", "{{json .Config.Env}}", container_id("portal-api")))
    )
    values = [
        value
        for name, _, value in (item.partition("=") for item in api_environment)
        if name in SECRETS and value
    ]
    assert values, "у portal-api нет секретов — проверка ничего бы не значила"
    joined = "\n".join(environment)
    assert not any(value in joined for value in values)


def test_log_has_no_paths_code_or_compiler_messages(sandbox: str) -> None:
    answer = json.loads(ok(compose("exec", "-T", "portal-api", "python", "-c", COMPILE)))
    # Сборка прошла до компилятора: маркер есть в его сообщении, значит, был и в работе.
    assert answer["status"] == "failed"
    assert any(MARKER in item["message"] for item in answer["diagnostics"])
    assert MARKER not in container_log(sandbox)
