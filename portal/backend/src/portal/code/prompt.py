"""Сборка запроса помощника проекта — одна для ответа, памяти, цикла и набора (§7.5.9, §12.1).

Файлы проекта, сообщения компилятора, справочник и схема — недоверенные данные: они идут
только в сообщении роли `user` размеченными блоками `data_block`, в системном сообщении —
только тексты портала (`CLAUDE.md`, пайплайн «Код», правило 3). Чистые функции.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from portal.code.domain import (
    BuildView,
    CodeAction,
    ProjectView,
    ReferenceType,
    SchemaView,
    TargetView,
)
from portal.core.settings import CodePrompts
from portal.dialogs.context import data_block
from portal.llm.ports import ModelMessage, TextPart, TokenEstimator

# Первая строка запроса попытки — ровно она: по ней запрос узнаёт заглушка (§12.2).
FIX_PROMPT = "Исправь ошибки сборки."
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_USING = re.compile(r"^\s*(?:global\s+)?using\s+(?!static\b)([A-Za-z_][A-Za-z0-9_.]*)\s*;", re.M)
_OUTCOME = {"succeeded": "собрано", "failed": "ошибки", "timeout": "не уложилась во время"}


@dataclass(frozen=True)
class CodePlan:
    """Запрос помощника на один вопрос (§7.5.9).

    `question` — сообщение пользователя целиком (блоки и текст); `without_project` — оно
    же без блока `<проект>`: по нему отличается `message_too_long` от `project_too_large`.
    """

    system: str
    question: str
    without_project: str


def _location(item: Mapping[str, Any], *, column: bool) -> str:
    """Префикс места `<файл>:<строка>[:<столбец>]: `; без файла — пусто."""
    if item.get("file") is None:
        return ""
    parts = [str(item["file"])]
    for key in ("line", "column") if column else ("line",):
        if item.get(key) is None:
            break
        parts.append(str(item[key]))
    return f"{':'.join(parts)}: "


def build_lines(build: BuildView, limit: int) -> list[str]:
    """Текст блока `<сборка>`: итог, затем ошибки, находки и предупреждения (§7.5.9)."""
    head = _OUTCOME[build.status]
    if build.outdated:
        head += "; выполнена до последних изменений файлов"
    errors = [item for item in build.diagnostics if item.get("severity") == "error"]
    warnings = [item for item in build.diagnostics if item.get("severity") == "warning"]
    messages = [
        *(
            f"{_location(item, column=True)}ошибка {item['code']}: {item['message']}"
            for item in errors
        ),
        *(
            f"{_location(item, column=False)}правило {item['rule']}: {item['message']}"
            for item in build.findings
        ),
        *(
            f"{_location(item, column=True)}предупреждение {item['code']}: {item['message']}"
            for item in warnings
        ),
    ]
    return [head, *messages[:limit]]


def project_block(project: ProjectView, sent: Sequence[str] | None) -> str:
    """Блок `<проект>`: отправленные файлы по порядку путей и строка о прочих (§7.5.9).

    `sent` — пути отправляемых файлов; `None` — все файлы проекта.
    """
    files = sorted(project.files, key=lambda file: file.path.lower())
    if not files:
        return data_block("проект", "(в проекте нет файлов)", project.name)
    wanted = None if sent is None else {path.lower() for path in sent}
    chosen = [file for file in files if wanted is None or file.path.lower() in wanted]
    rest = [file.path for file in files if wanted is not None and file.path.lower() not in wanted]
    parts = [data_block("файл", file.content, file.path) for file in chosen]
    if rest:
        parts.append(f"Не переданы: {', '.join(rest)}")
    return data_block("проект", "\n\n".join(parts), project.name)


def _type_text(item: ReferenceType) -> str:
    return "\n".join([item.signature, *(f"  {member.signature}" for member in item.members)])


def select_reference(
    types: Sequence[ReferenceType],
    always: Sequence[str],
    texts: Sequence[str],
    files: Sequence[str],
    max_tokens: int,
    estimator: TokenEstimator,
) -> str:
    """Часть справочника для помощника: только типы `cogis`, целиком (§7.5.14).

    Порядок: `always`; типы, чьё имя (у вложенного — последняя часть) встречается словом в
    `texts` (вопрос, выделение, файлы) — по первому вхождению; остальные типы пространств
    имён из `using` файлов. Не помещающийся в остаток `max_tokens` тип пропускается.
    """
    cogis = [item for item in types if item.origin == "cogis"]
    by_name = {item.full_name: item for item in cogis}
    order: list[ReferenceType] = [by_name[name] for name in always if name in by_name]
    first: dict[str, int] = {}
    offset = 0
    for text in texts:
        for match in _IDENTIFIER.finditer(text):
            first.setdefault(match.group(), offset + match.start())
        offset += len(text) + 1
    mentioned = [
        (first[item.name.rsplit(".", 1)[-1]], index)
        for index, item in enumerate(cogis)
        if item.name.rsplit(".", 1)[-1] in first
    ]
    order.extend(cogis[index] for _, index in sorted(mentioned))
    namespaces: list[str] = []
    for text in files:
        for match in _USING.finditer(text):
            if match.group(1) not in namespaces:
                namespaces.append(match.group(1))
    for namespace in namespaces:
        order.extend(item for item in cogis if item.namespace == namespace)
    lines: list[str] = []
    used = 0
    seen: set[str] = set()
    current: str | None = None
    for item in order:
        if item.full_name in seen:
            continue
        piece = _type_text(item)
        if item.namespace != current:
            piece = f"namespace {item.namespace}\n{piece}"
        cost = estimator.text(piece) + 1
        if used + cost > max_tokens:
            continue
        seen.add(item.full_name)
        lines.append(piece)
        used += cost
        current = item.namespace
    return "\n".join(lines)


def code_system(
    action: CodeAction,
    prompts: CodePrompts,
    *,
    reference: bool,
    documented: bool | None,
) -> str:
    """Системное сообщение помощника без правил головного сообщения и базы знаний."""
    parts = [getattr(prompts.system, action), prompts.gptool_rules]
    if action in ("write", "fix"):
        parts.append(prompts.edit_format)
    if not reference:
        parts.append(prompts.no_sdk_notice)
    if documented is False:
        parts.append(prompts.no_documentation_notice)
    return "\n\n".join(part.strip() for part in parts)


def web_system(
    action: CodeAction,
    kind: str,
    prompts: CodePrompts,
    api_types: str,
    *,
    documented: bool | None,
) -> str:
    """Системное сообщение помощника веб-вида (§7.5.16): без правил GPTool и справочника SDK.

    Описание `api` — текст портала (`api.d.ts`), а не данные пользователя.
    """
    web = prompts.web
    parts = [getattr(web.system, action), getattr(web.kinds, kind)]
    if action == "write":
        parts.append(web.edit_format)
    parts.append(data_block("описание_api", api_types))
    if documented is False:
        parts.append(prompts.no_documentation_notice)
    return "\n\n".join(part.strip() for part in parts)


def build_code_plan(
    action: CodeAction,
    content: str,
    project: ProjectView,
    sent: Sequence[str] | None,
    target: TargetView | None,
    build: BuildView | None,
    schema: SchemaView | None,
    reference: Sequence[ReferenceType] | None,
    documented: bool | None,
    prompts: CodePrompts,
    estimator: TokenEstimator,
    *,
    reference_always: Sequence[str],
    reference_max_tokens: int,
    build_messages_max: int,
    api_types: str = "",
) -> CodePlan:
    """Системное сообщение и вопрос помощника (§7.5.9).

    `sent` — пути файлов, которые уходят модели (`None` — все); файл выделения в них уже
    включён вызывающим. Найденное в документации добавляет к вопросу сценарий ответа. У
    веб-вида (`project.kind` не `gptool`) справочника и сборки нет, системное сообщение —
    `web_system` с описанием `api` (§7.5.16).
    """
    web = project.kind != "gptool"
    if web:
        reference, build = None, None
    wanted = None if sent is None else {path.lower() for path in sent}
    files = [file for file in project.files if wanted is None or file.path.lower() in wanted]
    blocks: list[str] = []
    if reference is not None:
        texts = [content, target.text if target else "", *(file.content for file in files)]
        chosen = select_reference(
            reference,
            reference_always,
            texts,
            [file.content for file in files],
            reference_max_tokens,
            estimator,
        )
        if chosen:
            blocks.append(data_block("справочник", chosen))
    if schema is not None:
        blocks.append(data_block("схема", schema.content, schema.name))
    project_at = len(blocks)
    blocks.append(project_block(project, sent))
    if build is not None:
        blocks.append(data_block("сборка", "\n".join(build_lines(build, build_messages_max))))
    if target is not None:
        name = f"{target.path}, строки {target.start_line}–{target.end_line}"
        blocks.append(data_block("выделение", target.text, name))
    blocks.append(content)
    if web:
        system = web_system(action, project.kind, prompts, api_types, documented=documented)
    else:
        system = code_system(
            action, prompts, reference=reference is not None, documented=documented
        )
    without = [*blocks[:project_at], *blocks[project_at + 1 :]]
    return CodePlan(system, "\n\n".join(blocks), "\n\n".join(without))


def fix_attempt_messages(
    project: ProjectView,
    build: BuildView,
    reference: Sequence[ReferenceType] | None,
    prompts: CodePrompts,
    estimator: TokenEstimator,
    *,
    reference_always: Sequence[str],
    reference_max_tokens: int,
    build_messages_max: int,
) -> list[ModelMessage]:
    """Запрос попытки цикла исправления (§12.1): без истории и без найденного в базе знаний."""
    system = "\n\n".join(
        part.strip()
        for part in (
            prompts.system.fix,
            prompts.gptool_rules,
            prompts.edit_format,
            prompts.fix_attempt,
        )
    )
    blocks = [FIX_PROMPT]
    if reference is not None:
        contents = [file.content for file in project.files]
        chosen = select_reference(
            reference, reference_always, contents, contents, reference_max_tokens, estimator
        )
        if chosen:
            blocks.append(data_block("справочник", chosen))
    blocks.append(project_block(project, None))
    blocks.append(data_block("сборка", "\n".join(build_lines(build, build_messages_max))))
    return [
        ModelMessage("system", [TextPart(system)]),
        ModelMessage("user", [TextPart("\n\n".join(blocks))]),
    ]


def messages_tokens(messages: Sequence[ModelMessage], estimator: TokenEstimator) -> int:
    """Оценка текста сообщений запроса."""
    return sum(
        estimator.text(part.text)
        for message in messages
        for part in message.parts
        if isinstance(part, TextPart)
    )
