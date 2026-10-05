"""Перевод ошибок разбора запроса в общий формат (docs/portal-api.md §1.3)."""

from collections.abc import Mapping, Sequence
from typing import Any

from portal.core import errors

_REQUIRED_TYPES = frozenset({"missing", "string_too_short", "too_short"})
_TOO_LONG_TYPES = frozenset({"string_too_long", "too_long"})
_UNKNOWN_VALUE_TYPES = frozenset({"literal_error", "enum"})


def _field_code(error_type: str) -> str:
    if error_type in _REQUIRED_TYPES:
        return "required"
    if error_type in _TOO_LONG_TYPES:
        return "too_long"
    if error_type in _UNKNOWN_VALUE_TYPES:
        return "unknown_value"
    return "invalid_format"


def validation_failure(
    problems: Sequence[Mapping[str, Any]], *, location_prefix: int = 0
) -> errors.AppError:
    """Ошибки pydantic → `validation_error`: по одной записи на поле.

    `location_prefix` — сколько первых элементов пути к полю отбросить (у ошибок
    фреймворка первым идёт «body» или «query»). Ошибка без имени поля — тело не объект
    или не JSON — даёт `bad_request`.
    """
    fields: dict[str, errors.FieldError] = {}
    for problem in problems:
        location = problem["loc"][location_prefix:]
        if problem["type"] == "json_invalid" or not location:
            return errors.bad_request()
        name = ".".join(str(part) for part in location)
        fields.setdefault(name, errors.field_error(name, _field_code(problem["type"])))
    return errors.validation_error(list(fields.values()))
