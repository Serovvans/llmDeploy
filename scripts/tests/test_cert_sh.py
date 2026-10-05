"""Логика cert.sh с подменённым ``docker``: когда вызывается lego, что и когда заменяется.

Скрипт копируется во временное дерево (``scripts/`` + ``deploy/``). Сертификаты —
самоподписанные, их делает настоящий ``openssl``; настоящий выпуск проверяется на ВМ.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOST = "llm.example.ru"
TOKEN = "timeweb-secret-token"
FULL_ENV = {
    "LLM_HOSTNAME": HOST,
    "ACME_EMAIL": "admin@example.ru",
    "TIMEWEBCLOUD_AUTH_TOKEN": TOKEN,
}
OPENSSL = shutil.which("openssl")
# Записывает вызов. Запуск lego: при файле lego_fail — ошибка; иначе в состояние lego
# кладётся пара из lego_out (как после выпуска или продления). Caddy «запущен», если есть
# файл caddy_running.
DOCKER_STUB = """#!/usr/bin/env bash
call="$*"
echo "$call" >>"$STUB_DIR/docker.log"
case "$call" in
  *" run --rm lego "*)
    if [[ -e "$STUB_DIR/lego_fail" ]]; then echo "lego: acme error" >&2; exit 1; fi
    mkdir -p "$LEGO_CERTS"
    cp "$STUB_DIR/lego_out/cert.pem" "$LEGO_CERTS/$CERT_HOST.crt"
    cp "$STUB_DIR/lego_out/key.pem" "$LEGO_CERTS/$CERT_HOST.key"
    ;;
  *"ps --status running -q caddy") if [[ -e "$STUB_DIR/caddy_running" ]]; then echo abc123; fi ;;
  *"caddy reload"*) if [[ -e "$STUB_DIR/reload_fail" ]]; then exit 1; fi ;;
esac
"""
# Настоящий cp, но при файле cp_fail копирование сертификата (.crt) не удаётся.
CP_STUB = """#!/usr/bin/env bash
if [[ -e "$STUB_DIR/cp_fail" && "$1" == *.crt ]]; then echo "cp: no space left" >&2; exit 1; fi
exec /bin/cp "$@"
"""


def host_check_supported() -> bool:
    """LibreSSL из macOS не знает ``x509 -checkhost``; на ВМ — OpenSSL."""
    if OPENSSL is None:
        return False
    probe = subprocess.run([OPENSSL, "x509", "-help"], capture_output=True, text=True, check=False)
    return "-checkhost" in probe.stdout + probe.stderr


pytestmark = pytest.mark.skipif(
    not host_check_supported(), reason="нужен openssl с x509 -checkhost (OpenSSL 1.1+)"
)


def make_pair(directory: Path, host: str = HOST, days: int = 90) -> Path:
    """Самоподписанный сертификат на ``host`` и его ключ: ``cert.pem`` и ``key.pem``."""
    assert OPENSSL is not None
    directory.mkdir()
    command = [OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1"]
    command += [
        "-nodes",
        "-keyout",
        str(directory / "key.pem"),
        "-out",
        str(directory / "cert.pem"),
    ]
    command += ["-days", str(days), "-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host}"]
    subprocess.run(command, check=True, capture_output=True)
    return directory


@pytest.fixture(scope="module")
def pairs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("pairs")
    return {
        "fresh": make_pair(root / "fresh"),
        "renewed": make_pair(root / "renewed"),
        "expiring": make_pair(root / "expiring", days=10),
        "other_host": make_pair(root / "other_host", host="other.example.ru"),
    }


class Sandbox:
    """Временная копия скрипта с ``deploy/.env`` и заглушкой ``docker``."""

    def __init__(self, root: Path) -> None:
        self.deploy = root / "deploy"
        self.stubs = root / "bin"
        self.certs = self.deploy / "certs"
        self.lego_certs = self.deploy / "lego" / "certificates"
        (root / "scripts").mkdir()
        self.deploy.mkdir()
        self.stubs.mkdir()
        shutil.copy(REPO / "scripts" / "cert.sh", root / "scripts")
        self.script = root / "scripts" / "cert.sh"
        for name, body in (("docker", DOCKER_STUB), ("cp", CP_STUB)):
            stub = self.stubs / name
            stub.write_text(body)
            stub.chmod(0o755)
        assert OPENSSL is not None
        (self.stubs / "openssl").symlink_to(OPENSSL)
        self.write_env(FULL_ENV)
        (self.stubs / "caddy_running").touch()

    def write_env(self, values: dict[str, str]) -> None:
        lines = [f"{name}={value}\n" for name, value in values.items()]
        (self.deploy / ".env").write_text("".join(lines))

    def lego_will_produce(self, pair: Path, key_from: Path | None = None) -> None:
        """Что lego положит в своё состояние; ``key_from`` — ключ от другой пары."""
        out = self.stubs / "lego_out"
        out.mkdir()
        shutil.copy(pair / "cert.pem", out)
        shutil.copy((key_from or pair) / "key.pem", out)

    def lego_has(self, pair: Path) -> None:
        """Состояние lego после прошлого выпуска."""
        self.lego_certs.mkdir(parents=True)
        shutil.copy(pair / "cert.pem", self.lego_certs / f"{HOST}.crt")
        shutil.copy(pair / "key.pem", self.lego_certs / f"{HOST}.key")

    def deployed(self, pair: Path) -> None:
        """Рабочие файлы Caddy."""
        self.certs.mkdir()
        shutil.copy(pair / "cert.pem", self.certs / "fullchain.pem")
        shutil.copy(pair / "key.pem", self.certs / "privkey.pem")

    def assert_deployed(self, pair: Path) -> None:
        assert (self.certs / "fullchain.pem").read_bytes() == (pair / "cert.pem").read_bytes()
        assert (self.certs / "privkey.pem").read_bytes() == (pair / "key.pem").read_bytes()
        assert sorted(path.name for path in self.certs.iterdir()) == [
            "fullchain.pem",
            "privkey.pem",
        ]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash") or "/bin/bash"
        env = {
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "STUB_DIR": str(self.stubs),
            "LEGO_CERTS": str(self.lego_certs),
            "CERT_HOST": HOST,
        }
        return subprocess.run(
            [bash, str(self.script), *args], capture_output=True, text=True, env=env, check=False
        )

    def calls(self) -> list[str]:
        """Вызовы docker без общего префикса compose."""
        log = self.stubs / "docker.log"
        lines = log.read_text().splitlines() if log.exists() else []
        prefix = f"compose -f {self.deploy.resolve()}/docker-compose.yml "
        return [line.removeprefix(prefix) for line in lines]


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def assert_refused(result: subprocess.CompletedProcess[str], *fragments: str) -> None:
    assert result.returncode == 1
    assert "ОШИБКА" in result.stderr
    for fragment in fragments:
        assert fragment in result.stderr


LEGO = (
    f"--profile cert run --rm lego run --domains {HOST} --dns {{dns}} --accept-tos --renew-days 30"
)
PS_CADDY = "ps --status running -q caddy"
RELOAD_CADDY = "exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force"

# --- выпуск ---------------------------------------------------------------------


def test_issue_installs_files_and_reloads_caddy(sandbox: Sandbox, pairs: dict[str, Path]) -> None:
    sandbox.lego_will_produce(pairs["fresh"])
    result = sandbox.run("issue")
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == [LEGO.format(dns="timewebcloud"), PS_CADDY, RELOAD_CADDY]
    sandbox.assert_deployed(pairs["fresh"])
    assert (sandbox.certs / "fullchain.pem").stat().st_mode & 0o777 == 0o644
    assert (sandbox.certs / "privkey.pem").stat().st_mode & 0o777 == 0o600
    assert sandbox.certs.stat().st_mode & 0o777 == 0o700
    assert (sandbox.deploy / "lego").stat().st_mode & 0o777 == 0o700
    # Токен не попадает ни в аргументы docker, ни в вывод.
    assert TOKEN not in (sandbox.stubs / "docker.log").read_text() + result.stdout + result.stderr


def test_issue_leaves_stopped_caddy_alone(sandbox: Sandbox, pairs: dict[str, Path]) -> None:
    """Первый выпуск идёт до этапа gateway: Caddy ещё нет."""
    (sandbox.stubs / "caddy_running").unlink()
    sandbox.lego_will_produce(pairs["fresh"])
    result = sandbox.run("issue")
    assert result.returncode == 0, result.stderr
    assert RELOAD_CADDY not in sandbox.calls()
    assert "make gateway" in result.stdout
    sandbox.assert_deployed(pairs["fresh"])


def test_manual_issue_needs_no_token_and_reminds_about_renewal(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    sandbox.write_env({**FULL_ENV, "TIMEWEBCLOUD_AUTH_TOKEN": ""})
    sandbox.lego_will_produce(pairs["fresh"])
    result = sandbox.run("issue", "--manual")
    assert result.returncode == 0, result.stderr
    assert sandbox.calls()[0] == LEGO.format(dns="manual")
    assert "make cert-renew MANUAL=1" in result.stdout
    sandbox.assert_deployed(pairs["fresh"])


def test_repeated_issue_does_not_request_a_new_certificate(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    sandbox.lego_has(pairs["fresh"])
    sandbox.deployed(pairs["fresh"])
    result = sandbox.run("issue")
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == []


# --- продление ------------------------------------------------------------------


def test_renew_not_due_touches_nothing(sandbox: Sandbox, pairs: dict[str, Path]) -> None:
    sandbox.lego_has(pairs["fresh"])
    sandbox.deployed(pairs["fresh"])
    before = (sandbox.certs / "fullchain.pem").stat().st_mtime_ns
    result = sandbox.run("renew")
    assert result.returncode == 0, result.stderr
    assert "продление не требуется" in result.stdout
    assert sandbox.calls() == []
    assert (sandbox.certs / "fullchain.pem").stat().st_mtime_ns == before


def test_renew_due_replaces_files_and_reloads_caddy(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    sandbox.lego_has(pairs["expiring"])
    sandbox.deployed(pairs["expiring"])
    sandbox.lego_will_produce(pairs["renewed"])
    result = sandbox.run("renew")
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == [
        LEGO.format(dns="timewebcloud"),
        PS_CADDY,
        RELOAD_CADDY,
    ]
    sandbox.assert_deployed(pairs["renewed"])


def test_renew_that_changes_nothing_does_not_reload_caddy(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    """lego отработал без ошибки, но сертификат прежний: рабочие файлы и Caddy не тронуты."""
    sandbox.lego_has(pairs["expiring"])
    sandbox.deployed(pairs["expiring"])
    sandbox.lego_will_produce(pairs["expiring"])
    result = sandbox.run("renew")
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == [LEGO.format(dns="timewebcloud")]


def test_certificate_left_undeployed_by_interrupted_run_is_installed_without_lego(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    sandbox.lego_has(pairs["renewed"])
    sandbox.deployed(pairs["expiring"])
    result = sandbox.run("renew")
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == [PS_CADDY, RELOAD_CADDY]
    sandbox.assert_deployed(pairs["renewed"])


def test_renew_before_first_issue_is_refused(sandbox: Sandbox) -> None:
    assert_refused(sandbox.run("renew"), "ещё не выпускался", "make cert")
    assert sandbox.calls() == []


# --- отказы: рабочий сертификат и Caddy не тронуты --------------------------------


def test_failed_lego_leaves_working_certificate_and_caddy(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    sandbox.lego_has(pairs["expiring"])
    sandbox.deployed(pairs["expiring"])
    (sandbox.stubs / "lego_fail").touch()
    assert_refused(sandbox.run("renew"), "сертификат не получен", "не тронуты")
    assert sandbox.calls() == [LEGO.format(dns="timewebcloud")]
    sandbox.assert_deployed(pairs["expiring"])


def test_failed_first_issue_creates_nothing(sandbox: Sandbox) -> None:
    (sandbox.stubs / "lego_fail").touch()
    assert_refused(sandbox.run("issue"), "сертификат не получен")
    assert not sandbox.certs.exists()


def test_key_not_matching_certificate_is_refused_before_replacement(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    sandbox.lego_has(pairs["expiring"])
    sandbox.deployed(pairs["expiring"])
    sandbox.lego_will_produce(pairs["renewed"], key_from=pairs["fresh"])
    assert_refused(sandbox.run("renew"), "не соответствует сертификату")
    assert RELOAD_CADDY not in sandbox.calls()
    sandbox.assert_deployed(pairs["expiring"])


def test_certificate_for_another_host_is_refused_before_replacement(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    sandbox.deployed(pairs["fresh"])
    sandbox.lego_will_produce(pairs["other_host"])
    assert_refused(sandbox.run("issue"), f"выписан не на {HOST}")
    assert RELOAD_CADDY not in sandbox.calls()
    sandbox.assert_deployed(pairs["fresh"])


def test_failed_copy_does_not_leave_new_key_with_old_certificate(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    sandbox.lego_has(pairs["renewed"])
    sandbox.deployed(pairs["expiring"])
    (sandbox.stubs / "cp_fail").touch()
    assert_refused(sandbox.run("renew"), "не удалось скопировать", "не тронуты")
    assert RELOAD_CADDY not in sandbox.calls()
    sandbox.assert_deployed(pairs["expiring"])


def test_failed_caddy_reload_is_an_error_and_is_retried_by_next_run(
    sandbox: Sandbox, pairs: dict[str, Path]
) -> None:
    """Иначе таймер видел бы «уже разложен», а Caddy отдавал бы прежний сертификат."""
    sandbox.lego_has(pairs["renewed"])
    sandbox.deployed(pairs["expiring"])
    (sandbox.stubs / "reload_fail").touch()
    assert_refused(sandbox.run("renew"), "Caddy его не перечитал")
    assert_refused(sandbox.run("renew"), "Caddy его не перечитал")

    (sandbox.stubs / "reload_fail").unlink()
    (sandbox.stubs / "docker.log").unlink()
    result = sandbox.run("renew")
    assert result.returncode == 0, result.stderr
    assert sandbox.calls() == [PS_CADDY, RELOAD_CADDY]
    sandbox.assert_deployed(pairs["renewed"])
    (sandbox.stubs / "docker.log").unlink()
    assert sandbox.run("renew").returncode == 0
    assert sandbox.calls() == []


# --- отказы до обращения к lego ---------------------------------------------------


@pytest.mark.parametrize("missing", ["LLM_HOSTNAME", "ACME_EMAIL", "TIMEWEBCLOUD_AUTH_TOKEN"])
@pytest.mark.parametrize("command", ["issue", "renew"])
def test_missing_variable_is_refused(sandbox: Sandbox, command: str, missing: str) -> None:
    sandbox.write_env({**FULL_ENV, missing: ""})
    assert_refused(sandbox.run(command), f"{missing} в", "пуст")
    assert sandbox.calls() == []


@pytest.mark.parametrize("address", ["192.168.25.8", "localhost", "llm.example.ru/x"])
def test_hostname_that_is_not_a_domain_name_is_refused(sandbox: Sandbox, address: str) -> None:
    sandbox.write_env({**FULL_ENV, "LLM_HOSTNAME": address})
    result = sandbox.run("issue")
    assert_refused(result, "LLM_HOSTNAME")
    assert ("IP-адрес" in result.stderr) == (address == "192.168.25.8")
    assert sandbox.calls() == []


def test_missing_env_file_is_refused(sandbox: Sandbox) -> None:
    (sandbox.deploy / ".env").unlink()
    assert_refused(sandbox.run("issue"), "sudo make cert")


@pytest.mark.parametrize("args", [[], ["revoke"], ["issue", "--force"]])
def test_usage_errors(sandbox: Sandbox, args: list[str]) -> None:
    assert_refused(sandbox.run(*args), "--help")
    assert sandbox.calls() == []
