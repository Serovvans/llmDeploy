"""Виды проектов «Кода» и их постоянные (docs/portal-api.md §7.5.16); чистые функции."""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from portal.code.domain import CodeLimits

WEB_KINDS = ("js_plugin", "html_widget", "catalog", "card_hooks")
NAME_MAX_CHARS = 64
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
INSERTIONS = ("head_start", "head_end", "body_start", "body_end")
CLIENTS = ("smartphone", "tablet", "desktop")


@dataclass(frozen=True)
class Kind:
    """Вид проекта: расширения, «один файл» и файл шаблона веб-вида."""

    id: str
    extensions: tuple[str, ...]
    single_file: bool
    template: str | None


def kinds(gptool_extensions: tuple[str, ...]) -> dict[str, Kind]:
    """Все виды по порядку таблицы §7.5.16; расширения `gptool` — `code.file_extensions`."""
    listed = [
        Kind("gptool", gptool_extensions, False, None),
        Kind("js_plugin", (".js",), True, "plugin.js"),
        Kind("html_widget", (".html",), True, "widget.html"),
        Kind("catalog", (".css", ".html", ".js"), False, "catalog.css"),
        Kind("card_hooks", (".html",), True, "card-hooks.html"),
    ]
    return {item.id: item for item in listed}


def kind_limits(base: CodeLimits, kind: Kind) -> CodeLimits:
    """Пределы проекта вида: общие числа, расширения и «один файл» — вида."""
    return CodeLimits(
        base.max_files,
        base.file_max_bytes,
        base.project_max_bytes,
        kind.extensions,
        kind.single_file,
    )


def web_name_error(name: str) -> str | None:
    """Имя веб-вида — свободный текст (§7.5.16): 1…64 символа после обрезки пробелов."""
    stripped = name.strip()
    if not stripped:
        return "required"
    if len(stripped) > NAME_MAX_CHARS:
        return "too_long"
    if any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in stripped):
        return "invalid_format"
    return None


def default_catalog(path: str) -> tuple[str, list[str]]:
    """Место вставки и клиенты нового файла кастомизации по расширению (§7.5.16)."""
    insertion = "head_end" if path.lower().endswith(".css") else "body_end"
    return insertion, list(CLIENTS)


_FILE_TYPES = {".css": "CSS", ".html": "HTML", ".js": "JavaScript"}
_INSERTION_WORDS = {
    "head_start": "начало <head>",
    "head_end": "конец <head>",
    "body_start": "начало <body>",
    "body_end": "конец <body>",
}
_CLIENT_WORDS = {"smartphone": "смартфон", "tablet": "планшет", "desktop": "настольный ПК"}
MEMO_NAME = "Памятка.txt"


def _customization(path: str, insertion: str, clients: Sequence[str]) -> str:
    """Блок кастомизации Каталога для шага 2 памятки (portal-ui.md §5.13.22)."""
    extension = path[path.rfind(".") :].lower()
    chosen = (
        "все"
        if set(clients) == set(CLIENTS)
        else ", ".join(_CLIENT_WORDS[item] for item in CLIENTS if item in clients)
    )
    return "\n".join(
        [
            f"   {path}",
            f"     Тип: {_FILE_TYPES.get(extension, extension)}",
            f"     Место вставки: {_INSERTION_WORDS[insertion]}",
            f"     Тип клиента: {chosen}",
            f"     Код: всё содержимое файла {path}",
        ]
    )


def web_memo(template: str, files: Sequence[tuple[str, Mapping[str, Any] | None]]) -> str:
    """Текст `Памятка.txt` веб-вида с подстановками (§7.5.16); `files` — по порядку путей."""
    listing = [f"  {path} — код" for path, _ in files] + [f"  {MEMO_NAME} — эта памятка"]
    customizations = "\n\n".join(
        _customization(path, catalog["insertion"], catalog["clients"])
        for path, catalog in files
        if catalog is not None
    )
    values = {
        "{files}": "\n".join(listing),
        "{main_file}": files[0][0] if files else "",
        "{customizations}": customizations,
    }
    text = template
    for key, value in values.items():
        text = text.replace(key, value)
    return text
