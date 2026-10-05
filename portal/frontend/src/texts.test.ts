import { describe, expect, it } from 'vitest';

import { ApiError, NetworkError } from './api/client';
import { errorText, lockedUntil, retryAfter, texts } from './texts';

describe('тексты', () => {
  it('срок блокировки округляется вверх до минут и склоняется', () => {
    expect(retryAfter(1)).toBe('через минуту');
    expect(retryAfter(59)).toBe('через минуту');
    expect(retryAfter(60)).toBe('через минуту');
    expect(retryAfter(61)).toBe('через 2 минуты');
    expect(retryAfter(300)).toBe('через 5 минут');
    expect(retryAfter(11 * 60)).toBe('через 11 минут');
    expect(retryAfter(21 * 60)).toBe('через 21 минуту');
    expect(retryAfter(22 * 60)).toBe('через 22 минуты');
    expect(texts.login.loginLocked(300)).toBe(
      'Слишком много неудачных попыток. Попробуйте снова через 5 минут или попросите администратора портала снять блокировку',
    );
  });

  it('срок блокировки входа: сегодня — время, в другой день — дата и время', () => {
    const now = new Date(2026, 9, 5, 14, 0);
    expect(lockedUntil(new Date(2026, 9, 5, 14, 35).toISOString(), now)).toBe('до 14:35');
    expect(lockedUntil(new Date(2026, 9, 6, 9, 10).toISOString(), now)).toBe('до 06.10.2026, 09:10');
  });

  it('для отказа без своей формулировки выбирает общий текст', () => {
    expect(errorText(new NetworkError())).toBe(texts.common.noConnection);
    expect(errorText(new ApiError(500, { code: 'internal_error', message: 'Что-то' }))).toBe(texts.common.serverFailure);
    expect(errorText(new ApiError(503, { code: 'service_unavailable', message: '' }))).toBe(
      texts.common.serverFailure,
    );
    expect(errorText(new ApiError(409, { code: 'new_code', message: 'Текст сервера.' }))).toBe('Текст сервера.');
    expect(errorText(new ApiError(409, { code: 'new_code', message: '' }))).toBe(texts.common.actionFailed);
    expect(errorText(new Error('boom'))).toBe(texts.common.actionFailed);
  });
});
