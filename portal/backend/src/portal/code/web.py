"""Шаблоны веб-видов, описание `api` и библиотеки клиента CoGIS (docs/portal-api.md §7.5.16).

Шаблоны и `api.d.ts` читаются при старте: нет файла или функций в описании — сервис не
стартует (`ConfigError`). Библиотеки клиента (`<code.sdk_index_dir>/client/`) читаются при
обращении и перечитываются по времени изменения файлов, как индекс SDK: их меняет
`make cogis-sdk` без перезапуска портала.
"""

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from portal.code.domain import WebAssets
from portal.code.kinds import kinds
from portal.core.settings import ConfigError

_FUNCTION = re.compile(r"^\s{2}(\w+)\(", re.M)
_INTERFACE = re.compile(r"^interface CogisApi \{$(.*?)^\}$", re.M | re.S)
LIBRARY_FILES = ("jquery.js", "knockout.js", "leaflet.js", "leaflet.css")
_VERSIONS = {
    "jquery": re.compile(r"jQuery (?:JavaScript Library )?v(\d+\.\d+\.\d+)"),
    "knockout": re.compile(r"Knockout JavaScript library v(\d+\.\d+\.\d+)"),
    "leaflet": re.compile(r"Leaflet (\d+\.\d+\.\d+)"),
}
_HEAD_BYTES = 2048
_ETAG_BYTES = 16


@dataclass(frozen=True)
class WebSnapshot:
    """Тело `GET /api/code/web` и его `ETag`."""

    body: bytes
    etag: str


def api_functions(text: str) -> tuple[str, ...]:
    r"""Имена функций `interface CogisApi` по `^\s{2}(\w+)\(` (§7.5.16)."""
    found = _INTERFACE.search(text)
    return tuple(_FUNCTION.findall(found.group(1))) if found else ()


def load_web_assets(directory: Path) -> WebAssets:
    """Прочитать и проверить каталог `code.web_dir`."""
    templates: dict[str, str] = {}
    for kind in kinds(()).values():
        if kind.template is None:
            continue
        path = directory / "templates" / kind.template
        try:
            templates[kind.template] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigError(f"Шаблон веб-вида не прочитан: {path}") from error
    path = directory / "api.d.ts"
    try:
        api_types = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Описание api не прочитано: {path}") from error
    functions = api_functions(api_types)
    if not functions or len(set(functions)) != len(functions):
        raise ConfigError(f"{path}: функции CogisApi — пустой список или с повторами")
    return WebAssets(templates, api_types, functions)


class ClientLibraries:
    """Библиотеки клиента CoGIS в `<code.sdk_index_dir>/client/` и тело `GET /api/code/web`."""

    def __init__(self, directory: Path, assets: WebAssets) -> None:
        """Запомнить каталог; файлы читаются при первом обращении."""
        self._directory = directory
        self._assets = assets
        self._lock = threading.Lock()
        self._stamp: tuple[tuple[str, int, int], ...] | None = None
        self._snapshot: WebSnapshot | None = None

    def _stamps(self) -> tuple[tuple[str, int, int], ...]:
        stamps = []
        for name in LIBRARY_FILES:
            try:
                stat = (self._directory / name).stat()
            except OSError:
                continue
            stamps.append((name, stat.st_mtime_ns, stat.st_size))
        return tuple(stamps)

    def _version(self, library: str) -> str | None:
        try:
            with (self._directory / f"{library}.js").open("rb") as file:
                head = file.read(_HEAD_BYTES).decode("utf-8", "replace")
        except OSError:
            return None
        found = _VERSIONS[library].search(head)
        return found.group(1) if found else None

    def snapshot(self) -> WebSnapshot:
        """Текущее тело ответа; блокирующее чтение файлов — вызывается в потоке."""
        with self._lock:
            stamp = self._stamps()
            if self._snapshot is None or stamp != self._stamp:
                present = {name for name, _, _ in stamp}
                body = json.dumps(
                    {
                        "api_types": self._assets.api_types,
                        "api_functions": list(self._assets.api_functions),
                        "libraries": [
                            {
                                "id": library,
                                "version": self._version(library),
                                "available": f"{library}.js" in present,
                            }
                            for library in _VERSIONS
                        ],
                    },
                    ensure_ascii=False,
                ).encode()
                digest = hashlib.sha256(body).hexdigest()[: _ETAG_BYTES * 2]
                self._stamp = stamp
                self._snapshot = WebSnapshot(body, f'"{digest}"')
            return self._snapshot

    def read(self, name: str) -> bytes | None:
        """Содержимое файла библиотеки; не из списка или нет файла — `None`."""
        if name not in LIBRARY_FILES:
            return None
        try:
            return (self._directory / name).read_bytes()
        except OSError:
            return None
