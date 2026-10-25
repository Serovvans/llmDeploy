"""Импорт и выгрузка ZIP, архив для CoGIS (docs/portal-api.md §7.5.5, §7.5.7).

Из архива ничего не исполняется и не распаковывается на диск: записи читаются в память,
по пределам из оглавления; `.csproj` разбирается без сущностей и без сети.
"""

import io
import zipfile
import zlib
from collections.abc import Sequence
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from lxml import etree

from portal.code import errors
from portal.code.domain import CodeLimits, CogisArchive, ImportResult
from portal.code.paths import content_error, limit_exceeded, path_error, utf8_size
from portal.core.errors import file_unreadable, unsupported_file_type
from portal.core.settings import CodePackage
from portal.files.text import decode_text
from portal.files.zipdir import zip_directory_bytes

_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")
_SKIPPED_DIRS = frozenset({"bin", "obj"})
_REPORT_PATH_CHARS = 300
_UTF8_BOM = "\ufeff"
# Папка рядом с файлом проекта, куда сотрудник кладёт DLL с сервера CoGIS (Прочтите.txt).
_HINT_DIR = "cogis\\"
_READ_ERRORS = (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError)
# Ссылки без индекса SDK — только две обязательные DLL (§7.5.5).
REQUIRED_DLLS = ("eLiteGIS.Server.dll", "Atemiko.eLiteMap.dll")


def _skipped_silently(parts: Sequence[str]) -> bool:
    """Служебные папки Visual Studio: `bin`, `obj` и имена с точки (`.vs`, `.git`)."""
    return any(part.lower() in _SKIPPED_DIRS or part.startswith(".") for part in parts[:-1])


def _common_folder(paths: Sequence[str]) -> int:
    """Число общих начальных папок путей (без учёта регистра)."""
    split = [path.split("/")[:-1] for path in paths]
    if not split:
        return 0
    depth = 0
    for parts in zip(*split, strict=False):
        if len({part.lower() for part in parts}) != 1:
            break
        depth += 1
    return min(depth, min(len(parts) for parts in split))


def _read(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes | None:
    """Содержимое записи не длиннее `limit` байт; `None` — длиннее (оглавление солгало)."""
    with archive.open(info) as entry:
        data = entry.read(limit + 1)
    return None if len(data) > limit else data


def _package_references(data: bytes) -> list[tuple[str, str | None]]:
    """`PackageReference` файла `.csproj` в любом пространстве имён XML: `Include` и версия."""
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )
    root = etree.fromstring(data, parser)
    found: list[tuple[str, str | None]] = []
    for element in root.iter():
        if not isinstance(element.tag, str) or etree.QName(element).localname != "PackageReference":
            continue
        include = element.get("Include")
        if not include:
            continue
        version = element.get("Version")
        if version is None:
            child = next(
                (
                    item
                    for item in element
                    if isinstance(item.tag, str) and etree.QName(item).localname == "Version"
                ),
                None,
            )
            version = None if child is None or child.text is None else child.text.strip()
        found.append((include.strip(), version))
    return found


def read_import(
    data: bytes,
    limits: CodeLimits,
    packages: Sequence[CodePackage],
    max_directory_bytes: int,
) -> ImportResult:
    """Разобрать архив импорта по шагам §7.5.5, 1–6."""
    if not data.startswith(_SIGNATURES):
        raise unsupported_file_type()
    buffer = io.BytesIO(data)
    try:
        size = zip_directory_bytes(buffer)
    except zipfile.BadZipFile as error:
        raise file_unreadable() from error
    if size is None or size > max_directory_bytes:
        raise file_unreadable()
    try:
        with zipfile.ZipFile(buffer) as archive:
            return _read_entries(archive, limits, packages)
    except _READ_ERRORS as error:
        raise file_unreadable() from error


def _read_entries(
    archive: zipfile.ZipFile, limits: CodeLimits, packages: Sequence[CodePackage]
) -> ImportResult:
    result = ImportResult()
    sources: list[tuple[str, zipfile.ZipInfo]] = []
    projects: list[tuple[str, zipfile.ZipInfo]] = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/"):
            continue
        parts = name.split("/")
        if _skipped_silently(parts):
            continue
        if name.lower().endswith(".cs"):
            sources.append((f"{name[:-3]}.cs", info))
        else:
            result.ignored_files += 1
            if name.lower().endswith(".csproj"):
                projects.append((name, info))
    depth = _common_folder([path for path, _ in sources])
    taken: set[str] = set()
    count = size = 0
    for path, info in sorted(
        (("/".join(path.split("/")[depth:]), info) for path, info in sources),
        key=lambda item: item[0].lower(),
    ):
        reason = _source(archive, info, path, limits, taken, count, size, result)
        if reason is not None:
            # В отчёте — имя записи архива, как его видит сотрудник (§7.5.5).
            result.skipped_files.append(
                {"path": info.filename[:_REPORT_PATH_CHARS], "reason": reason}
            )
            continue
        taken.add(path.lower())
        count += 1
        size += utf8_size(result.files[-1][1])
    if not result.files:
        raise errors.import_no_files()
    _match_packages(archive, projects, limits, packages, result)
    return result


def _source(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    path: str,
    limits: CodeLimits,
    taken: set[str],
    count: int,
    size: int,
    result: ImportResult,
) -> str | None:
    """Взять один файл `.cs` в `result.files`; вернуть причину пропуска или `None`."""
    if path_error(path, limits.extensions) is not None or any(
        other.startswith(f"{path.lower()}/") or path.lower().startswith(f"{other}/")
        for other in taken
    ):
        return "invalid_path"
    if path.lower() in taken:
        return "duplicate"
    if info.file_size > limits.file_max_bytes:
        return "file_too_large"
    try:
        data = _read(archive, info, limits.file_max_bytes)
    except _READ_ERRORS:
        return "unreadable"
    if data is None:
        return "file_too_large"
    if b"\x00" in data:
        return "not_text"
    text = decode_text(data)
    if text is None:
        return "unreadable"
    if content_error(text, limits.file_max_bytes) is not None:
        return "file_too_large"
    if limit_exceeded(count + 1, size + utf8_size(text), limits) is not None:
        return "project_limit"
    result.files.append((path, text))
    return None


def _match_packages(
    archive: zipfile.ZipFile,
    projects: Sequence[tuple[str, zipfile.ZipInfo]],
    limits: CodeLimits,
    packages: Sequence[CodePackage],
    result: ImportResult,
) -> None:
    """Пакеты из `.csproj`: совпавшие с набором — в проект, прочие — в отчёт (шаг 6)."""
    known = {item.id.lower(): item for item in packages}
    chosen: set[str] = set()
    for name, info in projects:
        try:
            data = (
                None
                if info.file_size > limits.file_max_bytes
                else _read(archive, info, limits.file_max_bytes)
            )
            references = [] if data is None else _package_references(data)
        except (*_READ_ERRORS, etree.XMLSyntaxError, ValueError):
            data, references = None, []
        if data is None:
            result.skipped_files.append({"path": name[:_REPORT_PATH_CHARS], "reason": "unreadable"})
            continue
        for package_id, version in references:
            offline = known.get(package_id.lower())
            if offline is not None:
                chosen.add(offline.id.lower())
            if offline is None or version != offline.version:
                entry = {
                    "id": package_id[:_REPORT_PATH_CHARS],
                    "version": None if version is None else version[:_REPORT_PATH_CHARS],
                    "offline_version": None if offline is None else offline.version,
                }
                if entry not in result.unmatched_packages:
                    result.unmatched_packages.append(entry)
    result.packages = [item.id for item in packages if item.id.lower() in chosen]


def _entry(name: str, moment: datetime) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=moment.timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _memo_lines(lines: Sequence[str]) -> str:
    """Подстановка памятки: каждая строка — с отступом в два пробела (portal-ui.md)."""
    return "\n".join(f"  {line}" for line in lines)


def csproj(name: str, references: Sequence[str], packages: Sequence[tuple[str, str]]) -> str:
    """Проект Visual Studio 2022 для .NET 8 с настройками песочницы (§7.5.5)."""
    reference_items = "".join(
        f"    <Reference Include={quoteattr(file_name.removesuffix('.dll'))}>\n"
        f"      <HintPath>{escape(_HINT_DIR + file_name)}</HintPath>\n"
        "      <Private>false</Private>\n"
        "    </Reference>\n"
        for file_name in references
    )
    package_items = "".join(
        f"    <PackageReference Include={quoteattr(package_id)} Version={quoteattr(version)} />\n"
        for package_id, version in packages
    )
    groups = f"  <ItemGroup>\n{reference_items}  </ItemGroup>\n"
    if package_items:
        groups += f"  <ItemGroup>\n{package_items}  </ItemGroup>\n"
    return (
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        "  <PropertyGroup>\n"
        "    <TargetFramework>net8.0</TargetFramework>\n"
        "    <ImplicitUsings>enable</ImplicitUsings>\n"
        "    <Nullable>enable</Nullable>\n"
        f"    <AssemblyName>{escape(name)}</AssemblyName>\n"
        f"    <RootNamespace>{escape(name)}</RootNamespace>\n"
        "  </PropertyGroup>\n"
        f"{groups}"
        "</Project>\n"
    )


def export_zip(
    name: str,
    files: Sequence[tuple[str, str]],
    references: Sequence[str],
    packages: Sequence[tuple[str, str]],
    memo: str,
    moment: datetime,
) -> bytes:
    """Выгрузка проекта: папка `<name>/` с файлами, `.csproj` и `Прочтите.txt` (§7.5.5)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files:
            archive.writestr(_entry(f"{name}/{path}", moment), content.encode())
        archive.writestr(
            _entry(f"{name}/{name}.csproj", moment), csproj(name, references, packages).encode()
        )
        text = memo.replace("{files}", _memo_lines(references))
        archive.writestr(_entry(f"{name}/Прочтите.txt", moment), (_UTF8_BOM + text).encode())
    return buffer.getvalue()


_KIND_WORDS = {
    "tool": "инструмент",
    "package": "пакет, которого нет на сервере CoGIS",
    "memo": "эта памятка",
}
MEMO_NAME = "Памятка.txt"


def cogis_archive(
    name: str,
    dll: bytes,
    bundle: Sequence[tuple[str, bytes]],
    on_server: Sequence[str],
    factory: str | None,
    tools: Sequence[tuple[str, str | None]],
    sdk_version: str | None,
    memo: str,
    moment: datetime,
) -> CogisArchive:
    """DLL инструмента, DLL пакетов не с сервера и памятка (§7.5.7)."""
    listing = [
        {"file_name": f"{name}.dll", "kind": "tool"},
        *({"file_name": file_name, "kind": "package"} for file_name, _ in bundle),
        {"file_name": MEMO_NAME, "kind": "memo"},
    ]
    values = {
        "{dll_path}": f"/usr/elitegis/plugins/{name}.dll",
        "{factory}": factory or "",
        "{files}": _memo_lines(
            [f"{item['file_name']} — {_KIND_WORDS[item['kind']]}" for item in listing]
        ),
        "{on_server}": _memo_lines(list(on_server) or ["нет"]),
        "{tools}": _memo_lines(
            [f"{title} — {category or 'без категории'}" for title, category in tools]
        ),
        "{sdk_version}": sdk_version or "",
    }
    text = memo
    for key, value in values.items():
        text = text.replace(key, value)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_entry(f"{name}.dll", moment), dll)
        for file_name, content in bundle:
            archive.writestr(_entry(file_name, moment), content)
        archive.writestr(_entry(MEMO_NAME, moment), (_UTF8_BOM + text).encode())
    return CogisArchive(buffer.getvalue(), {"files": listing, "on_server": list(on_server)})


class ZipProjectArchives:
    """Реализация порта `ProjectArchives`: ZIP в памяти, без распаковки на диск (§7.5.5)."""

    def read_import(
        self,
        data: bytes,
        limits: CodeLimits,
        packages: Sequence[CodePackage],
        max_directory_bytes: int,
    ) -> ImportResult:
        """Разобрать архив импорта (§7.5.5, шаги 1–6)."""
        return read_import(data, limits, packages, max_directory_bytes)

    def export_zip(
        self,
        name: str,
        files: Sequence[tuple[str, str]],
        references: Sequence[str],
        packages: Sequence[tuple[str, str]],
        memo: str,
        moment: datetime,
    ) -> bytes:
        """Выгрузка проекта для Visual Studio."""
        return export_zip(name, files, references, packages, memo, moment)

    def cogis_archive(
        self,
        name: str,
        dll: bytes,
        bundle: Sequence[tuple[str, bytes]],
        on_server: Sequence[str],
        factory: str | None,
        tools: Sequence[tuple[str, str | None]],
        sdk_version: str | None,
        memo: str,
        moment: datetime,
    ) -> CogisArchive:
        """Архив для CoGIS с памяткой."""
        return cogis_archive(
            name, dll, bundle, on_server, factory, tools, sdk_version, memo, moment
        )
