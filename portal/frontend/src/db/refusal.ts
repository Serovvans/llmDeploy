/** Отказы подключения (концепция §5.7, таблица «Отказы подключения»; контракт API-49, API-50). */
import { isApiError } from '../api/client';
import { texts } from '../texts';

const f = texts.db.refusals;

/** Текст отказа подключения или `null`, если ошибка не про подключение. */
export function dbRefusal(error: unknown): string | null {
  if (isApiError(error, 'db_connection_unavailable') || isApiError(error, 'db_connection_not_found')) {
    return f.unavailable;
  }
  if (isApiError(error, 'db_connection_unusable')) {
    return f.unusable;
  }
  if (isApiError(error, 'db_samples_not_allowed')) {
    return f.samplesOff;
  }
  if (isApiError(error, 'db_unavailable')) {
    const reason = error.details.reason;
    return reason === 'timeout' ? f.timeout : reason === 'busy' ? f.busy : f.connect;
  }
  return null;
}
