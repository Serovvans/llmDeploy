"""Отказы инструментов (docs/portal-api.md §5.3, §7.1, §7.3)."""

from portal.core.errors import AppError


def schema_not_found() -> AppError:
    """Схемы нет среди схем пользователя."""
    return AppError(422, "schema_not_found", "Выбранная схема базы не найдена.")


def schema_name_taken() -> AppError:
    """У пользователя уже есть схема с таким названием."""
    return AppError(409, "schema_name_taken", "Схема с таким названием уже есть.")


def schema_limit_reached() -> AppError:
    """Сохранено предельное число схем."""
    return AppError(
        409, "schema_limit_reached", "Сохранено предельное число схем. Удалите ненужные."
    )


def document_too_long() -> AppError:
    """Текст документа не помещается в запрос к модели."""
    return AppError(
        422, "document_too_long", "Документ слишком длинный для разбора. Разделите его на части."
    )
