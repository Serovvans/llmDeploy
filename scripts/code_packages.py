"""Загрузка офлайн-набора пакетов NuGet для песочницы сборки C#, docs/portal-api.md §7.5.14.

Список пакетов берётся из действующей конфигурации портала (с накладкой ВМ
``deploy/portal/config.override.yaml``) командой ``portal code-packages`` образа
``portal-api``: строка на пакет, через табуляцию — ``id``, ``version``, ``sha512``.
Каждый ``.nupkg`` загружается с nuget.org и кладётся в том ``llm_code-packages`` в
раскладке nuget.org (``<id>/<version>/<id>.<version>.nupkg``, всё в нижнем регистре),
только если его SHA-512 совпал с суммой из списка. Файл, уже лежащий с верной суммой,
заново не загружается. Песочница монтирует том только для чтения; в работе портал и
песочница в интернет не обращаются (docs/portal-design.md §7, §8.5).

Запуск на ВМ, от root (том пишется через его каталог на хосте), при подготовке релиза
и после правки ``code.packages`` в накладке:
    sudo bash scripts/with_env.sh uv run code_packages.py [--config-dir DIR]

Аргументы:
    --config-dir DIR  каталог накладки конфигурации портала, монтируется в
                      ``/etc/portal``; по умолчанию ``deploy/portal`` этого клона. При
                      подготовке релиза из отдельного клона — каталог рабочего клона.

Окружение (из deploy/.env через with_env.sh):
    PORTAL_IMAGE_TAG    тег образа ``llm-portal-api``, из которого берётся список.
    PORTAL_DB_PASSWORD  его требует общая загрузка настроек портала; к базе команда не
                        обращается. Передаётся контейнеру через окружение, не в argv.
    HTTPS_PROXY         корпоративный прокси, если исходящий 443 идёт через него.
"""

import argparse
import base64
import binascii
import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger("code_packages")

NUGET_BASE_URL = "https://api.nuget.org/v3-flatcontainer"
# Имя проекта compose (строка name: в docker-compose.yml) и том офлайн-набора.
COMPOSE_PROJECT = "llm"
VOLUME = "code-packages"
PORTAL_API_IMAGE = f"{COMPOSE_PROJECT}-portal-api"
CONFIG_MOUNT = "/etc/portal"
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "deploy" / "portal"
DOWNLOAD_TIMEOUT_S = 120.0
DOCKER_TIMEOUT_S = 300
SHA512_BYTES = 64
# Первый символ — буква или цифра: «.» и «..» не становятся частью пути в томе.
PACKAGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+-]*")


class PackageError(Exception):
    """Список пакетов, загрузка или окружение не позволяют продолжить."""


@dataclass(frozen=True)
class Package:
    """Пакет офлайн-набора: ``sha512`` — SHA-512 ``.nupkg`` в base64, как у nuget.org."""

    id: str
    version: str
    sha512: str

    @property
    def file_name(self) -> str:
        """Имя файла в раскладке nuget.org: ``<id>.<version>.nupkg`` в нижнем регистре."""
        return f"{self.id.lower()}.{self.version.lower()}.nupkg"

    def relative_path(self) -> Path:
        """Путь файла внутри тома набора."""
        return Path(self.id.lower(), self.version.lower(), self.file_name)

    def url(self) -> str:
        """Адрес ``.nupkg`` в плоском каталоге nuget.org."""
        return f"{NUGET_BASE_URL}/{self.id.lower()}/{self.version.lower()}/{self.file_name}"


def parse_package_list(text: str) -> list[Package]:
    """Разобрать вывод ``portal code-packages``: ``id<TAB>version<TAB>sha512`` на строку.

    Пустые строки пропускаются; неверная строка или повтор ``id`` (без учёта регистра) —
    ``PackageError`` с номером строки.
    """
    packages: list[Package] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.strip().split("\t")
        if len(fields) != 3:
            raise PackageError(f"строка {number}: ожидалось id, version и sha512 через табуляцию")
        package_id, version, sha512 = fields
        if not PACKAGE_ID_RE.fullmatch(package_id):
            raise PackageError(f"строка {number}: недопустимый id пакета «{package_id}»")
        if not VERSION_RE.fullmatch(version):
            raise PackageError(f"строка {number}: недопустимая версия «{version}» у {package_id}")
        try:
            digest = base64.b64decode(sha512, validate=True)
        except binascii.Error:
            digest = b""
        if len(digest) != SHA512_BYTES:
            raise PackageError(f"строка {number}: sha512 у {package_id} — не SHA-512 в base64")
        if package_id.lower() in seen:
            raise PackageError(f"строка {number}: пакет {package_id} указан дважды")
        seen.add(package_id.lower())
        packages.append(Package(package_id, version, sha512))
    return packages


def sha512_base64(data: bytes) -> str:
    """SHA-512 в base64 — в той форме, в какой nuget.org публикует ``packageHash``."""
    return base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def build_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Клиент nuget.org; ``transport`` подменяется в тестах. Прокси — из HTTPS_PROXY."""
    return httpx.Client(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True, transport=transport)


def download(client: httpx.Client, package: Package) -> bytes:
    """Загрузить ``.nupkg``; ответ не 200 или обрыв — ``PackageError``."""
    try:
        response = client.get(package.url())
    except httpx.HTTPError as exc:
        raise PackageError(f"{package.url()}: {exc}") from exc
    if response.status_code != httpx.codes.OK:
        raise PackageError(f"{package.url()}: HTTP {response.status_code}")
    return response.content


def write_atomically(path: Path, data: bytes) -> None:
    """Записать файл через временный в том же каталоге: недописанного файла не остаётся."""
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        # Песочница читает том под своим пользователем, не root.
        tmp.chmod(0o644)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def sync_package(client: httpx.Client, package: Package, dest: Path) -> str:
    """Положить пакет в ``dest``, если его там нет с верной суммой; вернуть итог строкой."""
    path = dest / package.relative_path()
    if path.is_file():
        if sha512_base64(path.read_bytes()) == package.sha512:
            return "уже есть"
        logger.warning("%s: сумма лежащего файла не совпала, загружается заново", path)
    data = download(client, package)
    actual = sha512_base64(data)
    if actual != package.sha512:
        raise PackageError(
            f"{package.url()}: SHA-512 {actual} не совпал с суммой из списка {package.sha512}; "
            "файл не сохранён"
        )
    write_atomically(path, data)
    return f"загружен, {len(data)} байт"


def sync_packages(client: httpx.Client, packages: Sequence[Package], dest: Path) -> bool:
    """Синхронизировать набор; печатает строку на пакет. True — все пакеты на месте."""
    ok = True
    for package in packages:
        try:
            outcome = sync_package(client, package, dest)
        except PackageError as exc:
            ok = False
            print(f"FAIL  {package.id} {package.version}: {exc}")
        else:
            print(f"OK    {package.id} {package.version}: {outcome}")
    return ok


# --- docker ---------------------------------------------------------------------

DockerRunner = Callable[[Sequence[str]], str]


def run_docker(args: Sequence[str]) -> str:
    """Выполнить ``docker ...`` и вернуть stdout; отказ — ``PackageError`` со stderr."""
    command = ["docker", *args]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackageError(f"не удалось выполнить {' '.join(command)}: {exc}") from exc
    if result.returncode != 0:
        raise PackageError(f"{' '.join(command)}: {result.stderr.strip()[:500]}")
    return result.stdout


def list_command(image: str, config_dir: Path) -> list[str]:
    """Аргументы ``docker`` для ``portal code-packages``: без сети, накладка только для чтения.

    ``-e PORTAL_DB_PASSWORD`` без значения: docker берёт его из своего окружения, и пароль
    не попадает в список процессов.
    """
    return [
        "run", "--rm", "--network", "none", "-e", "PORTAL_DB_PASSWORD",
        "-v", f"{config_dir}:{CONFIG_MOUNT}:ro", image, "portal", "code-packages",
    ]  # fmt: skip


def volume_name() -> str:
    """Полное имя тома набора, как его создаёт compose."""
    return f"{COMPOSE_PROJECT}_{VOLUME}"


def ensure_volume(docker: DockerRunner) -> Path:
    """Создать том набора, если его нет, и вернуть его каталог на хосте.

    Метки compose: том создаётся раньше первого запуска песочницы, и без них compose
    предупреждал бы, что том создан не им.
    """
    name = volume_name()
    try:
        docker(["volume", "inspect", name])
    except PackageError:
        docker([
            "volume", "create", "--label", f"com.docker.compose.project={COMPOSE_PROJECT}",
            "--label", f"com.docker.compose.volume={VOLUME}", name,
        ])  # fmt: skip
        logger.info("создан том %s", name)
    return Path(docker(["volume", "inspect", "--format", "{{.Mountpoint}}", name]).strip())


@dataclass(frozen=True)
class Settings:
    """Параметры запуска: аргументы CLI и окружение, прочитанные в одном месте."""

    image: str
    config_dir: Path


def load_settings(argv: Sequence[str] | None, environ: Mapping[str, str]) -> Settings:
    """Прочитать аргументы и окружение; нет нужной переменной — ``PackageError``."""
    parser = argparse.ArgumentParser(description="Загрузка офлайн-набора пакетов NuGet.")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help="каталог накладки конфигурации портала (по умолчанию deploy/portal этого клона)",
    )
    args = parser.parse_args(argv)
    for name in ("PORTAL_IMAGE_TAG", "PORTAL_DB_PASSWORD"):
        if not environ.get(name):
            raise PackageError(
                f"{name} не задан: запускать через scripts/with_env.sh (берёт deploy/.env)"
            )
    return Settings(
        image=f"{PORTAL_API_IMAGE}:{environ['PORTAL_IMAGE_TAG']}",
        config_dir=args.config_dir.resolve(),
    )


def run(
    settings: Settings, docker: DockerRunner = run_docker, client: httpx.Client | None = None
) -> bool:
    """Получить список, подготовить том и загрузить пакеты; True — набор полный."""
    listing = docker(list_command(settings.image, settings.config_dir))
    packages = parse_package_list(listing)
    if not packages:
        logger.info("офлайн-набор в конфигурации пуст: загружать нечего")
        return True
    dest = ensure_volume(docker)
    logger.info("офлайн-набор: %d пакетов в том %s", len(packages), volume_name())
    with client or build_client() as http:
        return sync_packages(http, packages, dest)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI: 0 — все пакеты на месте, 1 — нет."""
    logging.basicConfig(level=logging.INFO, format="==> %(message)s")
    try:
        ok = run(load_settings(argv, os.environ))
    except PackageError as exc:
        logger.error("ОШИБКА: %s", exc)
        return 1
    if not ok:
        logger.error(
            "ОШИБКА: не все пакеты загружены. Что делать: проверить доступ к api.nuget.org "
            "(docs/devops-request.md) и суммы code.packages в накладке; повторить: "
            "make code-packages"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
