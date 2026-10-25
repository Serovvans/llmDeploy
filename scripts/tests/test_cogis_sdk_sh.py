"""Логика cogis_sdk.sh без ВМ: проверка папки SDK, запуск индекса, проверка версии.

Скрипт копируется во временное дерево (``scripts/`` + ``deploy/``) и подключается через
``source``; ``docker`` подменён заглушкой в ``PATH``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TAG = "0123456789ab"
# Записывает вызов; образ «есть», если нет файла no-image; индекс падает при fail-index;
# exec в portal-api печатает содержимое файла version (пусто — SDK не виден).
DOCKER_STUB = """#!/usr/bin/env bash
echo "$*" >>"$STUB_DIR/docker.log"
case " $* " in
  " image inspect "*) [[ ! -e "$STUB_DIR/no-image" ]] || exit 1 ;;
  " volume inspect "*) exit 1 ;;
  " run "*) [[ ! -e "$STUB_DIR/fail-index" ]] || { echo "error: no plugins.txt" >&2; exit 1; } ;;
  *" exec "*) cat "$STUB_DIR/version" ;;
esac
exit 0
"""


class Tree:
    """Временная копия скрипта с ``deploy/.env``, папкой SDK и заглушкой docker."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.stubs = root / "bin"
        self.sdk = root / "deploy" / "cogis-sdk"
        (root / "scripts").mkdir()
        self.sdk.mkdir(parents=True)
        self.stubs.mkdir()
        shutil.copy(REPO / "scripts" / "cogis_sdk.sh", root / "scripts")
        (root / "deploy" / ".env").write_text(
            f"COMPOSE_PROFILES=gateway,embeddings,portal\nPORTAL_IMAGE_TAG={TAG}\n"
        )
        stub = self.stubs / "docker"
        stub.write_text(DOCKER_STUB)
        stub.chmod(0o755)
        (self.stubs / "version").write_text("13.0.1234.0\n")
        for name in ("eLiteGIS.Server.dll", "Atemiko.eLiteMap.dll", "Npgsql.dll"):
            (self.sdk / name).write_bytes(b"MZ")
        (self.sdk / "plugins.txt").write_text("Npgsql.dll\nNewtonsoft.Json.dll\n")

    def docker_calls(self) -> list[str]:
        log = self.stubs / "docker.log"
        return log.read_text().splitlines() if log.exists() else []

    def run(self, body: str) -> subprocess.CompletedProcess[str]:
        script = f'source "{self.root}/scripts/cogis_sdk.sh"\n{body}\n'
        path = f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin"
        bash = shutil.which("bash") or "/bin/bash"
        return subprocess.run(
            [bash, "-c", script],
            capture_output=True,
            text=True,
            env={"PATH": path, "STUB_DIR": str(self.stubs)},
            check=False,
        )

    def main(self) -> subprocess.CompletedProcess[str]:
        """``main`` без проверки root: права root нужны только на ВМ."""
        return self.run("check_root() { :; }\nmain")


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


def assert_refused(result: subprocess.CompletedProcess[str], *fragments: str) -> None:
    assert result.returncode == 1
    assert "ОШИБКА" in result.stderr
    for fragment in fragments:
        assert fragment in result.stderr


def test_sdk_is_indexed_offline_then_sandbox_restarted_and_version_shown(tree: Tree) -> None:
    result = tree.main()
    assert result.returncode == 0, result.stderr
    calls = tree.docker_calls()
    index = next(call for call in calls if call.startswith("run "))
    assert index.startswith("run --rm --network none --read-only --cap-drop ALL ")
    assert f"-v {tree.sdk}:/sdk:ro -v llm_cogis-sdk-index:/index " in index
    assert index.endswith(f"llm-code-sandbox:{TAG} index --sdk /sdk --out /index")
    volume = next(call for call in calls if call.startswith("volume create"))
    assert "com.docker.compose.volume=cogis-sdk-index llm_cogis-sdk-index" in volume
    restart = next(i for i, call in enumerate(calls) if "--force-recreate" in call)
    assert calls[restart].endswith("up -d --no-deps --force-recreate --wait code-sandbox")
    assert calls.index(index) < restart
    assert "exec -T portal-api python -c" in calls[-1]
    assert "http://code-sandbox:8080/health" in calls[-1]
    assert "SDK CoGIS подключён: версия 13.0.1234.0" in result.stdout
    assert "enabled: true" in result.stdout


@pytest.mark.parametrize("name", ["eLiteGIS.Server.dll", "Atemiko.eLiteMap.dll", "plugins.txt"])
def test_missing_required_file_is_refused_before_docker(tree: Tree, name: str) -> None:
    (tree.sdk / name).unlink()
    assert_refused(tree.main(), f"нет {name}")
    assert tree.docker_calls() == []


def test_missing_sdk_folder_is_refused(tree: Tree) -> None:
    shutil.rmtree(tree.sdk)
    assert_refused(tree.main(), "нет папки", "cogis-sdk")


def test_subfolders_and_empty_plugin_list_are_warned_about(tree: Tree) -> None:
    (tree.sdk / "plugins").mkdir()
    (tree.sdk / "plugins.txt").write_text("\n")
    result = tree.run("check_sdk_dir")
    assert result.returncode == 0, result.stderr
    assert "подпапки" in result.stdout
    assert "(plugins)" in result.stdout
    assert "нет ни одного .dll" in result.stdout


def test_missing_image_points_to_make_portal(tree: Tree) -> None:
    (tree.stubs / "no-image").touch()
    assert_refused(tree.main(), f"нет образа llm-code-sandbox:{TAG}", "make portal")
    assert not any(call.startswith("run ") for call in tree.docker_calls())


def test_failed_index_does_not_restart_sandbox(tree: Tree) -> None:
    (tree.stubs / "fail-index").touch()
    assert_refused(tree.main(), "индекс SDK не построен")
    assert not any("--force-recreate" in call for call in tree.docker_calls())


def test_sandbox_without_sdk_version_is_a_failure(tree: Tree) -> None:
    (tree.stubs / "version").write_text("")
    assert_refused(tree.main(), "песочница не видит SDK")


def test_portal_must_be_deployed_first(tree: Tree) -> None:
    (tree.root / "deploy" / ".env").write_text("COMPOSE_PROFILES=gateway,embeddings\n")
    assert_refused(tree.run("check_portal_deployed"), "нет portal", "make portal")
