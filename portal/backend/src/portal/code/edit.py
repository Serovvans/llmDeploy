"""Правка модели: блоки «файл целиком», разбор и замена в истории (§7.5.9, §7.5.10).

Ответ модели — недоверенный текст: из него берутся только пути и содержимое файлов, и
каждый путь проходит те же правила, что у сотрудника (§7.5.3). Чистые функции.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from portal.code.domain import CodeLimits, EditFile, ParsedEdit, ProjectFile, ProjectView
from portal.code.paths import conflicts, limit_exceeded, path_error, utf8_size

_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`]*)(.*)$")
_PATH = re.compile(r'(?:^|\s)path=(?:"([^"\n]*)"|(\S+))')
_CSHARP = frozenset({"csharp", "cs", "c#"})
# Веб-виды (§7.5.16): принимается ли файл, решает расширение пути, а не слово ограждения.
_ALL_LANGUAGES = _CSHARP | {"javascript", "js", "html", "css"}
_REJECTED_PATH_CHARS = 300
_PATH_REASONS = {
    "outside_project": "outside_project",
    "invalid_format": "invalid_path",
    "extension_not_allowed": "extension_not_allowed",
}
HISTORY_LINE = "(файл {path} — текст правки опущен; текущее состояние файлов — в блоке «проект»)"


@dataclass(frozen=True)
class EditBlock:
    """Блок правки в тексте ответа: строки `[start, end)` вместе с ограждениями."""

    path: str
    content: str
    closed: bool
    start: int
    end: int


def _closes(line: str, fence: str) -> bool:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return False
    body = stripped.rstrip(" \t")
    return len(body) >= len(fence) and set(body) == {fence[0]}


def edit_languages(kind: str) -> frozenset[str]:
    """Слова ограждения блока правки: у `gptool` — C#, у веб-видов — ещё JS, HTML и CSS."""
    return _CSHARP if kind == "gptool" else _ALL_LANGUAGES


def edit_blocks(text: str, languages: frozenset[str] = _ALL_LANGUAGES) -> Iterator[EditBlock]:
    """Блоки правки ответа по порядку: язык из `languages` и `path=` в строке ограждения.

    Блоки — по правилу §7.1 (шаги 1, 3–5): блок открывает строка с не более чем тремя
    пробелами и ограждением из трёх и более `` ` `` или `~`, закрывает — строка из знаков
    того же вида не короче открывающей; незакрытый блок длится до конца текста.
    """
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        opening = _OPEN.match(lines[index])
        if opening is None:
            index += 1
            continue
        fence, language, rest = opening.groups()
        end = index + 1
        while end < len(lines) and not _closes(lines[end], fence):
            end += 1
        closed = end < len(lines)
        found = _PATH.search(rest)
        if language.lower() in languages and found is not None:
            body = "\n".join(lines[index + 1 : end])
            content = f"{body}\n" if body else ""
            path = found.group(1) if found.group(1) is not None else found.group(2)
            yield EditBlock(path, content, closed, index, end + 1 if closed else end)
        index = end + 1


def history_text(answer: str) -> str:
    """Ответ для истории: каждый блок правки, закрытый или нет, заменён строкой (§7.5.9).

    Вид проекта здесь неизвестен: заменяются блоки с любым словом ограждения правки.
    """
    lines = answer.split("\n")
    out: list[str] = []
    position = 0
    for block in edit_blocks(answer):
        out.extend(lines[position : block.start])
        out.append(HISTORY_LINE.format(path=block.path.replace("\n", " ")))
        position = block.end
    out.extend(lines[position:])
    return "\n".join(out)


def normalize_path(raw: str) -> str:
    """Путь из ответа модели: обратная косая — в `/`, начальное `./` снимается (§7.5.10)."""
    path = raw.replace("\\", "/")
    return path[2:] if path.startswith("./") else path


def same_content(left: str, right: str) -> bool:
    """Содержимое совпадает с точностью до перевода строки в конце файла."""
    return left.rstrip("\n") == right.rstrip("\n")


def parse_edit(
    answer: str,
    project: ProjectView,
    limits: CodeLimits,
    finish: Literal["complete", "length_limit"],
) -> ParsedEdit:
    """Разобрать правку относительно файлов `project` (§7.5.10).

    В правку идут только закрытые блоки; один путь несколько раз — действует последний
    блок. Изменённый файл получает путь и ревизию файла проекта, новый — без ревизии;
    содержимое, равное текущему, не входит. Пределы проверяются по проекту с уже
    принятыми в разбор файлами, по порядку ответа.
    """
    latest: dict[str, tuple[str, EditBlock]] = {}
    for block in edit_blocks(answer, edit_languages(project.kind)):
        if block.closed:
            path = normalize_path(block.path)
            latest.pop(path.lower(), None)
            latest[path.lower()] = (path, block)
    by_path = {file.path.lower(): file for file in project.files}
    count = len(project.files)
    size = sum(file.size_bytes for file in project.files)
    accepted: list[EditFile] = []
    rejected: list[dict[str, str]] = []
    others = [file.path for file in project.files]
    for path, block in latest.values():
        reason = _reason(path, block.content, by_path, others, count, size, limits)
        if reason is not None:
            rejected.append({"path": block.path[:_REJECTED_PATH_CHARS], "reason": reason})
            continue
        current = by_path.get(path.lower())
        if current is not None:
            if same_content(current.content, block.content):
                continue
            size += utf8_size(block.content) - current.size_bytes
            accepted.append(EditFile(current.path, "modified", block.content, current.revision))
        else:
            count += 1
            size += utf8_size(block.content)
            others.append(path)
            accepted.append(EditFile(path, "added", block.content, None))
    return ParsedEdit(accepted, rejected, finish == "length_limit")


def _reason(
    path: str,
    content: str,
    by_path: dict[str, ProjectFile],
    others: Sequence[str],
    count: int,
    size: int,
    limits: CodeLimits,
) -> str | None:
    """Причина отказа файла правки по правилам §7.5.3; `None` — файл годен."""
    error = path_error(path, limits.extensions)
    if error is not None:
        return _PATH_REASONS[error]
    current = by_path.get(path.lower())
    if current is None and limits.single_file:
        return "single_file"
    # U+0000 файл проекта хранить не может (§7.5.3); своей причины у него в контракте нет.
    if not content.strip() or "\x00" in content:
        return "empty_file"
    if utf8_size(content) > limits.file_max_bytes:
        return "file_too_large"
    if current is None and conflicts(path, others):
        return "path_conflict"
    current_size = 0 if current is None else current.size_bytes
    files = count if current is not None else count + 1
    if limit_exceeded(files, size - current_size + utf8_size(content), limits) is not None:
        return "project_limit"
    return None
