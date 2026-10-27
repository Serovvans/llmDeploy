"""Отказы подключений к базам заказчика (docs/portal-api.md §7.6.4, §7.5.18)."""

from datetime import UTC, datetime
from typing import Any

from portal.core.errors import AppError


def _time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def title_taken() -> AppError:
    """Название подключения занято без учёта регистра."""
    return AppError(409, "db_connection_title_taken", "Подключение с таким названием уже есть.")


def connection_unusable() -> AppError:
    """У роли подключения лишние права: к базе портал не обращается."""
    return AppError(
        409,
        "db_connection_unusable",
        "Подключение не используется: у учётной записи базы лишние права. "
        "Сообщите администратору портала.",
    )


def connection_unavailable() -> AppError:
    """Подключения выбора нет или к нему закрыт доступ."""
    return AppError(
        409,
        "db_connection_unavailable",
        "Подключение недоступно: его удалили или закрыли к нему доступ.",
    )


def connection_not_found() -> AppError:
    """Новый выбор называет недоступное подключение."""
    return AppError(422, "db_connection_not_found", "Подключение не найдено.")


def snapshot_missing() -> AppError:
    """Снимка схемы ещё нет."""
    return AppError(409, "db_snapshot_missing", "Схема базы ещё не загружена.")


def snapshot_too_soon(available_at: datetime) -> AppError:
    """Снимок обновляли недавно."""
    return AppError(
        429,
        "db_snapshot_too_soon",
        "Схему недавно обновляли. Попробуйте позже.",
        details={"available_at": _time(available_at)},
    )


def db_unavailable(reason: str, details: dict[str, Any] | None = None) -> AppError:
    """База не ответила или отказала (§7.6.4)."""
    return AppError(
        503,
        "db_unavailable",
        "База данных не ответила. Попробуйте позже.",
        details={"reason": reason, **(details or {})},
    )


def samples_not_allowed() -> AppError:
    """Образцы строк этой таблицы не разрешены (§7.5.18)."""
    return AppError(409, "db_samples_not_allowed", "Образцы строк этой таблицы не разрешены.")


def schema_required() -> AppError:
    """У проекта нет схемы и выбора подключения (§7.5.18)."""
    return AppError(422, "schema_required", "Выберите схему базы в настройках проекта.")


def sample_in_progress() -> AppError:
    """У сотрудника уже идёт загрузка слоя предпросмотра из образца (§7.5.18)."""
    return AppError(429, "db_sample_in_progress", "Образец уже загружается. Дождитесь окончания.")
