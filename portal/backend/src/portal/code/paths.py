"""Имя проекта, пути файлов и пределы проекта (docs/portal-api.md §7.5.3); чистые функции."""

import re
from collections.abc import Iterable
from typing import Literal

from portal.code.domain import CodeLimits

PATH_MAX_CHARS = 200
PATH_MAX_PARTS = 8
# Ключевые слова C#, которые нельзя взять именем без `@` (спецификация C# 12, §6.4.4).
CSHARP_KEYWORDS = frozenset(
    {
        "abstract",
        "as",
        "base",
        "bool",
        "break",
        "byte",
        "case",
        "catch",
        "char",
        "checked",
        "class",
        "const",
        "continue",
        "decimal",
        "default",
        "delegate",
        "do",
        "double",
        "else",
        "enum",
        "event",
        "explicit",
        "extern",
        "false",
        "finally",
        "fixed",
        "float",
        "for",
        "foreach",
        "goto",
        "if",
        "implicit",
        "in",
        "int",
        "interface",
        "internal",
        "is",
        "lock",
        "long",
        "namespace",
        "new",
        "null",
        "object",
        "operator",
        "out",
        "override",
        "params",
        "private",
        "protected",
        "public",
        "readonly",
        "ref",
        "return",
        "sbyte",
        "sealed",
        "short",
        "sizeof",
        "stackalloc",
        "static",
        "string",
        "struct",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "uint",
        "ulong",
        "unchecked",
        "unsafe",
        "ushort",
        "using",
        "virtual",
        "void",
        "volatile",
        "while",
    }
)
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
# Занятые имена каркаса: типы шаблонов мастера и примера; пространство имён проекта
# скрыло бы их (`CS0118`). С учётом регистра, как ключевые слова (§7.5.3).
SCAFFOLD_NAMES = frozenset(
    {
        "AddContactTool",
        "DateTime",
        "GpCategory",
        "GpCategoryAttribute",
        "GpDescription",
        "GpDescriptionAttribute",
        "GpRequired",
        "GpRequiredAttribute",
        "GpTool",
        "GpToolFactory",
        "GpToolIcon",
        "GpToolIconAttribute",
        "IGpToolFactory",
        "NpgsqlCommand",
        "NpgsqlConnection",
        "NpgsqlConnectionStringBuilder",
        "Type",
        "UserException",
        "Workspace",
        "WorkspaceName",
    }
)
# Эталонные сборки .NET 8 с простым именем без точки: `<name>.dll` проекта спорила бы с
# ними. Без учёта регистра, вместе с `id` пакетов `code.packages` (§7.5.3, А-123).
FRAMEWORK_NAMES = frozenset({"mscorlib", "netstandard", "system", "windowsbase"})
_PROJECT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_DRIVE = re.compile(r"[A-Za-z]:")


def windows_reserved(name: str) -> bool:
    """Имя до первой точки — зарезервированное имя Windows (`nul.cs`, `Com1.Helper.cs`)."""
    return name.partition(".")[0].lower() in _WINDOWS_RESERVED


def project_name_error(name: str, packages: Iterable[str]) -> str | None:
    """Код поля для имени проекта; `None` — имя годно.

    `packages` — `id` всех пакетов `code.packages`: имя проекта не совпадает с ними без
    учёта регистра, как и с `FRAMEWORK_NAMES`. Проверяется только при создании (А-96).
    """
    folded = name.lower()
    if (
        not _PROJECT_NAME.fullmatch(name)
        or name in CSHARP_KEYWORDS
        or name in SCAFFOLD_NAMES
        or windows_reserved(name)
        or folded in FRAMEWORK_NAMES
        or any(folded == package.lower() for package in packages)
    ):
        return "invalid_format"
    return None


def path_error(path: str, extensions: Iterable[str]) -> str | None:
    """Код поля для пути файла: `outside_project`, `invalid_format`, `extension_not_allowed`."""
    parts = path.split("/")
    if (
        path.startswith("/")
        or _DRIVE.match(path)
        or "\\" in path
        or any(part in (".", "..") for part in parts)
    ):
        return "outside_project"
    if not 1 <= len(path) <= PATH_MAX_CHARS or not 1 <= len(parts) <= PATH_MAX_PARTS:
        return "invalid_format"
    for part in parts:
        if not _PART.fullmatch(part) or part.endswith(".") or windows_reserved(part):
            return "invalid_format"
    if not any(path.endswith(extension) for extension in extensions):
        return "extension_not_allowed"
    return None


def content_error(content: str, file_max_bytes: int) -> str | None:
    """Код поля для содержимого: `invalid_format` (U+0000) или `too_long` (байты UTF-8)."""
    if "\x00" in content:
        return "invalid_format"
    if len(content.encode()) > file_max_bytes:
        return "too_long"
    return None


def conflicts(path: str, others: Iterable[str]) -> bool:
    """Путь совпадает без учёта регистра с папкой другого файла или лежит «внутри» файла."""
    mine = path.lower()
    for other in others:
        theirs = other.lower()
        if theirs.startswith(f"{mine}/") or mine.startswith(f"{theirs}/"):
            return True
    return False


def limit_exceeded(
    files: int, size_bytes: int, limits: CodeLimits
) -> Literal["files", "bytes"] | None:
    """Какой предел проекта превышен: `files`, `bytes` или `None`."""
    if files > limits.max_files:
        return "files"
    if size_bytes > limits.project_max_bytes:
        return "bytes"
    return None


def utf8_size(text: str) -> int:
    """Длина текста в байтах UTF-8."""
    return len(text.encode())
