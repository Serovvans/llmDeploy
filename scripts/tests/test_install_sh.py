"""Логика install.sh, не требующая ВМ: .env, профили этапов, сборка и проверки портала.

Скрипт копируется во временное дерево (``scripts/`` + ``deploy/``) и подключается через
``source``; ``docker``, ``git``, ``curl`` и ``uv`` подменены заглушками в ``PATH``.
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
# Образ «есть», только если он перечислен в файле images (туда же пишет `docker tag`);
# `compose config --services` печатает файл services, с `--env-file` отказывает при файле
# reject-env; у контейнера сервиса svc идентификатор c-svc, у его образа — sha256:c-svc.
DOCKER_STUB = """#!/usr/bin/env bash
echo "$*" >>"$STUB_DIR/docker.log"
case " $* " in
  " image inspect "*) grep -qx "$3" "$STUB_DIR/images" 2>/dev/null || exit 1 ;;
  " volume inspect "*) exit 1 ;;
  " tag "*) echo "$3" >>"$STUB_DIR/images" ;;
  " inspect --format "*) echo "sha256:${!#}" ;;
  *" --env-file "*) [[ ! -e "$STUB_DIR/reject-env" ]] || exit 1; cat "$STUB_DIR/services" ;;
  *" config --services "*) cat "$STUB_DIR/services" 2>/dev/null ;;
  *" config --images "*) echo "vllm/vllm-openai:v0.29.0" ;;
  *" ps -a -q "* | *" ps -q "*) echo "c-${!#}" ;;
  *" portal migrate ") [[ ! -e "$STUB_DIR/fail-migrate" ]] || exit 1 ;;
  *" cp "*) touch "${!#}" ;;
esac
exit 0
"""
# Коммит клона — файл .commit в его каталоге; незакоммиченные правки — файл dirty.
GIT_STUB = """#!/usr/bin/env bash
dir=""
while [[ $# -gt 0 && "$1" != "rev-parse" && "$1" != "status" ]]; do
  if [[ "$1" == "-C" ]]; then dir="$2"; fi
  shift
done
if [[ "$1" == "rev-parse" ]]; then cat "$dir/.commit"; else cat "$STUB_DIR/dirty" 2>/dev/null; fi
exit 0
"""
COMMIT = "0123456789ab"
# Записывает аргументы и каталог запуска (with_env.sh переходит в scripts/).
UV_STUB = """#!/usr/bin/env bash
echo "$* cwd=$(basename "$PWD")" >>"$STUB_DIR/uv.log"
[[ ! -e "$STUB_DIR/fail-uv" ]]
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
    "/v1/rerank": 404,
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
        shutil.copy(REPO / "scripts" / "with_env.sh", root / "scripts")
        shutil.copy(REPO / "deploy" / ".env.example", root / "deploy")
        stubs = (("docker", DOCKER_STUB), ("git", GIT_STUB), ("curl", CURL_STUB), ("uv", UV_STUB))
        for name, body in stubs:
            stub = self.stubs / name
            stub.write_text(body)
            stub.chmod(0o755)
        (root / ".commit").write_text(COMMIT + "\n")
        self.set_codes(PORTAL_CODES)

    def set_images(self, *images: str) -> None:
        """Объявить образы существующими для ``docker image inspect``."""
        (self.stubs / "images").write_text("".join(f"{image}\n" for image in images))

    def images(self) -> list[str]:
        file = self.stubs / "images"
        return file.read_text().splitlines() if file.exists() else []

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

    def uv_calls(self) -> list[str]:
        log = self.stubs / "uv.log"
        return log.read_text().splitlines() if log.exists() else []

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


BUILD = "build portal-api portal-worker portal-web sql-sandbox code-sandbox"
RELEASE_IMAGES = [
    f"llm-{service}:{COMMIT}"
    for service in ("portal-api", "portal-worker", "portal-web", "sql-sandbox", "code-sandbox")
]


def builds(sandbox: Sandbox) -> list[str]:
    return [call for call in sandbox.docker_calls() if " build " in call]


def test_portal_images_are_built_together_with_sql_sandbox(sandbox: Sandbox) -> None:
    sandbox.write_env({"PORTAL_IMAGE_TAG": COMMIT})
    result = sandbox.run("portal_enabled=1\nbuild_portal_images")
    assert result.returncode == 0, result.stderr
    assert builds(sandbox) == [f"compose -f {sandbox.root}/deploy/docker-compose.yml {BUILD}"]


def test_images_prepared_for_this_commit_are_not_rebuilt_in_the_window(sandbox: Sandbox) -> None:
    sandbox.write_env({"PORTAL_IMAGE_TAG": COMMIT})
    sandbox.set_images(*RELEASE_IMAGES)
    result = sandbox.run("portal_enabled=1\nbuild_portal_images")
    assert result.returncode == 0, result.stderr
    assert builds(sandbox) == []
    assert "сборка пропущена" in result.stdout


@pytest.mark.parametrize("reason", ["dirty", "one image missing"])
def test_prepared_images_are_rebuilt_when_they_may_not_match_the_code(
    sandbox: Sandbox, reason: str
) -> None:
    sandbox.write_env({"PORTAL_IMAGE_TAG": COMMIT})
    if reason == "dirty":
        sandbox.set_images(*RELEASE_IMAGES)
        (sandbox.stubs / "dirty").write_text(" M portal/backend/src/portal/cli.py\n")
    else:
        sandbox.set_images(*RELEASE_IMAGES[:-1])
    assert sandbox.run("portal_enabled=1\nbuild_portal_images").returncode == 0
    assert len(builds(sandbox)) == 1


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
    assert calls[:2] == ["config -q", "pull --ignore-buildable"]
    assert calls.index(BUILD) < calls.index("up -d --wait")
    assert calls[-2] == "up -d --wait"
    assert calls[-1].startswith("cp caddy:")
    assert sum(call.startswith("run --rm --no-deps") for call in calls) == 2
    assert sandbox.env()["COMPOSE_PROFILES"] == "gateway,embeddings,portal"
    assert sandbox.env()["PORTAL_IMAGE_TAG"] == COMMIT
    assert "Этап 3 завершён" in result.stdout


def test_gateway_stage_recreates_bifrost_and_caddy_to_apply_configs(sandbox: Sandbox) -> None:
    """Без пересоздания Caddy работал бы с Caddyfile прежнего релиза."""
    assert sandbox.run("gateway_enabled=1\napply_mounted_configs").returncode == 0
    recreate = "up -d --force-recreate --no-deps --wait bifrost caddy"
    assert sandbox.docker_calls()[-1].endswith(recreate)


def test_model_stage_has_nothing_to_recreate(sandbox: Sandbox) -> None:
    assert sandbox.run("gateway_enabled=0\napply_mounted_configs").returncode == 0
    assert sandbox.docker_calls() == []


# --- пакет 0.5: .env прежней версии, реранкер, веса ----------------------------

NEW_VARIABLES = (
    "SQL_SANDBOX_PASSWORD",
    "SQL_SANDBOX_CONTROL_PASSWORD",
    "SQL_SANDBOX_ROLES",
    "PORTAL_IMAGE_TAG",
    "RERANK_MODEL_ID",
    "RERANK_GPU_MEM_UTIL",
)
LIVE_PORTAL_ENV = {**PORTAL_READY_ENV, "COMPOSE_PROFILES": "gateway,embeddings,portal"}


def write_env_of_previous_release(sandbox: Sandbox, values: dict[str, str]) -> None:
    """``.env`` работающей ВМ: строк с переменными пакета 0.5 в нём нет вовсе."""
    sandbox.write_env(values)
    lines = sandbox.env_file.read_text().splitlines()
    kept = [line for line in lines if not line.startswith(NEW_VARIABLES)]
    sandbox.env_file.write_text("\n".join(kept) + "\n")


@pytest.mark.parametrize("stage", ["model", "gateway", "portal"])
def test_env_of_previous_release_is_completed_on_every_stage(sandbox: Sandbox, stage: str) -> None:
    kept = {**GATEWAY_ENV, "GPU_MEM_UTIL": "0.80", "PORTAL_DB_PASSWORD": "db-password"}
    write_env_of_previous_release(sandbox, {**PORTAL_READY_ENV, **kept})
    result = sandbox.prepare_env("--stage", stage)
    assert result.returncode == 0, result.stderr
    env = sandbox.env()
    assert len(env["SQL_SANDBOX_PASSWORD"]) == len(env["SQL_SANDBOX_CONTROL_PASSWORD"]) == 64
    assert env["SQL_SANDBOX_PASSWORD"] != env["SQL_SANDBOX_CONTROL_PASSWORD"]
    assert env["PORTAL_IMAGE_TAG"] == COMMIT
    assert env["RERANK_MODEL_ID"] == "BAAI/bge-reranker-v2-m3"
    assert env["RERANK_GPU_MEM_UTIL"] == "0.04"
    assert env["SQL_SANDBOX_ROLES"] == "2"
    assert {name: env[name] for name in kept} == kept


def test_generated_sandbox_passwords_survive_a_rerun(sandbox: Sandbox) -> None:
    write_env_of_previous_release(sandbox, GATEWAY_ENV)
    assert sandbox.prepare_env("--stage", "gateway").returncode == 0
    first = sandbox.env()
    assert sandbox.prepare_env("--stage", "gateway").returncode == 0
    for name in ("SQL_SANDBOX_PASSWORD", "SQL_SANDBOX_CONTROL_PASSWORD"):
        assert sandbox.env()[name] == first[name]


def test_image_tag_follows_the_commit_only_while_portal_is_enabled(sandbox: Sandbox) -> None:
    sandbox.write_env({**GATEWAY_ENV, "COMPOSE_PROFILES": "gateway", "PORTAL_IMAGE_TAG": "old"})
    assert sandbox.prepare_env("--stage", "gateway").returncode == 0
    assert sandbox.env()["PORTAL_IMAGE_TAG"] == "old"
    sandbox.write_env({**LIVE_PORTAL_ENV, "PORTAL_IMAGE_TAG": "old"})
    assert sandbox.prepare_env().returncode == 0
    assert sandbox.env()["PORTAL_IMAGE_TAG"] == COMMIT


def test_rerank_is_refused_without_embeddings(sandbox: Sandbox) -> None:
    sandbox.write_env(GATEWAY_ENV)
    result = sandbox.prepare_env("--stage", "gateway", "--profiles", "rerank,monitoring")
    assert_refused(result, "rerank требует профиль embeddings")
    assert sandbox.env()["COMPOSE_PROFILES"] == ""


def test_rerank_is_refused_until_main_model_share_is_lowered(sandbox: Sandbox) -> None:
    sandbox.write_env(PORTAL_READY_ENV)
    result = sandbox.prepare_env("--stage", "portal", "--profiles", "embeddings,rerank")
    assert_refused(result, "сумма долей видеопамяти больше 0.92", "GPU_MEM_UTIL=0.81")

    args = ("--stage", "portal", "--profiles", "embeddings,rerank", "--gpu-mem-util", "0.81")
    result = sandbox.prepare_env(*args)
    assert result.returncode == 0, result.stderr
    env = sandbox.env()
    assert env["COMPOSE_PROFILES"] == "gateway,embeddings,rerank,portal"
    assert env["GPU_MEM_UTIL"] == "0.81"


def test_refused_release_leaves_env_and_rollback_copy_intact(sandbox: Sandbox) -> None:
    """Забытый GPU_MEM_UTIL=0.81 в окне: отказ, затем повтор — откат вернёт прежний .env."""
    write_env_of_previous_release(sandbox, {**LIVE_PORTAL_ENV, "GPU_MEM_UTIL": "0.85"})
    before = sandbox.env_file.read_text()
    saved = sandbox.root / "deploy" / "rollback" / ".env"

    result = sandbox.prepare_env("--stage", "portal", "--profiles", "embeddings,rerank")
    assert_refused(result, "сумма долей видеопамяти больше 0.92")
    assert sandbox.env_file.read_text() == before
    assert not saved.exists()

    args = ("--stage", "portal", "--profiles", "embeddings,rerank", "--gpu-mem-util", "0.81")
    assert sandbox.prepare_env(*args).returncode == 0
    assert saved.read_text() == before
    assert sandbox.env()["COMPOSE_PROFILES"] == "gateway,embeddings,rerank,portal"


def test_weights_of_reranker_and_speech_model_are_preloaded(sandbox: Sandbox) -> None:
    sandbox.write_env({"RERANK_MODEL_ID": "BAAI/bge-reranker-v2-m3"})
    result = sandbox.run(
        "profiles=gateway,embeddings,rerank,portal\nportal_enabled=1\npreload_models"
    )
    assert result.returncode == 0, result.stderr
    calls = sandbox.docker_calls()
    assert any(call.endswith("vllm download BAAI/bge-reranker-v2-m3") for call in calls)
    speech = [call for call in calls if call.startswith("run --rm ")]
    assert len(speech) == 2
    for call, directory in zip(speech, ("gigaam-v3-e2e-rnnt", "silero"), strict=True):
        assert "-v llm_asr-models:/models/asr --entrypoint hf vllm/vllm-openai:v0.29.0" in call
        assert call.endswith(f"--local-dir /models/asr/{directory}")
        assert " --revision " in call
        assert "int8" not in call
    volume = next(call for call in calls if call.startswith("volume create"))
    assert "com.docker.compose.project=llm" in volume
    assert "com.docker.compose.volume=asr-models llm_asr-models" in volume


def test_speech_weights_are_not_loaded_without_portal(sandbox: Sandbox) -> None:
    sandbox.write_env({})
    assert sandbox.run("profiles=gateway\nportal_enabled=0\npreload_models").returncode == 0
    assert not any("asr" in call for call in sandbox.docker_calls())
    assert sandbox.uv_calls() == []


def test_offline_packages_are_loaded_with_portal_config_of_this_clone(sandbox: Sandbox) -> None:
    sandbox.write_env(GATEWAY_ENV)
    result = sandbox.run("live_dir=\nportal_enabled=1\npreload_code_packages")
    assert result.returncode == 0, result.stderr
    assert sandbox.uv_calls() == ["run code_packages.py cwd=scripts"]


def test_release_preparation_loads_packages_with_live_overlay(sandbox: Sandbox) -> None:
    sandbox.write_env(GATEWAY_ENV)
    result = sandbox.run("live_dir=/opt/llm\nportal_enabled=1\npreload_code_packages")
    assert result.returncode == 0, result.stderr
    assert sandbox.uv_calls()[0].startswith(
        "run code_packages.py --config-dir /opt/llm/deploy/portal "
    )


def test_failed_package_load_stops_installation(sandbox: Sandbox) -> None:
    sandbox.write_env(GATEWAY_ENV)
    (sandbox.stubs / "fail-uv").touch()
    result = sandbox.run("live_dir=\nportal_enabled=1\npreload_code_packages")
    assert_refused(result, "офлайн-набор пакетов не загружен", "make code-packages")


# --- релиз: подготовка и путь отката -------------------------------------------

ROLLBACK_ENV = "deploy/rollback/.env"


def test_env_before_release_is_saved_for_rollback(sandbox: Sandbox) -> None:
    write_env_of_previous_release(sandbox, {**LIVE_PORTAL_ENV, "GPU_MEM_UTIL": "0.85"})
    before = sandbox.env_file.read_text()
    args = ("--profiles", "embeddings,rerank", "--gpu-mem-util", "0.81")
    assert sandbox.prepare_env(*args).returncode == 0
    saved = sandbox.root / ROLLBACK_ENV
    assert saved.read_text() == before
    assert saved.stat().st_mode & 0o777 == 0o600
    assert saved.parent.stat().st_mode & 0o777 == 0o700

    # Повторный запуск на том же коммите не затирает путь отката новым .env.
    assert sandbox.prepare_env().returncode == 0
    assert saved.read_text() == before


def test_second_release_commit_in_the_window_keeps_the_copy_from_before_it(
    sandbox: Sandbox,
) -> None:
    """Исправление неудачного релиза ставится следующим коммитом: откат — на релиз до окна."""
    write_env_of_previous_release(sandbox, {**LIVE_PORTAL_ENV, "GPU_MEM_UTIL": "0.85"})
    before = sandbox.env_file.read_text()
    args = ("--profiles", "embeddings,rerank", "--gpu-mem-util", "0.81")
    assert sandbox.prepare_env(*args).returncode == 0
    failed_release = sandbox.env_file.read_text()

    (sandbox.root / ".commit").write_text("c0ffee000000\n")
    assert sandbox.prepare_env().returncode == 0
    assert (sandbox.root / ROLLBACK_ENV).read_text() == before
    # .env неудачного релиза — отдельной копией под его тегом.
    saved = sandbox.root / "deploy" / "rollback" / COMMIT / ".env"
    assert saved.read_text() == failed_release
    assert saved.stat().st_mode & 0o777 == 0o600


def test_first_portal_install_saves_nothing_for_rollback(sandbox: Sandbox) -> None:
    sandbox.write_env(PORTAL_READY_ENV)
    assert sandbox.prepare_env("--stage", "portal").returncode == 0
    assert not (sandbox.root / ROLLBACK_ENV).exists()


@pytest.mark.parametrize(
    ("args", "expected"), [("", "preflight:"), ("--prepare-from /x", "preflight:--release")]
)
def test_release_preparation_runs_release_preflight(
    sandbox: Sandbox, args: str, expected: str
) -> None:
    (sandbox.root / "scripts" / "preflight.sh").write_text('echo "preflight:$*"\n')
    result = sandbox.run(f"parse_args {args}\nrun_preflight")
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout.splitlines()


class LiveClone:
    """Рабочий клон рядом с релизным: свой коммит и ``.env`` прежней версии."""

    commit = "feedfacecafe"

    def __init__(self, sandbox: Sandbox) -> None:
        self.root = sandbox.root / "live"
        (self.root / "deploy").mkdir(parents=True)
        (self.root / ".commit").write_text(self.commit + "\n")
        write_env_of_previous_release(sandbox, LIVE_PORTAL_ENV)
        self.env_file = self.root / "deploy" / ".env"
        sandbox.env_file.rename(self.env_file)
        (sandbox.stubs / "services").write_text("vllm\nportal-api\nportal-worker\nportal-web\n")


PREPARE = """
check_system() {{ :; }}
start_stack() {{ echo STACK-STARTED; }}
main --prepare-from "{live}" --skip-preflight --profiles embeddings,rerank --gpu-mem-util 0.81
"""


def test_release_is_prepared_without_touching_live_clone_or_stack(sandbox: Sandbox) -> None:
    live = LiveClone(sandbox)
    live_env = live.env_file.read_text()
    result = sandbox.run(PREPARE.format(live=live.root))
    assert result.returncode == 0, result.stderr

    assert live.env_file.read_text() == live_env
    assert sorted(path.name for path in live.root.rglob("*") if path.is_file()) == [
        ".commit",
        ".env",
    ]
    # Копия .env с секретами в релизном клоне не остаётся.
    assert not sandbox.env_file.exists()
    assert "STACK-STARTED" not in result.stdout
    calls = sandbox.docker_calls()
    assert not any(" up " in call or " cp " in call for call in calls)

    # Нынешние образы помечены коммитом рабочего клона до сборки новых.
    old = [
        f"llm-{service}:{live.commit}" for service in ("portal-api", "portal-worker", "portal-web")
    ]
    assert sandbox.images() == old
    last_tag = max(index for index, call in enumerate(calls) if call.startswith("tag "))
    assert last_tag < next(index for index, call in enumerate(calls) if call.endswith(BUILD))
    assert "tag sha256:c-portal-api llm-portal-api:feedfacecafe" in calls
    assert f"git checkout {COMMIT}" in result.stdout
    # Копия и откат — из релизного клона: в прежнем коммите рабочего их целей нет.
    assert f"make release-backup LIVE={live.root} DIR=" in result.stdout
    assert f"git checkout {live.commit}\n" in result.stdout
    assert f"make release-rollback LIVE={live.root}\n" in result.stdout


def test_prepare_refuses_to_run_in_the_live_clone(sandbox: Sandbox) -> None:
    sandbox.write_env(LIVE_PORTAL_ENV)
    before = sandbox.env_file.read_text()
    result = sandbox.run(PREPARE.format(live=sandbox.root))
    assert_refused(result, "из отдельного клона")
    assert sandbox.env_file.read_text() == before
    assert sandbox.docker_calls() == []


def test_prepare_stops_when_running_image_cannot_be_tagged(sandbox: Sandbox) -> None:
    live = LiveClone(sandbox)
    (live.root / ".commit").write_text(COMMIT + "\n")
    result = sandbox.run(PREPARE.format(live=live.root))
    assert_refused(result, "на том же коммите")
    assert builds(sandbox) == []


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
        ("/v1/rerank", 401, "POST /v1/rerank через 443 закрыт: HTTP 401"),
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
