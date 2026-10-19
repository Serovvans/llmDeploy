"""Общий формат ошибок API (docs/portal-api.md §1.3) без привязки к веб-фреймворку."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

_FIELD_MESSAGES = {
    "required": "Заполните поле.",
    "too_long": "Слишком длинное значение.",
    "too_many": "Слишком много значений.",
    "invalid_format": "Неверный формат.",
    "unknown_value": "Недопустимое значение.",
}


@dataclass(frozen=True)
class FieldError:
    """Ошибка одного поля запроса."""

    field: str
    code: str
    message: str


def field_error(field: str, code: str, message: str | None = None) -> FieldError:
    """Ошибка поля; для общих кодов текст подставляется сам."""
    return FieldError(field, code, message or _FIELD_MESSAGES[code])


class AppError(Exception):
    """Отказ, который превращается в ответ со статусом ≥ 400 в общем формате."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        fields: Sequence[FieldError] = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Запомнить статус, код причины, текст для пользователя и подробности."""
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.fields = tuple(fields)
        self.details = dict(details) if details is not None else None


def bad_request() -> AppError:
    """Тело запроса не разбирается."""
    return AppError(400, "bad_request", "Запрос не удалось разобрать.")


def unauthenticated() -> AppError:
    """Нет сессии или она истекла."""
    return AppError(401, "unauthenticated", "Сеанс завершён. Войдите снова.")


def login_step_expired() -> AppError:
    """Сессия незавершённого входа отсутствует или истекла."""
    return AppError(401, "login_step_expired", "Время на ввод кода вышло. Войдите снова.")


def csrf_check_failed() -> AppError:
    """У изменяющего запроса нет заголовка защиты от CSRF."""
    return AppError(403, "csrf_check_failed", "Запрос отклонён. Обновите страницу и повторите.")


def login_step_required(step: str) -> AppError:
    """Шаг входа не тот, которого требует маршрут."""
    return AppError(403, "login_step_required", "Сначала завершите вход.", details={"step": step})


def forbidden() -> AppError:
    """Не хватает роли или права на действие."""
    return AppError(403, "forbidden", "Недостаточно прав для этого действия.")


def feature_disabled() -> AppError:
    """Функция выключена флагом конфигурации (§13.6)."""
    return AppError(403, "feature_disabled", "Эта функция сейчас отключена.")


def not_found() -> AppError:
    """Ресурса нет или он чужой."""
    return AppError(404, "not_found", "Не найдено.")


def upload_timeout(reason: Literal["idle", "total"]) -> AppError:
    """Приём записи оборван по сроку: простой или общий срок приёма (§1.6)."""
    messages = {
        "idle": "Загрузка прервалась: данные перестали поступать. Повторите загрузку.",
        "total": (
            "Загрузка шла слишком долго и была прервана. Повторите её при более быстром соединении."
        ),
    }
    return AppError(408, "upload_timeout", messages[reason])


def length_required() -> AppError:
    """У загрузки записи нет заголовка `Content-Length` или он не число (§1.6)."""
    return AppError(411, "length_required", "Не указан размер загружаемого файла.")


def request_too_large(max_bytes: int) -> AppError:
    """Тело запроса без файла больше предела."""
    return AppError(
        413, "request_too_large", "Слишком большой запрос.", details={"max_bytes": max_bytes}
    )


def file_too_large(max_bytes: int) -> AppError:
    """Файл больше лимита области."""
    return AppError(
        413, "file_too_large", "Файл слишком большой.", details={"max_bytes": max_bytes}
    )


def unsupported_file_type() -> AppError:
    """Тип файла по содержимому не из разрешённых."""
    return AppError(415, "unsupported_file_type", "Файлы такого типа не принимаются.")


def file_unreadable() -> AppError:
    """Файл не открывается (§1.5)."""
    return AppError(
        422, "file_unreadable", "Файл не удалось открыть: он повреждён или защищён паролем."
    )


def docx_too_large() -> AppError:
    """В DOCX разметки или прочего содержимого, кроме картинок, больше предела (§1.5)."""
    return AppError(
        422,
        "docx_too_large",
        "В документе Word слишком много содержимого помимо картинок. Разделите его на части.",
    )


def docx_images_too_large() -> AppError:
    """Картинки DOCX вместе больше предела (§1.5)."""
    return AppError(
        422,
        "docx_images_too_large",
        "Картинки в документе Word занимают слишком много места. "
        "Уменьшите их или разделите документ на части.",
    )


def too_many_pages(max_pages: int) -> AppError:
    """В PDF больше страниц, чем разрешено его области (§1.5)."""
    return AppError(
        422,
        "too_many_pages",
        f"В документе больше {max_pages} страниц.",
        details={"max_pages": max_pages},
    )


def validation_error(fields: Sequence[FieldError]) -> AppError:
    """Ошибка в полях запроса."""
    return AppError(422, "validation_error", "Проверьте заполнение полей.", fields=fields)


def too_many_uploads() -> AppError:
    """Мест загрузки записей нет либо у сотрудника уже идёт загрузка записи (§1.6)."""
    return AppError(
        429, "too_many_uploads", "Сейчас загружаются другие записи. Повторите загрузку позже."
    )


def internal_error() -> AppError:
    """Необработанный сбой."""
    return AppError(500, "internal_error", "Внутренняя ошибка. Повторите попытку позже.")


def service_unavailable() -> AppError:
    """Недоступна база или иная внутренняя служба."""
    return AppError(
        503, "service_unavailable", "Сервис временно недоступен. Повторите попытку позже."
    )


def media_quota_exceeded(reason: Literal["total", "user", "free_space"]) -> AppError:
    """Квота записей или свободное место тома исчерпаны (§1.6); размеры не раскрываются."""
    message = (
        "Место под ваши записи закончилось: освободите место, удалив свои записи."
        if reason == "user"
        else "Место под записи закончилось, обратитесь к администратору."
    )
    return AppError(507, "media_quota_exceeded", message, details={"reason": reason})


def storage_full() -> AppError:
    """На томе файлов меньше порога свободного места (§8.1); размеры не раскрываются."""
    return AppError(
        507,
        "storage_full",
        "На сервере заканчивается место для файлов. Сообщите администратору.",
    )
