"""Чтение шаблонов мастера и примера при старте (docs/portal-api.md §7.5.4).

Нет файла, подстановка с неизвестным именем, повтор типа или пакет не из набора — сервис
не стартует (`ConfigError`).
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from portal.code.wizard import PLACEHOLDER, ParameterType, WizardTemplates
from portal.core.settings import ConfigError

_GPTOOL: dict[str, frozenset[str]] = {
    "factory.cs": frozenset({"namespace", "tool_types"}),
    "tool.cs": frozenset(
        {
            "namespace",
            "usings",
            "class_name",
            "icon",
            "category",
            "title_ru",
            "title_en",
            "inputs",
            "outputs",
            "validate",
            "database",
        }
    ),
    "input.cs": frozenset({"required", "title_ru", "title_en", "type", "name"}),
    "required.cs": frozenset(),
    "output.cs": frozenset({"title_ru", "title_en", "type", "name"}),
    "validate.cs": frozenset({"condition", "message"}),
    "database.cs": frozenset(),
}
_EXAMPLE = frozenset({"namespace"})


def _read(path: Path, allowed: frozenset[str]) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Шаблон раздела «Код» не прочитан: {path}") from error
    unknown = {match.group(1) for match in PLACEHOLDER.finditer(text)} - allowed
    if unknown:
        raise ConfigError(f"{path}: неизвестные подстановки {sorted(unknown)}")
    return text


def _packages(data: Any, key: str, known: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ConfigError(f"wizard.yaml: {key} — список строк")
    lower = {item.lower() for item in known}
    if any(item.lower() not in lower for item in data):
        raise ConfigError(f"wizard.yaml: {key} — пакет не из code.packages")
    return tuple(data)


def load_templates(directory: Path, packages: Sequence[str]) -> WizardTemplates:
    """Прочитать и проверить каталог `code.templates_dir`; `packages` — `id` офлайн-набора."""
    gptool = {
        name: _read(directory / "gptool" / name, allowed) for name, allowed in _GPTOOL.items()
    }
    try:
        raw = yaml.safe_load((directory / "gptool" / "wizard.yaml").read_text(encoding="utf-8"))
        types = tuple(
            ParameterType(
                id=str(item["id"]),
                title=str(item["title"]),
                csharp=str(item["csharp"]),
                using=None if item["using"] is None else str(item["using"]),
                input=bool(item["input"]),
                output=bool(item["output"]),
                required_check=None
                if item["required_check"] is None
                else str(item["required_check"]),
            )
            for item in raw["parameter_types"]
        )
        required_message = str(raw["required_message"])
        database, sample = raw["database"], raw["example"]
        usings = tuple(str(item) for item in database["usings"])
    except (OSError, UnicodeDecodeError, yaml.YAMLError, KeyError, TypeError) as error:
        raise ConfigError("code_templates/gptool/wizard.yaml: неверный формат") from error
    if len({item.id for item in types}) != len(types):
        raise ConfigError("wizard.yaml: id типов параметров повторяются")
    examples = sorted((directory / "example").rglob("*.cs"))
    if not examples:
        raise ConfigError("code_templates/example: нет файлов .cs")
    example_files = tuple(
        (path.relative_to(directory / "example").as_posix(), _read(path, _EXAMPLE))
        for path in examples
    )
    return WizardTemplates(
        factory=gptool["factory.cs"],
        tool=gptool["tool.cs"],
        input=gptool["input.cs"],
        required=gptool["required.cs"],
        output=gptool["output.cs"],
        validate=gptool["validate.cs"],
        database=gptool["database.cs"],
        types=types,
        required_message=required_message,
        database_usings=usings,
        database_packages=_packages(database["packages"], "database.packages", packages),
        example_packages=_packages(sample["packages"], "example.packages", packages),
        example_files=example_files,
    )
