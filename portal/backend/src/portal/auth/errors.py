"""Отказы входа и администрирования (docs/portal-api.md §2.4, §2.5, §3)."""

from portal.core.errors import AppError


def invalid_credentials() -> AppError:
    """Неизвестный логин или неверный пароль — ответ одинаковый."""
    return AppError(401, "invalid_credentials", "Неверный логин или пароль.")


def account_blocked() -> AppError:
    """Учётная запись заблокирована (сообщается только при верном пароле)."""
    return AppError(
        403, "account_blocked", "Учётная запись заблокирована. Обратитесь к администратору."
    )


def login_locked(retry_after_seconds: int) -> AppError:
    """Вход для логина временно закрыт."""
    return AppError(
        429,
        "login_locked",
        "Слишком много неудачных попыток. Попробуйте позже.",
        details={"retry_after_seconds": retry_after_seconds},
    )


def too_many_attempts(retry_after_seconds: int) -> AppError:
    """Слишком много неудачных попыток с адреса."""
    return AppError(
        429,
        "too_many_attempts",
        "Слишком много попыток входа с вашего адреса. Попробуйте позже.",
        details={"retry_after_seconds": retry_after_seconds},
    )


def invalid_code() -> AppError:
    """Код из приложения не подошёл."""
    return AppError(422, "invalid_code", "Неверный код.")


def code_already_used() -> AppError:
    """Код этого шага времени уже принят."""
    return AppError(422, "code_already_used", "Этот код уже использован. Дождитесь нового.")


def invalid_backup_code() -> AppError:
    """Резервный код неверен или уже использован."""
    return AppError(422, "invalid_backup_code", "Резервный код не подошёл.")


def setup_not_started() -> AppError:
    """Подтверждение второго фактора без начатой настройки."""
    return AppError(409, "setup_not_started", "Сначала начните настройку второго фактора.")


def login_taken() -> AppError:
    """Логин уже занят."""
    return AppError(409, "login_taken", "Пользователь с таким логином уже есть.")


def cannot_modify_self() -> AppError:
    """Действие над собственной учётной записью."""
    return AppError(
        409, "cannot_modify_self", "Это действие нельзя выполнить над своей учётной записью."
    )
