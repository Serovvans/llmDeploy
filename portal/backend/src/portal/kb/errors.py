"""Отказы маршрутов базы знаний (docs/portal-api.md §8.1, §8.3, §1.5)."""

from collections.abc import Mapping
from typing import Any

from portal.core.errors import AppError


def duplicate_document(details: Mapping[str, Any]) -> AppError:
    """Такой файл уже есть в этой коллекции; `details.document` называет его."""
    return AppError(
        409,
        "duplicate_document",
        "Такой документ уже есть в базе знаний.",
        details={"document": dict(details)},
    )


def document_not_in_error() -> AppError:
    """На повторную обработку отправляется только документ с ошибкой."""
    return AppError(
        409, "document_not_in_error", "Документ уже обрабатывается или обработан без ошибки."
    )


def document_not_ready() -> AppError:
    """Текст доступен только у обработанного документа."""
    return AppError(409, "document_not_ready", "Документ ещё не обработан.")


def source_unavailable() -> AppError:
    """Запись нечем обработать: нет ни исходного файла, ни сжатого звука (§8.7)."""
    return AppError(
        409,
        "source_unavailable",
        "Исходный файл записи уже удалён. Загрузите запись заново.",
    )


def range_not_satisfiable() -> AppError:
    """Запрошенный диапазон байт лежит за пределами файла звука (§8.7)."""
    return AppError(416, "range_not_satisfiable", "Запрошенный диапазон вне файла.")
