"""Отказы диалогов и вложений (docs/portal-api.md §5.2, §5.3, §5.8, §1.5)."""

from portal.core.errors import AppError
from portal.llm.ports import MAX_IMAGES_PER_REQUEST


def generation_in_progress() -> AppError:
    """В диалоге ещё формируется ответ."""
    return AppError(409, "generation_in_progress", "В этом диалоге ещё формируется ответ.")


def nothing_to_regenerate() -> AppError:
    """В диалоге нет сообщений."""
    return AppError(409, "nothing_to_regenerate", "В диалоге нет вопроса, на который отвечать.")


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


def wrong_dialog_kind() -> AppError:
    """Действие не для диалога этого вида."""
    return AppError(409, "wrong_dialog_kind", "Вложения можно добавлять только в чате.")


def attachment_already_sent() -> AppError:
    """Вложение уже отправлено с сообщением."""
    return AppError(
        409, "attachment_already_sent", "Вложение уже отправлено: удалить его можно с чатом."
    )


def file_unreadable() -> AppError:
    """Файл не открывается."""
    return AppError(
        422, "file_unreadable", "Файл не удалось открыть: он повреждён или защищён паролем."
    )


def too_many_pages(max_pages: int) -> AppError:
    """В PDF больше страниц, чем разрешено."""
    return AppError(
        422,
        "too_many_pages",
        f"В документе больше {max_pages} страниц.",
        details={"max_pages": max_pages},
    )
