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
PASSWORD = "db-secret-42"
# Npgsql и две его зависимости, как в стартовом наборе code.packages.
PACKAGES = (
    "Npgsql\t8.0.5\tsha-a\n"
    "Microsoft.Extensions.Logging.Abstractions\t8.0.2\tsha-b\n"
    "System.Diagnostics.DiagnosticSource\t8.0.1\tsha-c\n"
)
# Записывает вызов; образ «есть», если нет файла no-image-<имя>; portal code-packages
# печатает файл packages (с fail-packages — ошибку) и записывает полученный пароль;
# индекс падает при fail-index; exec в portal-api печатает файл version (пусто — SDK не
# виден).
DOCKER_STUB = """#!/usr/bin/env bash
echo "$*" >>"$STUB_DIR/docker.log"
case " $* " in
  " image inspect llm-code-sandbox:"*) [[ ! -e "$STUB_DIR/no-image-code-sandbox" ]] || exit 1 ;;
  " image inspect llm-portal-api:"*) [[ ! -e "$STUB_DIR/no-image-portal-api" ]] || exit 1 ;;
  " run "*" portal code-packages ")
    printf '%s' "${PORTAL_DB_PASSWORD-}" >"$STUB_DIR/password"
    [[ ! -e "$STUB_DIR/fail-packages" ]] || { echo "Не задана PORTAL_DB_PASSWORD" >&2; exit 1; }
    cat "$STUB_DIR/packages" ;;
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
            f"PORTAL_DB_PASSWORD='{PASSWORD}'\n"
        )
        stub = self.stubs / "docker"
        stub.write_text(DOCKER_STUB)
        stub.chmod(0o755)
        (self.stubs / "version").write_text("13.0.1234.0\n")
        (self.stubs / "packages").write_text(PACKAGES)
        for name in ("eLiteGIS.Server.dll", "Atemiko.eLiteMap.dll", "Npgsql.dll"):
            (self.sdk / name).write_bytes(b"MZ")
        (self.sdk / "plugins.txt").write_text("Npgsql.dll\nNewtonsoft.Json.dll\n")

    def docker_calls(self) -> list[str]:
        log = self.stubs / "docker.log"
        return log.read_text().splitlines() if log.exists() else []

    def index_called(self) -> bool:
        return any(" index --sdk " in call for call in self.docker_calls())

    def sandbox_recreated(self) -> bool:
        return any("--force-recreate" in call for call in self.docker_calls())

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
    index = next(call for call in calls if " index --sdk " in call)
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


@pytest.mark.parametrize("service", ["code-sandbox", "portal-api"])
def test_missing_image_points_to_make_portal(tree: Tree, service: str) -> None:
    (tree.stubs / f"no-image-{service}").touch()
    assert_refused(tree.main(), f"нет образа llm-{service}:{TAG}", "make portal")
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


def test_package_on_server_without_dll_copy_is_refused_before_index(tree: Tree) -> None:
    (tree.sdk / "Npgsql.dll").unlink()
    (tree.sdk / "plugins.txt").write_text(" npgsql.dll \nNewtonsoft.Json.dll\n")
    result = tree.main()
    assert_refused(
        result,
        f"нет в {tree.sdk}: npgsql.dll\n",
        "/usr/elitegis/plugins/",
        "make cogis-sdk",
    )
    assert not tree.index_called()
    assert not tree.sandbox_recreated()


def test_dll_copy_matches_without_case(tree: Tree) -> None:
    (tree.sdk / "Npgsql.dll").unlink()
    (tree.sdk / "NPGSQL.DLL").write_bytes(b"MZ")
    (tree.sdk / "plugins.txt").write_text(" npgsql.dll \n")
    result = tree.main()
    assert result.returncode == 0, result.stderr
    assert tree.index_called()


def test_all_missing_copies_are_named(tree: Tree) -> None:
    (tree.sdk / "Npgsql.dll").unlink()
    (tree.sdk / "plugins.txt").write_text(
        "Npgsql.dll\nMicrosoft.Extensions.Logging.Abstractions.dll\nreadme.txt\n"
    )
    assert_refused(tree.main(), ": Npgsql.dll Microsoft.Extensions.Logging.Abstractions.dll\n")


def test_package_not_on_server_needs_no_dll(tree: Tree) -> None:
    # Npgsql.dll в папке; зависимости в plugins.txt не названы — их копии не нужны.
    result = tree.main()
    assert result.returncode == 0, result.stderr


def test_empty_package_set_passes(tree: Tree) -> None:
    (tree.sdk / "Npgsql.dll").unlink()
    (tree.stubs / "packages").write_text("")
    result = tree.main()
    assert result.returncode == 0, result.stderr
    assert tree.index_called()


def test_failed_package_list_is_refused_with_its_stderr(tree: Tree) -> None:
    (tree.stubs / "fail-packages").touch()
    assert_refused(tree.main(), "portal code-packages", "Не задана PORTAL_DB_PASSWORD")
    assert not tree.index_called()
    assert not tree.sandbox_recreated()


def test_package_list_runs_offline_with_password_from_env_not_argv(tree: Tree) -> None:
    result = tree.main()
    assert result.returncode == 0, result.stderr
    calls = tree.docker_calls()
    listing = next(call for call in calls if call.endswith(" portal code-packages"))
    assert listing == (
        "run --rm --network none -e PORTAL_DB_PASSWORD "
        f"-v {tree.root / 'deploy' / 'portal'}:/etc/portal:ro "
        f"llm-portal-api:{TAG} portal code-packages"
    )
    assert calls.index(listing) < next(i for i, c in enumerate(calls) if " index --sdk " in c)
    assert (tree.stubs / "password").read_text() == PASSWORD
    assert all(PASSWORD not in call for call in calls)
