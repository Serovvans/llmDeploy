import { ApiError, isApiError, NetworkError } from '../api/client';
import { errorText, texts } from '../texts';

function retrySeconds(error: ApiError): number | null {
  const seconds = error.details.retry_after_seconds;
  return typeof seconds === 'number' ? seconds : null;
}

/** Текст временной блокировки логина (`login_locked`) со сроком из `retry_after_seconds`. */
export function loginLockedText(error: ApiError): string {
  const seconds = retrySeconds(error);
  return seconds === null ? errorText(error) : texts.login.loginLocked(seconds);
}

/** Текст ограничения попыток с адреса (`too_many_attempts`). */
export function tooManyAttemptsText(error: ApiError): string {
  const seconds = retrySeconds(error);
  return seconds === null ? errorText(error) : texts.login.tooManyAttempts(seconds);
}

/** Текст заметки над полями экрана входа (концепция §5.1). */
export function loginErrorText(error: unknown): string {
  if (error instanceof NetworkError) {
    return texts.login.noConnection;
  }
  if (isApiError(error, 'invalid_credentials')) {
    return texts.login.invalidCredentials;
  }
  if (isApiError(error, 'login_locked')) {
    return loginLockedText(error);
  }
  if (isApiError(error, 'too_many_attempts')) {
    return tooManyAttemptsText(error);
  }
  if (isApiError(error, 'account_blocked')) {
    return texts.login.accountBlocked;
  }
  return errorText(error);
}

export interface PasswordErrors {
  current: string | null;
  next: string | null;
  repeat: string | null;
}

export const NO_PASSWORD_ERRORS: PasswordErrors = { current: null, next: null, repeat: null };

/** Пределы длины пароля из `GET /api/config`; `null`, пока конфигурация не получена. */
export type PasswordLimits = { min_length: number; max_length: number } | null;

/**
 * Проверки смены пароля, которые делает интерфейс до отправки (концепция §5.3):
 * пустые поля, предел длины и несовпавший повтор.
 */
export function checkNewPassword(next: string, repeat: string, limits: PasswordLimits): PasswordErrors {
  const errors = { ...NO_PASSWORD_ERRORS };
  if (!next) {
    errors.next = texts.password.enterNew;
  } else if (limits && next.length > limits.max_length) {
    errors.next = texts.password.tooLong(limits.max_length);
  }
  if (!repeat) {
    errors.repeat = texts.password.enterRepeat;
  } else if (next && next !== repeat) {
    errors.repeat = texts.password.mismatch;
  }
  return errors;
}

const PASSWORD_ERRORS: Record<string, string> = {
  password_too_common: texts.password.tooCommon,
  password_same_as_old: texts.password.sameAsOld,
  current_password_invalid: texts.password.currentInvalid,
};

/** Ошибки полей из отказа `validation_error` смены пароля (концепция §5.3, §5.11); `null` — отказ другой. */
export function passwordErrorsFrom(error: unknown, limits: PasswordLimits): PasswordErrors | null {
  if (!isApiError(error, 'validation_error') || error.fields.length === 0) {
    return null;
  }
  const errors = { ...NO_PASSWORD_ERRORS };
  for (const { field, code, message } of error.fields) {
    let text = PASSWORD_ERRORS[code] ?? message;
    if (limits && code === 'password_too_short') {
      text = texts.password.tooShort(limits.min_length);
    } else if (limits && code === 'too_long' && field === 'new_password') {
      text = texts.password.tooLong(limits.max_length);
    }
    if (field === 'current_password') {
      errors.current = text;
    } else {
      errors.next = text;
    }
  }
  return errors;
}
