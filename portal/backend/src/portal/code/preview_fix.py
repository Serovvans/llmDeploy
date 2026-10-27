"""«Исправить с помощником» по ошибкам предпросмотра (docs/portal-api.md §7.5.16, А-148).

Текст вопроса собирает сервер: при слое из образца (`layer: "sample"`) запрос структурно
не может нести текст ошибки и адрес, в которых могли оказаться значения строк базы
заказчика, — только тип ошибки из закрытого списка, путь файла проекта, строку в пределах
файла и имя директивы CSP.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from portal.core.errors import AppError, field_error, validation_error

FIELD = "preview_fix"
# Имена ошибок JavaScript; любое другое имя интерфейс передаёт как `Error`.
ERROR_TYPES = frozenset(
    {
        "Error",
        "AggregateError",
        "EvalError",
        "InternalError",
        "RangeError",
        "ReferenceError",
        "SyntaxError",
        "TypeError",
        "URIError",
        "DOMException",
    }
)
# Директивы, которые бывают в `effectiveDirective` на странице предпросмотра (§7.5.17,
# «Директивы»): из заголовка `/code-preview/*` в Caddyfile и уточняющие `-elem`/`-attr`.
DIRECTIVES = frozenset(
    {
        "default-src",
        "script-src",
        "script-src-elem",
        "script-src-attr",
        "style-src",
        "style-src-elem",
        "style-src-attr",
        "img-src",
        "font-src",
        "connect-src",
        "media-src",
        "object-src",
        "frame-src",
        "worker-src",
        "manifest-src",
        "form-action",
        "base-uri",
    }
)
_TEXT_MAX = 2000
_SHOWN = 10
_COMMON_KEYS = frozenset({"kind", "path", "line"})
_KIND_KEYS = {"error": frozenset({"type"}), "network": frozenset({"directive"})}
_PLACEHOLDER = re.compile(r"\{(path|line|what|target|n)\}")

Layer = Literal["invented", "sample"]


@dataclass(frozen=True)
class PreviewProblem:
    """Запись журнала предпросмотра после проверки."""

    kind: Literal["error", "network"]
    path: str | None
    line: int | None
    type: str | None
    directive: str | None
    text: str | None


@dataclass(frozen=True)
class PreviewFix:
    """Поле `preview_fix` после проверки."""

    layer: Layer
    problems: tuple[PreviewProblem, ...]


class PreviewFixTexts(Protocol):
    """Тексты `code.preview_fix.*` (§13.6; слова — ПИ §5.13.20)."""

    @property
    def header(self) -> str:
        """Заголовок при придуманном слое."""
        ...

    @property
    def header_sample(self) -> str:
        """Заголовок при слое из образца."""
        ...

    @property
    def item(self) -> str:
        """Строка с файлом и номером строки."""
        ...

    @property
    def item_no_line(self) -> str:
        """Строка с файлом без номера строки."""
        ...

    @property
    def item_no_place(self) -> str:
        """Строка без места."""
        ...

    @property
    def network(self) -> str:
        """Запрет сети."""
        ...

    @property
    def more(self) -> str:
        """«И ещё N»."""
        ...


def _fail(code: str) -> AppError:
    return validation_error([field_error(FIELD, code)])


def _problem(raw: Any, layer: Layer, lines: Mapping[str, int]) -> PreviewProblem:
    if not isinstance(raw, dict):
        raise _fail("invalid_format")
    kind = raw.get("kind")
    if kind not in _KIND_KEYS:
        raise _fail("invalid_format")
    allowed = _COMMON_KEYS | _KIND_KEYS[kind] | ({"text"} if layer == "invented" else set())
    if set(raw) - allowed:
        raise _fail("invalid_format")
    path, line = raw.get("path"), raw.get("line")
    if path is not None and (not isinstance(path, str) or path not in lines):
        raise _fail("unknown_value")
    if line is not None and (
        path is None
        or not isinstance(line, int)
        or isinstance(line, bool)
        or not 1 <= line <= lines[path]
    ):
        raise _fail("invalid_format")
    error_type = directive = None
    if kind == "error":
        error_type = raw.get("type")
        if error_type not in ERROR_TYPES:
            raise _fail("unknown_value")
    else:
        directive = raw.get("directive")
        if directive not in DIRECTIVES:
            raise _fail("unknown_value")
    text = None
    if layer == "invented":
        text = raw.get("text")
        if not isinstance(text, str) or not text:
            raise _fail("required")
        if len(text) > _TEXT_MAX:
            raise _fail("too_long")
    return PreviewProblem(kind, path, line, error_type, directive, text)


def parse_preview_fix(raw: Any, lines: Mapping[str, int], max_problems: int) -> PreviewFix:
    """Проверить `preview_fix` (§7.5.16); `lines` — путь файла проекта → число его строк.

    Ошибки — `validation_error` по полю `preview_fix`.
    """
    if not isinstance(raw, dict) or set(raw) - {"layer", "problems"}:
        raise _fail("invalid_format")
    layer = raw.get("layer")
    if layer not in ("invented", "sample"):
        raise _fail("invalid_format")
    problems = raw.get("problems")
    if problems is None or problems == []:
        raise _fail("required")
    if not isinstance(problems, list):
        raise _fail("invalid_format")
    if len(problems) > max_problems:
        raise _fail("too_many")
    return PreviewFix(layer, tuple(_problem(item, layer, lines) for item in problems))


def _fill(template: str, values: Mapping[str, str]) -> str:
    """Подстановки за один проход: подставленный текст повторно не разбирается."""
    return _PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), template)


def preview_fix_text(fix: PreviewFix, texts: PreviewFixTexts) -> str:
    """Текст вопроса: заголовок, до десяти строк ошибок и «и ещё N», по одной на строке."""
    invented = fix.layer == "invented"
    result = [texts.header if invented else texts.header_sample]
    for problem in fix.problems[:_SHOWN]:
        if problem.kind == "error":
            what = (problem.text if invented else problem.type) or ""
        else:
            target = (problem.text if invented else problem.directive) or ""
            what = _fill(texts.network, {"target": target})
        if problem.path is None:
            result.append(_fill(texts.item_no_place, {"what": what}))
        elif problem.line is None:
            result.append(_fill(texts.item_no_line, {"path": problem.path, "what": what}))
        else:
            values = {"path": problem.path, "line": str(problem.line), "what": what}
            result.append(_fill(texts.item, values))
    if len(fix.problems) > _SHOWN:
        result.append(_fill(texts.more, {"n": str(len(fix.problems) - _SHOWN)}))
    return "\n".join(result)
