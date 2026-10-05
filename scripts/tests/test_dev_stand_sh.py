"""Логика dev_stand.sh с подменённым ``docker``: проект compose и какие тома удаляются."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "dev_stand.sh"
# Имя проекта — из строки name: настоящего файла compose стенда.
COMPOSE_PREFIX = f"compose -p portal-dev -f {SCRIPT.parent}/../portal/dev/docker-compose.yml "
UP = "up -d --build --wait"
# Записывает вызов; отвечает на запрос списка томов и на поиск тома по меткам (том
# «существует», если его ключ есть в файле existing).
DOCKER_STUB = """#!/usr/bin/env bash
call="$*"
echo "$call" >>"$STUB_DIR/docker.log"
case "$call" in
  *"config --volumes")
    [[ ! -e "$STUB_DIR/broken_config" ]] || exit 1
    printf 'portal-files\\nportal-db-data\\nqdrant-data\\ncaddy-data\\n'
    ;;
  "volume ls"*)
    project="${call#*label=com.docker.compose.project=}"
    project="${project%% *}"
    key="${call##*label=com.docker.compose.volume=}"
    if grep -qx "$key" "$STUB_DIR/existing"; then echo "${project}_${key}"; fi
    ;;
esac
"""
ALL_VOLUMES = ["portal-files", "portal-db-data", "qdrant-data", "caddy-data"]


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    stub = tmp_path / "docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)
    (tmp_path / "existing").write_text("".join(f"{name}\n" for name in ALL_VOLUMES))
    return tmp_path


def run(stubs: Path, *args: str, project_in_env: str = "") -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash") or "/bin/bash"
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "STUB_DIR": str(stubs)}
    if project_in_env:
        env["COMPOSE_PROJECT_NAME"] = project_in_env
    return subprocess.run(
        [bash, str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False
    )


def calls(stubs: Path) -> list[str]:
    log = stubs / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def actions(stubs: Path) -> list[str]:
    """Вызовы docker, которые что-то меняют: без чтения конфигурации и поиска томов."""
    stripped = [line.removeprefix(COMPOSE_PREFIX) for line in calls(stubs)]
    return [call for call in stripped if not call.startswith(("config", "volume ls"))]


def test_reset_removes_data_volumes_between_down_and_up_and_keeps_caddy_data(stubs: Path) -> None:
    result = run(stubs, "reset")
    assert result.returncode == 0, result.stderr
    assert actions(stubs) == [
        "down",
        "volume rm portal-dev_portal-files",
        "volume rm portal-dev_portal-db-data",
        "volume rm portal-dev_qdrant-data",
        UP,
    ]
    assert "portal-dev_caddy-data сохранён" in result.stdout


def test_volume_that_does_not_exist_is_skipped(stubs: Path) -> None:
    (stubs / "existing").write_text("portal-db-data\ncaddy-data\n")
    assert run(stubs, "reset").returncode == 0
    assert actions(stubs) == ["down", "volume rm portal-dev_portal-db-data", UP]


@pytest.mark.parametrize("command", ["up", "down", "reset"])
def test_project_name_from_environment_cannot_redirect_the_stand(stubs: Path, command: str) -> None:
    """COMPOSE_PROJECT_NAME рабочего стека: команды всё равно идут в проект стенда."""
    assert run(stubs, command, project_in_env="llm").returncode == 0
    for call in calls(stubs):
        assert call.startswith(COMPOSE_PREFIX) or call.startswith("volume ")
        assert "llm" not in call.replace(str(SCRIPT.parent), "")


def test_up_and_down_touch_no_volumes(stubs: Path) -> None:
    assert run(stubs, "up").returncode == 0
    assert run(stubs, "down").returncode == 0
    assert actions(stubs) == [UP, "down"]


def test_unreadable_compose_config_leaves_stand_running(stubs: Path) -> None:
    (stubs / "broken_config").touch()
    result = run(stubs, "reset")
    assert result.returncode == 1
    assert "portal/dev/.env" in result.stderr
    assert actions(stubs) == []


def test_unknown_command_is_refused(stubs: Path) -> None:
    assert run(stubs, "purge").returncode == 1
    assert calls(stubs) == []
