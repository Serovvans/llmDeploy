"""Тесты code_packages.py: разбор списка, раскладка тома, проверка SHA-512 — без сети и docker."""

import base64
import hashlib
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

import code_packages
from code_packages import Package, PackageError

NUPKG = b"PK\x03\x04 npgsql package"
SHA = base64.b64encode(hashlib.sha512(NUPKG).digest()).decode()
NPGSQL = Package("Npgsql", "9.0.3", SHA)


def client_serving(files: dict[str, bytes], calls: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        body = files.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    return code_packages.build_client(httpx.MockTransport(handler))


def test_package_list_is_parsed_in_order() -> None:
    other = base64.b64encode(b"\x01" * 64).decode()
    text = f"Npgsql\t9.0.3\t{SHA}\n\nMicrosoft.Extensions.Logging.Abstractions\t8.0.2\t{other}\n"
    assert code_packages.parse_package_list(text) == [
        NPGSQL,
        Package("Microsoft.Extensions.Logging.Abstractions", "8.0.2", other),
    ]


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        (f"Npgsql 9.0.3 {SHA}", "через табуляцию"),
        (f"../etc\t9.0.3\t{SHA}", "недопустимый id"),
        (f"Npgsql\t..\t{SHA}", "недопустимая версия"),
        ("Npgsql\t9.0.3\tnot-base64!", "не SHA-512"),
        (f"Npgsql\t9.0.3\t{base64.b64encode(b'short').decode()}", "не SHA-512"),
        (f"Npgsql\t9.0.3\t{SHA}\nnpgsql\t9.0.4\t{SHA}", "указан дважды"),
    ],
)
def test_bad_package_list_is_refused(line: str, fragment: str) -> None:
    with pytest.raises(PackageError, match=fragment):
        code_packages.parse_package_list(line)


def test_layout_and_url_follow_nuget_org_in_lower_case() -> None:
    package = Package("Microsoft.Extensions.Logging.Abstractions", "8.0.2-RC", SHA)
    name = "microsoft.extensions.logging.abstractions"
    assert package.relative_path() == Path(name, "8.0.2-rc", f"{name}.8.0.2-rc.nupkg")
    assert package.url() == (
        f"https://api.nuget.org/v3-flatcontainer/{name}/8.0.2-rc/{name}.8.0.2-rc.nupkg"
    )


def test_matching_package_is_stored_readable(tmp_path: Path) -> None:
    with client_serving({NPGSQL.url(): NUPKG}) as client:
        assert code_packages.sync_package(client, NPGSQL, tmp_path).startswith("загружен")
    stored = tmp_path / "npgsql" / "9.0.3" / "npgsql.9.0.3.nupkg"
    assert stored.read_bytes() == NUPKG
    assert stored.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in stored.parent.iterdir()] == [stored.name]


def test_mismatching_package_is_not_stored(tmp_path: Path) -> None:
    with (
        client_serving({NPGSQL.url(): NUPKG + b"tampered"}) as client,
        pytest.raises(PackageError, match="не совпал"),
    ):
        code_packages.sync_package(client, NPGSQL, tmp_path)
    assert not list(tmp_path.rglob("*.nupkg*"))


def test_present_package_with_right_sum_is_not_downloaded(tmp_path: Path) -> None:
    stored = tmp_path / NPGSQL.relative_path()
    stored.parent.mkdir(parents=True)
    stored.write_bytes(NUPKG)
    calls: list[str] = []
    with client_serving({}, calls) as client:
        assert code_packages.sync_package(client, NPGSQL, tmp_path) == "уже есть"
    assert calls == []


def test_present_package_with_wrong_sum_is_replaced(tmp_path: Path) -> None:
    stored = tmp_path / NPGSQL.relative_path()
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b"truncated")
    with client_serving({NPGSQL.url(): NUPKG}) as client:
        code_packages.sync_package(client, NPGSQL, tmp_path)
    assert stored.read_bytes() == NUPKG


def test_one_failure_does_not_stop_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = Package("Missing", "1.0.0", SHA)
    with client_serving({NPGSQL.url(): NUPKG}) as client:
        assert not code_packages.sync_packages(client, [missing, NPGSQL], tmp_path)
    out = capsys.readouterr().out
    assert "FAIL  Missing 1.0.0" in out
    assert "HTTP 404" in out
    assert "OK    Npgsql 9.0.3" in out


class FakeDocker:
    """Записывает вызовы; том «есть», если ``volume_exists``."""

    def __init__(self, listing: str, *, volume_exists: bool, mountpoint: Path) -> None:
        self.listing = listing
        self.volume_exists = volume_exists
        self.mountpoint = mountpoint
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "run":
            return self.listing
        if list(args[:2]) == ["volume", "inspect"] and len(args) == 3:
            if not self.volume_exists:
                raise PackageError("no such volume")
            return "[]"
        if args[:2] == ["volume", "create"]:
            self.volume_exists = True
            return "llm_code-packages\n"
        return f"{self.mountpoint}\n"


def test_run_lists_offline_without_secret_in_argv_and_creates_labelled_volume(
    tmp_path: Path,
) -> None:
    docker = FakeDocker(f"Npgsql\t9.0.3\t{SHA}\n", volume_exists=False, mountpoint=tmp_path)
    settings = code_packages.Settings(image="llm-portal-api:abc", config_dir=Path("/live/portal"))
    assert code_packages.run(settings, docker, client_serving({NPGSQL.url(): NUPKG}))
    listing = docker.calls[0]
    assert listing[:4] == ["run", "--rm", "--network", "none"]
    assert "PORTAL_DB_PASSWORD" in listing
    assert not any("=" in arg and "PASSWORD" in arg for arg in listing)
    assert "/live/portal:/etc/portal:ro" in listing
    assert listing[-3:] == ["llm-portal-api:abc", "portal", "code-packages"]
    create = next(call for call in docker.calls if call[:2] == ["volume", "create"])
    assert "com.docker.compose.project=llm" in create
    assert "com.docker.compose.volume=code-packages" in create
    assert create[-1] == "llm_code-packages"
    assert (tmp_path / NPGSQL.relative_path()).is_file()


def test_empty_list_touches_nothing(tmp_path: Path) -> None:
    docker = FakeDocker("\n", volume_exists=False, mountpoint=tmp_path)
    settings = code_packages.Settings(image="llm-portal-api:abc", config_dir=tmp_path)
    assert code_packages.run(settings, docker, client_serving({}))
    assert len(docker.calls) == 1


@pytest.mark.parametrize("missing", ["PORTAL_IMAGE_TAG", "PORTAL_DB_PASSWORD"])
def test_settings_require_env_from_with_env(missing: str) -> None:
    environ = {"PORTAL_IMAGE_TAG": "abc", "PORTAL_DB_PASSWORD": "secret"}
    del environ[missing]
    with pytest.raises(PackageError, match=f"{missing} не задан"):
        code_packages.load_settings([], environ)


def test_settings_build_image_name_from_tag(tmp_path: Path) -> None:
    environ = {"PORTAL_IMAGE_TAG": "abc", "PORTAL_DB_PASSWORD": "secret"}
    settings = code_packages.load_settings(["--config-dir", str(tmp_path)], environ)
    assert settings == code_packages.Settings("llm-portal-api:abc", tmp_path.resolve())
