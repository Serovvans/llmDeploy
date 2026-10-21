"""Отказы диалогов и вложений (docs/portal-api.md §5.2, §5.3, §5.8, §1.5)."""

from uuid import UUID

from portal.core.errors import AppError
from portal.llm.ports import MAX_IMAGES_PER_REQUEST


def generation_in_progress() -> AppError:
    """В диалоге ещё формируется ответ."""
    return AppError(409, "generation_in_progress", "В этом диалоге ещё формируется ответ.")


def nothing_to_regenerate() -> AppError:
    """В диалоге нет сообщений."""
    return AppError(409, "nothing_to_regenerate", "В диалоге нет вопроса, на который отвечать.")


def nothing_to_export() -> AppError:
    """В диалоге нечего экспортировать."""
    return AppError(409, "nothing_to_export", "В диалоге пока нечего сохранять в файл.")


def too_many_images() -> AppError:
    """Изображений и страниц-сканов больше, чем принимает модель."""
    return AppError(
        422,
        "too_many_images",
        f"В одном сообщении не больше {MAX_IMAGES_PER_REQUEST} изображений и страниц сканов.",
        details={"max_images": MAX_IMAGES_PER_REQUEST},
    )


def message_too_long() -> AppError:
    """Сообщение с вложениями не помещается в запрос к модели."""
    return AppError(
        422, "message_too_long", "Сообщение слишком длинное. Сократите текст или вложения."
    )


def attachment_not_ready(attachment_id: UUID) -> AppError:
    """Среди вложений вопроса есть запись без готовой расшифровки (§5.3)."""
    return AppError(
        409,
        "attachment_not_ready",
        "Расшифровка записи не готова: дождитесь её или уберите запись из сообщения.",
        details={"attachment_id": str(attachment_id)},
    )


def wrong_dialog_kind() -> AppError:
    """Действие не для диалога этого вида."""
    return AppError(409, "wrong_dialog_kind", "Вложения можно добавлять только в чате.")


def attachment_already_sent() -> AppError:
    """Вложение уже отправлено с сообщением."""
    return AppError(
        409, "attachment_already_sent", "Вложение уже отправлено: удалить его можно с чатом."
    )


def draft_in_progress() -> AppError:
    """У сотрудника уже идёт черновик пересказа (§5.10.8)."""
    return AppError(
        429, "draft_in_progress", "Пересказ уже готовится. Дождитесь его или закройте и повторите."
    )


def nothing_to_summarize() -> AppError:
    """В диапазоне пересказа нечего пересказывать (§5.10.5)."""
    return AppError(409, "nothing_to_summarize", "Пересказывать пока нечего.")
