"""Логика install.sh, не требующая ВМ: .env, профили этапов, сборка и проверки портала.

Скрипт копируется во временное дерево (``scripts/`` + ``deploy/``) и подключается через
``source``; ``docker`` и ``curl`` подменены заглушками в ``PATH``.
"""

import base64
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATEWAY_ENV = {"LLM_HOSTNAME": "192.168.25.8", "TLS_MODE": "internal"}
PORTAL_READY_ENV = {
    **GATEWAY_ENV,
    "COMPOSE_PROFILES": "gateway,embeddings",
    "PORTAL_LLM_API_KEY": "sk-bf-portal",
}
# Записывает вызов; для `compose cp` создаёт файл назначения (корневой сертификат Caddy).
DOCKER_STUB = """#!/usr/bin/env bash
echo "$*" >>"$STUB_DIR/docker.log"
if [[ " $* " == *" cp "* ]]; then touch "${!#}"; fi
"""
# Отдаёт код ответа по окончанию адреса (последний аргумент), как curl -w '%{http_code}'.
CURL_STUB = """#!/usr/bin/env bash
url="${!#}"
while read -r suffix code; do
  if [[ "$url" == *"$suffix" ]]; then printf '%s' "$code"; exit 0; fi
done <"$STUB_DIR/codes"
printf '000'
"""
PORTAL_CODES = {
    "/v1/chat/completions": 401,
    "/v1/unknown-route": 404,
    "/v1/mcp/tools": 404,
    "/v1/skills": 404,
    "/v1": 404,
    "/v1/": 404,
    "/v1/async/chat/completions": 404,
    "/v1/mcp/tool/execute": 404,
    "/api/auth/session": 401,
    "/api/governance/virtual-keys": 404,
    "/metrics": 404,
    "/": 200,
}


class Sandbox:
    """Временная копия скрипта и ``deploy/.env.example`` с заглушками внешних команд."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env_file = root / "deploy" / ".env"
        self.stubs = root / "bin"
        (root / "scripts").mkdir()
        (root / "deploy").mkdir()
        self.stubs.mkdir()
        shutil.copy(REPO / "scripts" / "install.sh", root / "scripts")
        shutil.copy(REPO / "deploy" / ".env.example", root / "deploy")
        for name, body in (("docker", DOCKER_STUB), ("curl", CURL_STUB)):
            stub = self.stubs / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.set_codes(PORTAL_CODES)

    def set_codes(self, codes: dict[str, int]) -> None:
        lines = [f"{suffix} {code}\n" for suffix, code in codes.items()]
        (self.stubs / "codes").write_text("".join(lines))

    def write_env(self, values: dict[str, str]) -> None:
        """Создать ``.env`` из примера с заданными значениями (как после прошлых этапов)."""
        lines = (self.root / "deploy" / ".env.example").read_text().splitlines()
        for name, value in values.items():
            lines = [f"{name}={value}" if line.startswith(f"{name}=") else line for line in lines]
        self.env_file.write_text("\n".join(lines) + "\n")

    def env(self) -> dict[str, str]:
        pairs = (line.split("=", 1) for line in self.env_file.read_text().splitlines())
        return {pair[0]: pair[1] for pair in pairs if len(pair) == 2 and pair[0].isidentifier()}

    def docker_calls(self) -> list[str]:
        log = self.stubs / "docker.log"
        return log.read_text().splitlines() if log.exists() else []

    def run(self, body: str) -> subprocess.CompletedProcess[str]:
        """Выполнить функции скрипта после ``source``."""
        script = f'source "{self.root}/scripts/install.sh"\n{body}\n'
        path = f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin"
        bash = shutil.which("bash") or "/bin/bash"
        return subprocess.run(
            [bash, "-c", script],
            capture_output=True,
            text=True,
            env={"PATH": path, "STUB_DIR": str(self.stubs)},
            check=False,
        )

    def prepare_env(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run(f"parse_args {' '.join(args)}\nprepare_env")


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def assert_refused(result: subprocess.CompletedProcess[str], *fragments: str) -> None:
    assert result.returncode == 1
    assert "ОШИБКА" in result.stderr
    for fragment in fragments:
        assert fragment in result.stderr


# --- секреты портала на любом этапе -------------------------------------------


def test_model_stage_generates_portal_secrets(sandbox: Sandbox) -> None:
    result = sandbox.prepare_env("--stage", "model")
    assert result.returncode == 0, result.stderr
    env = sandbox.env()
    for name in ("PORTAL_DB_PASSWORD", "QDRANT_API_KEY"):
        assert len(env[name]) == 64
        int(env[name], 16)
    assert len(base64.b64decode(env["PORTAL_SECRET_KEY"], validate=True)) == 32
    assert env["PORTAL_LLM_API_KEY"] == ""
    assert env["COMPOSE_PROFILES"] == ""
    assert env["SITE_MODE"] == ""
    assert sandbox.env_file.stat().st_mode & 0o777 == 0o600


def test_env_from_before_portal_gets_missing_variables(sandbox: Sandbox) -> None:
    """После git pull на развёрнутой ВМ: в .env нет даже строк с переменными портала."""
    sandbox.write_env({**GATEWAY_ENV, "COMPOSE_PROFILES": "gateway", "VLLM_API_KEY": "old"})
    old_lines = [
        line
        for line in sandbox.env_file.read_text().splitlines()
        if not line.startswith(("PORTAL_", "QDRANT_", "SITE_MODE"))
    ]
    sandbox.env_file.write_text("\n".join(old_lines) + "\n")

    assert sandbox.prepare_env().returncode == 0
    env = sandbox.env()
    assert env["PORTAL_DB_PASSWORD"] and env["QDRANT_API_KEY"] and env["PORTAL_SECRET_KEY"]
    assert env["VLLM_API_KEY"] == "old"
    assert env["COMPOSE_PROFILES"] == "gateway"


@pytest.mark.parametrize("stage", ["model", "gateway", "portal"])
def test_existing_portal_secrets_are_never_overwritten(sandbox: Sandbox, stage: str) -> None:
    secrets = {
        "PORTAL_SECRET_KEY": "c2VjcmV0LWtleS1rZXB0LWFzLWlzLTMyLWJ5dGVzISE=",
        "PORTAL_DB_PASSWORD": "db-password",
        "QDRANT_API_KEY": "qdrant-key",
    }
    sandbox.write_env({**PORTAL_READY_ENV, **secrets})
    for _ in range(2):
        assert sandbox.prepare_env("--stage", stage).returncode == 0
    env = sandbox.env()
    assert {name: env[name] for name in secrets} == secrets


# --- этап portal: отказы -------------------------------------------------------


def test_unknown_stage_is_rejected(sandbox: Sandbox) -> None:
    assert_refused(sandbox.run("parse_args --stage web"), "model, gateway или portal")


def test_portal_refuses_without_gateway(sandbox: Sandbox) -> None:
    sandbox.write_env({"COMPOSE_PROFILES": "embeddings", "PORTAL_LLM_API_KEY": "sk-bf-portal"})
    assert_refused(sandbox.prepare_env("--stage", "portal"), "gateway", "make gateway")
    assert sandbox.env()["COMPOSE_PROFILES"] == "embeddings"
    assert sandbox.env()["SITE_MODE"] == ""


@pytest.mark.parametrize("profiles", ["gateway", "gateway,monitoring"])
def test_portal_refuses_without_embeddings(sandbox: Sandbox, profiles: str) -> None:
    sandbox.write_env({**PORTAL_READY_ENV, "COMPOSE_PROFILES": profiles})
    assert_refused(sandbox.prepare_env("--stage", "portal"), "embeddings", "make portal PROFILES=")
    assert sandbox.env()["COMPOSE_PROFILES"] == profiles


def test_portal_refuses_without_llm_key_and_tells_how_to_get_it(sandbox: Sandbox) -> None:
    sandbox.write_env({**PORTAL_READY_ENV, "PORTAL_LLM_API_KEY": ""})
    result = sandbox.prepare_env("--stage", "portal")
    assert_refused(result, "PORTAL_LLM_API_KEY", "make key NAME=portal")
    env = sandbox.env()
    assert env["COMPOSE_PROFILES"] == "gateway,embeddings"
    assert env["SITE_MODE"] == ""


def test_enabled_portal_refuses_to_lose_embeddings(sandbox: Sandbox) -> None:
    sandbox.write_env({**PORTAL_READY_ENV, "COMPOSE_PROFILES": "gateway,embeddings,portal"})
    result = sandbox.prepare_env("--stage", "gateway", "--profiles", "monitoring")
    assert_refused(result, "embeddings")


# --- этап portal: включение ----------------------------------------------------


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("gateway,embeddings", "gateway,embeddings,portal"),
        ("gateway,embeddings,monitoring", "gateway,embeddings,monitoring,portal"),
        ("gateway,embeddings,portal", "gateway,embeddings,portal"),
    ],
)
def test_portal_stage_adds_profile_and_site_mode(
    sandbox: Sandbox, current: str, expected: str
) -> None:
    sandbox.write_env({**PORTAL_READY_ENV, "COMPOSE_PROFILES": current})
    result = sandbox.prepare_env("--stage", "portal")
    assert result.returncode == 0, result.stderr
    env = sandbox.env()
    assert env["COMPOSE_PROFILES"] == expected
    assert env["SITE_MODE"] == "portal"
    assert env["LLM_HOSTNAME"] == "192.168.25.8"
    assert env["PORTAL_LLM_API_KEY"] == "sk-bf-portal"


def test_portal_stage_accepts_embeddings_from_argument(sandbox: Sandbox) -> None:
    sandbox.write_env({**PORTAL_READY_ENV, "COMPOSE_PROFILES": "gateway"})
    result = sandbox.prepare_env("--stage", "portal", "--profiles", "embeddings")
    assert result.returncode == 0, result.stderr
    assert sandbox.env()["COMPOSE_PROFILES"] == "gateway,embeddings,portal"


def test_portal_survives_rerun_of_gateway_stage_with_new_hostname(sandbox: Sandbox) -> None:
    sandbox.write_env({**PORTAL_READY_ENV, "COMPOSE_PROFILES": "gateway,embeddings,portal"})
    result = sandbox.prepare_env("--stage", "gateway", "--hostname", "llm.example.ru")
    assert result.returncode == 0, result.stderr
    env = sandbox.env()
    assert env["COMPOSE_PROFILES"] == "gateway,embeddings,portal"
    assert env["SITE_MODE"] == "portal"
    assert env["LLM_HOSTNAME"] == "llm.example.ru"


def test_gateway_stage_leaves_site_mode_empty(sandbox: Sandbox) -> None:
    sandbox.write_env(GATEWAY_ENV)
    assert sandbox.prepare_env("--stage", "gateway", "--profiles", "embeddings").returncode == 0
    env = sandbox.env()
    assert env["COMPOSE_PROFILES"] == "gateway,embeddings"
    assert env["SITE_MODE"] == ""


# --- сборка образов ------------------------------------------------------------


def test_portal_images_are_built_together(sandbox: Sandbox) -> None:
    result = sandbox.run("portal_enabled=1\nbuild_portal_images")
    assert result.returncode == 0, result.stderr
    compose_file = f"{sandbox.root}/deploy/docker-compose.yml"
    assert sandbox.docker_calls() == [
        f"compose -f {compose_file} build portal-api portal-worker portal-web"
    ]


def test_no_build_without_portal(sandbox: Sandbox) -> None:
    assert sandbox.run("portal_enabled=0\nbuild_portal_images").returncode == 0
    assert sandbox.docker_calls() == []


def test_full_portal_run_builds_before_start(sandbox: Sandbox) -> None:
    """Весь main с подменой: проверка системы и systemd-часть запуска требуют ВМ и root."""
    sandbox.write_env(PORTAL_READY_ENV)
    body = """
check_system() { :; }
start_stack() { compose up -d --wait; }
main --stage portal --skip-preflight
"""
    result = sandbox.run(body)
    assert result.returncode == 0, result.stderr
    prefix = f"compose -f {sandbox.root}/deploy/docker-compose.yml "
    calls = [call.removeprefix(prefix) for call in sandbox.docker_calls()]
    assert calls[:3] == ["config -q", "pull", "build portal-api portal-worker portal-web"]
    assert calls[-2] == "up -d --wait"
    assert calls[-1].startswith("cp caddy:")
    assert sum(call.startswith("run --rm --no-deps") for call in calls) == 2
    assert sandbox.env()["COMPOSE_PROFILES"] == "gateway,embeddings,portal"
    assert "Этап 3 завершён" in result.stdout


def test_gateway_stage_recreates_bifrost_to_apply_config(sandbox: Sandbox) -> None:
    assert sandbox.run("gateway_enabled=1\napply_bifrost_config").returncode == 0
    assert sandbox.docker_calls()[-1].endswith("up -d --force-recreate --no-deps --wait bifrost")


def test_model_stage_has_no_bifrost_to_recreate(sandbox: Sandbox) -> None:
    assert sandbox.run("gateway_enabled=0\napply_bifrost_config").returncode == 0
    assert sandbox.docker_calls() == []


# --- проверка через 443 --------------------------------------------------------

VERIFY = "hostname=192.168.25.8\ntls_mode=internal\nportal_enabled={portal}\nverify_gateway"


def test_verify_portal_passes(sandbox: Sandbox) -> None:
    result = sandbox.run(VERIFY.format(portal=1))
    assert result.returncode == 0, result.stderr
    for label in ("страница входа", "без сессии", "админ-API Bifrost", "/metrics", "без ключа"):
        assert label in result.stdout


@pytest.mark.parametrize(
    ("suffix", "code", "fragment"),
    [
        ("/", 404, "страница входа портала открывается: HTTP 404"),
        ("/api/auth/session", 200, "портал без сессии отвечает 401: HTTP 200"),
        ("/api/governance/virtual-keys", 200, "админ-API Bifrost через 443 закрыт: HTTP 200"),
        ("/metrics", 200, "/metrics через 443 закрыт: HTTP 200"),
        ("/v1/chat/completions", 200, "без ключа отклоняется: HTTP 200"),
    ],
)
def test_verify_portal_failures(sandbox: Sandbox, suffix: str, code: int, fragment: str) -> None:
    sandbox.set_codes({**PORTAL_CODES, suffix: code})
    assert_refused(sandbox.run(VERIFY.format(portal=1)), fragment)


@pytest.mark.parametrize("portal", [0, 1])
@pytest.mark.parametrize(
    ("suffix", "code", "fragment"),
    [
        ("/v1/unknown-route", 200, "GET /v1/unknown-route через 443 закрыт: HTTP 200"),
        ("/v1/", 200, "GET /v1/ через 443 закрыт: HTTP 200"),
        ("/v1/mcp/tool/execute", 401, "POST /v1/mcp/tool/execute через 443 закрыт: HTTP 401"),
    ],
)
def test_verify_gateway_fails_when_other_v1_route_reaches_bifrost(
    sandbox: Sandbox, portal: int, suffix: str, code: int, fragment: str
) -> None:
    root_code = 200 if portal else 404
    sandbox.set_codes({**PORTAL_CODES, "/": root_code, suffix: code})
    assert_refused(sandbox.run(VERIFY.format(portal=portal)), fragment)


def test_verify_gateway_without_portal_still_expects_closed_root(sandbox: Sandbox) -> None:
    assert_refused(sandbox.run(VERIFY.format(portal=0)), "/ через 443 закрыт: HTTP 200")
    sandbox.set_codes({**PORTAL_CODES, "/": 404})
    assert sandbox.run(VERIFY.format(portal=0)).returncode == 0


def test_verify_gateway_checks_the_same_site_on_extra_ports(sandbox: Sandbox) -> None:
    result = sandbox.run(VERIFY.format(portal=1))
    assert result.returncode == 0, result.stderr
    assert "порт 8080: запрос к модели без ключа отклоняется" in result.stdout
    assert "порт 18443: запрос к модели без ключа отклоняется" in result.stdout


def test_verify_gateway_fails_when_extra_port_is_not_caddy(sandbox: Sandbox) -> None:
    """На 8080 отвечает не Caddy (например, прежний админ-порт Bifrost)."""
    sandbox.set_codes({":8080/v1/chat/completions": 404, **PORTAL_CODES})
    assert_refused(sandbox.run(VERIFY.format(portal=1)), "порт 8080", "HTTP 404")


# --- сертификат и таймер продления (TLS_MODE=corp) -----------------------------

CORP = "hostname=llm.example.ru\ntls_mode=corp\ngateway_enabled=1\n"
RENEWAL = (
    CORP
    + 'install_unit() { echo "unit $(basename "$1")"; }\n'
    + 'systemctl() { echo "systemctl $*"; }\n'
    + "install_cert_renewal"
)


def test_missing_certificate_points_to_make_cert_when_acme_is_configured(sandbox: Sandbox) -> None:
    sandbox.write_env({"ACME_EMAIL": "admin@example.ru"})
    assert_refused(sandbox.run(CORP + "prepare_tls"), "нет файла", "make cert")


def test_missing_certificate_without_acme_points_to_corporate_ca(sandbox: Sandbox) -> None:
    sandbox.write_env({})
    result = sandbox.run(CORP + "prepare_tls")
    assert_refused(result, "нет файла", "корпоративного CA")
    assert "make cert" not in result.stderr


def issued_by_lego(sandbox: Sandbox) -> None:
    certificates = sandbox.root / "deploy" / "lego" / "certificates"
    certificates.mkdir(parents=True)
    (certificates / "llm.example.ru.crt").touch()


def test_renewal_timer_is_enabled_for_certificate_issued_by_lego(sandbox: Sandbox) -> None:
    sandbox.write_env({"TIMEWEBCLOUD_AUTH_TOKEN": "token"})
    issued_by_lego(sandbox)
    result = sandbox.run(RENEWAL)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[1:] == [
        "unit llm-cert-renew.service",
        "unit llm-cert-renew.timer",
        "systemctl daemon-reload",
        "systemctl enable --now llm-cert-renew.timer",
    ]


def test_renewal_timer_is_not_enabled_without_dns_token(sandbox: Sandbox) -> None:
    """Сертификат выпущен с ручным подтверждением: таймер продлить его не сможет."""
    sandbox.write_env({})
    issued_by_lego(sandbox)
    result = sandbox.run(RENEWAL)
    assert result.returncode == 0, result.stderr
    assert "systemctl" not in result.stdout
    assert "make cert-renew MANUAL=1" in result.stdout


@pytest.mark.parametrize("tls_mode", ["corp", "internal"])
def test_renewal_timer_is_not_touched_without_lego_certificate(
    sandbox: Sandbox, tls_mode: str
) -> None:
    """Сертификат корпоративного CA положен вручную или TLS — собственный CA Caddy."""
    sandbox.write_env({"TIMEWEBCLOUD_AUTH_TOKEN": "token"})
    if tls_mode == "internal":
        issued_by_lego(sandbox)
    result = sandbox.run(RENEWAL.replace("tls_mode=corp", f"tls_mode={tls_mode}"))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# --- итог ----------------------------------------------------------------------


def test_portal_summary_reminds_about_secret_key_and_first_admin(sandbox: Sandbox) -> None:
    body = "hostname=192.168.25.8\ntls_mode=internal\ngateway_enabled=1\nportal_enabled=1\n"
    result = sandbox.run(body + "print_summary")
    assert result.returncode == 0, result.stderr
    assert "PORTAL_SECRET_KEY" in result.stdout
    assert "вне ВМ" in result.stdout
    assert "make portal-admin LOGIN=" in result.stdout
    assert f"make smoke-portal CA={sandbox.root}/deploy/caddy-root.crt" in result.stdout


def test_gateway_summary_gives_new_bifrost_admin_port(sandbox: Sandbox) -> None:
    body = "hostname=llm.example.ru\ntls_mode=corp\ngateway_enabled=1\nportal_enabled=0\n"
    result = sandbox.run(body + "print_summary")
    assert result.returncode == 0, result.stderr
    assert "ssh -L 8081:127.0.0.1:8081" in result.stdout
    assert "https://llm.example.ru:18443/v1" in result.stdout
