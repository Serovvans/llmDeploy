"""Мастер «Новый GPTool» и пример: проверка формы и каркас по шаблонам (§7.5.4).

Каркас строит код портала без модели: подстановки — проверенные идентификаторы и
строковые литералы C#; описание инструмента в код не попадает никогда. Чистые функции.
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from portal.code.paths import CSHARP_KEYWORDS, project_name_error
from portal.core.errors import FieldError, field_error, validation_error

PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
FACTORY_CLASS = "GpToolFactory"
# Базовый класс в `gptool/tool.cs`: `class GpTool : GpTool` не собирается (CS0146).
TOOL_BASE_CLASS = "GpTool"
CLASS_MAX_CHARS = 64
TITLE_MAX_CHARS = 200
_PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_ICON = re.compile(r"[a-z0-9 -]{1,100}")
_WORD = re.compile(r"[A-Za-z0-9]+")
# Как у тегов (А-63): разрыв строки и невидимое переупорядочивание в литерале C# — нет.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
# Члены класса инструмента в шаблонах, которые имя параметра не может повторить (§7.5.4).
TOOL_MEMBERS = ("ValidateInput", "InnerExecute")
DATABASE_MEMBERS = ("TargetWorkspace", "OpenConnection", "GetConnectionString", "WorkspaceName")


@dataclass(frozen=True)
class ParameterType:
    """Тип параметра мастера из `wizard.yaml`."""

    id: str
    title: str
    csharp: str
    using: str | None
    input: bool
    output: bool
    required_check: str | None


@dataclass(frozen=True)
class WizardTemplates:
    """Шаблоны мастера и примера, прочитанные при старте (§7.5.4)."""

    factory: str
    tool: str
    input: str
    required: str
    output: str
    validate: str
    database: str
    types: tuple[ParameterType, ...]
    required_message: str
    database_usings: tuple[str, ...]
    database_packages: tuple[str, ...]
    example_packages: tuple[str, ...]
    example_files: tuple[tuple[str, str], ...]

    def type(self, type_id: str) -> ParameterType | None:
        """Тип параметра по `id`."""
        return next((item for item in self.types if item.id == type_id), None)


@dataclass(frozen=True)
class Scaffold:
    """Каркас проекта: файлы, пакеты и текст запроса помощнику (`None` — описаний нет)."""

    files: list[tuple[str, str]]
    packages: list[str]
    assistant_request: str | None


def fill(template: str, values: Mapping[str, str]) -> str:
    """Подстановка `{{имя}}` в один проход: подставленное повторно не разбирается."""
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def fragment(text: str) -> str:
    """Файл-фрагмент без одного последнего перевода строки (§7.5.4)."""
    return text[:-1] if text.endswith("\n") else text


def literal(text: str) -> str:
    """Строковый литерал C# в кавычках: обратная косая и кавычка экранируются (§7.5.4)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _title_error(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "required"
    if len(value.strip()) > TITLE_MAX_CHARS:
        return "too_long"
    if any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in value.strip()):
        return "invalid_format"
    return None


def _check_parameters(
    prefix: str,
    tool: Mapping[str, Any],
    class_name: str,
    templates: WizardTemplates,
    problems: list[FieldError],
) -> None:
    members = [class_name, *TOOL_MEMBERS, *(DATABASE_MEMBERS if tool["uses_database"] else ())]
    reserved = {member.lower() for member in members}
    seen: set[str] = set()
    for group in ("inputs", "outputs"):
        for index, parameter in enumerate(tool[group]):
            where = f"{prefix}.{group}.{index}"
            name = parameter["name"]
            if (
                not _PARAMETER_NAME.fullmatch(name)
                or name in CSHARP_KEYWORDS
                or name.lower() in seen
                or name.lower() in reserved
            ):
                problems.append(field_error(f"{where}.name", "invalid_format"))
            seen.add(name.lower())
            kind = templates.type(parameter["type"])
            if kind is None or not (kind.input if group == "inputs" else kind.output):
                problems.append(field_error(f"{where}.type", "unknown_value"))
            for key in ("title_ru", "title_en"):
                if (code := _title_error(parameter[key])) is not None:
                    problems.append(field_error(f"{where}.{key}", code))


def check_form(
    form: Mapping[str, Any],
    templates: WizardTemplates,
    max_tools: int,
    max_parameters: int,
    description_max_chars: int,
) -> None:
    """Проверить форму мастера; нарушения — `validation_error` по полям (§7.5.4)."""
    problems: list[FieldError] = []
    if project_name_error(form["name"]) is not None:
        problems.append(field_error("name", "invalid_format"))
    tools = form["tools"]
    if not tools:
        problems.append(field_error("tools", "required"))
    elif len(tools) > max_tools:
        problems.append(field_error("tools", "too_long"))
    names = class_names([tool["title_en"] for tool in tools[:max_tools]])
    for index, (tool, class_name) in enumerate(zip(tools[:max_tools], names, strict=True)):
        prefix = f"tools.{index}"
        for key in ("title_ru", "title_en", "category"):
            if (code := _title_error(tool[key])) is not None:
                problems.append(field_error(f"{prefix}.{key}", code))
        if not _ICON.fullmatch(tool["icon"]):
            problems.append(field_error(f"{prefix}.icon", "invalid_format"))
        if len(tool["description"]) > description_max_chars:
            problems.append(field_error(f"{prefix}.description", "too_long"))
        if len(tool["inputs"]) + len(tool["outputs"]) > max_parameters:
            problems.append(field_error(f"{prefix}.inputs", "too_long"))
        _check_parameters(prefix, tool, class_name, templates, problems)
    if problems:
        raise validation_error(problems)


def class_names(titles: Sequence[str]) -> list[str]:
    """Имена классов инструментов из `title_en` (§7.5.4).

    Отрезки латинских букв и цифр с заглавной первой буквой, слитно, в конце `Tool`; пусто
    или начинается с цифры — `Tool<номер>`. Совпадение без учёта регистра с другим
    инструментом, с фабрикой, с `GpTool` или ключевым словом — суффикс `2`, `3`, …; до 64
    символов.
    """
    taken = {FACTORY_CLASS.lower(), TOOL_BASE_CLASS.lower(), *CSHARP_KEYWORDS}
    names: list[str] = []
    for number, title in enumerate(titles, start=1):
        base = "".join(word[0].upper() + word[1:] for word in _WORD.findall(title))
        if not base or base[0].isdigit():
            base = f"Tool{number}"
        elif not base.endswith("Tool"):
            base = f"{base}Tool"
        name = base[:CLASS_MAX_CHARS]
        suffix = 1
        while name.lower() in taken:
            suffix += 1
            name = f"{base[: CLASS_MAX_CHARS - len(str(suffix))]}{suffix}"
        taken.add(name.lower())
        names.append(name)
    return names


def _tool_file(
    namespace: str, class_name: str, tool: Mapping[str, Any], templates: WizardTemplates
) -> str:
    usings = {
        f"using {kind.using};"
        for parameter in [*tool["inputs"], *tool["outputs"]]
        if (kind := templates.type(parameter["type"])) is not None and kind.using
    }
    if tool["uses_database"]:
        usings |= {f"using {name};" for name in templates.database_usings}
    kinds = {item.id: item for item in templates.types}  # типы проверены check_form
    inputs, validate = [], []
    for parameter in tool["inputs"]:
        kind = kinds[parameter["type"]]
        inputs.append(
            fill(
                fragment(templates.input),
                {
                    "required": fragment(templates.required) if parameter["required"] else "",
                    "title_ru": literal(parameter["title_ru"].strip()),
                    "title_en": literal(parameter["title_en"].strip()),
                    "type": kind.csharp,
                    "name": parameter["name"],
                },
            )
        )
        if parameter["required"] and kind.required_check:
            message = templates.required_message.replace("{title}", parameter["title_ru"].strip())
            validate.append(
                fill(
                    fragment(templates.validate),
                    {
                        "condition": kind.required_check.replace("{name}", parameter["name"]),
                        "message": literal(message),
                    },
                )
            )
    outputs = []
    for parameter in tool["outputs"]:
        kind = kinds[parameter["type"]]
        outputs.append(
            fill(
                fragment(templates.output),
                {
                    "title_ru": literal(parameter["title_ru"].strip()),
                    "title_en": literal(parameter["title_en"].strip()),
                    "type": kind.csharp,
                    "name": parameter["name"],
                },
            )
        )
    return fill(
        templates.tool,
        {
            "namespace": namespace,
            "usings": "\n".join(sorted(usings)),
            "class_name": class_name,
            "icon": literal(tool["icon"]),
            "category": literal(tool["category"].strip()),
            "title_ru": literal(tool["title_ru"].strip()),
            "title_en": literal(tool["title_en"].strip()),
            "inputs": "\n".join(inputs),
            "outputs": "\n".join(outputs),
            "validate": "\n".join(validate),
            "database": fragment(templates.database) if tool["uses_database"] else "",
        },
    )


def scaffold(
    form: Mapping[str, Any],
    templates: WizardTemplates,
    wizard_request: str,
    package_order: Sequence[str],
) -> Scaffold:
    """Каркас мастера: фабрика и файл на инструмент; запрос помощнику по описаниям."""
    namespace = form["name"]
    tools = form["tools"]
    names = class_names([tool["title_en"] for tool in tools])
    files = [
        (
            f"{FACTORY_CLASS}.cs",
            fill(
                templates.factory,
                {
                    "namespace": namespace,
                    "tool_types": ", ".join(f"typeof({name})" for name in names),
                },
            ),
        )
    ]
    files.extend(
        (f"{name}.cs", _tool_file(namespace, name, tool, templates))
        for name, tool in zip(names, tools, strict=True)
    )
    packages: set[str] = set()
    if any(tool["uses_database"] for tool in tools):
        packages = {item.lower() for item in templates.database_packages}
    tasks = [
        f"«{tool['title_ru'].strip()}» ({name}): {tool['description'].strip()}"
        for name, tool in zip(names, tools, strict=True)
        if tool["description"].strip()
    ]
    request = wizard_request.strip().replace("{tasks}", "\n".join(tasks)) if tasks else None
    ordered = [item for item in package_order if item.lower() in packages]
    return Scaffold(files, ordered, request)


def example(name: str, templates: WizardTemplates, package_order: Sequence[str]) -> Scaffold:
    """Проект-пример: файлы `example/*.cs` с пространством имён `name` (§7.5.4)."""
    files = [(path, fill(text, {"namespace": name})) for path, text in templates.example_files]
    wanted = {item.lower() for item in templates.example_packages}
    return Scaffold(files, [item for item in package_order if item.lower() in wanted], None)
