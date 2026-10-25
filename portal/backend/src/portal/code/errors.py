"""Отказы раздела «Код» (docs/portal-api.md §7.5.2)."""

from collections.abc import Sequence
from typing import Literal

from portal.core.errors import AppError


def project_name_taken() -> AppError:
    """У сотрудника уже есть проект с этим именем без учёта регистра."""
    return AppError(409, "project_name_taken", "Проект с таким именем уже есть.")


def project_limit_reached(max_projects: int) -> AppError:
    """Проектов у сотрудника — `code.max_projects`."""
    return AppError(
        409,
        "project_limit_reached",
        "Достигнут предел числа проектов. Удалите ненужный проект.",
        details={"max": max_projects},
    )


def project_limit(limit: Literal["files", "bytes"], max_value: int) -> AppError:
    """Изменение превысило бы предел файлов или байт проекта."""
    return AppError(
        422,
        "project_limit",
        "Превышен предел размера проекта.",
        details={"limit": limit, "max": max_value},
    )


def file_exists(path: str) -> AppError:
    """Файл с этим путём без учёта регистра уже есть."""
    return AppError(
        409, "file_exists", "Файл с таким именем уже есть в проекте.", details={"path": path}
    )


def file_changed(revision: int) -> AppError:
    """Файл изменён в другом окне: присланная ревизия не текущая."""
    return AppError(
        409,
        "file_changed",
        "Файл изменён в другом окне. Обновите проект.",
        details={"revision": revision},
    )


def import_no_files() -> AppError:
    """В архиве не осталось файлов `.cs`."""
    return AppError(
        422, "import_no_files", "В архиве нет файлов .cs, которые можно взять в проект."
    )


def edit_decided() -> AppError:
    """Решение по правке уже принято или она заменена новой."""
    return AppError(409, "edit_decided", "По этой правке уже принято решение.")


def edit_outdated(paths: Sequence[str]) -> AppError:
    """Файлы изменились после того, как сотрудник открыл правку."""
    return AppError(
        409,
        "edit_outdated",
        "Файлы изменились, пока вы смотрели правку. Откройте её заново.",
        details={"paths": list(paths)},
    )


def build_in_progress() -> AppError:
    """У проекта уже идёт сборка кнопкой."""
    return AppError(429, "build_in_progress", "Сборка уже идёт. Дождитесь её окончания.")


def build_unavailable() -> AppError:
    """Песочница недоступна или не дождались места."""
    return AppError(503, "build_unavailable", "Сборка недоступна. Попробуйте позже.")


def archive_unavailable(
    reason: Literal["no_build", "not_built", "findings", "outdated"],
) -> AppError:
    """Архива нет: сборки нет, она не удалась, есть находки или она устарела."""
    return AppError(
        409,
        "archive_unavailable",
        "Архив доступен после успешной сборки без нарушений правил GPTool.",
        details={"reason": reason},
    )


def nothing_to_fix() -> AppError:
    """В последней сборке нет ни ошибок, ни находок правил."""
    return AppError(
        409,
        "nothing_to_fix",
        "Исправлять нечего: в последней сборке нет ошибок и нарушений правил.",
    )


def project_too_large() -> AppError:
    """Проект не помещается в запрос помощника целиком."""
    return AppError(
        422,
        "project_too_large",
        "Проект слишком велик для помощника. Ограничьте его открытыми файлами.",
    )
