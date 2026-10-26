/** Подключения к базам заказчика на стенде (docs/portal-api.md §7.6): тестовая база `customer-db`. */
import { expect, type APIRequestContext } from '@playwright/test';

import { uniqueSuffix } from './accounts';
import { customerDbLog } from './stand';

/** Имя приложения сеансов портала в журнале `customer-db` (`db.application_name`). */
export const PORTAL_APPLICATION = 'scancontrol-portal';

/** Пароль ролей входа тестовой базы: только из окружения прогона, в файлы не пишется. */
export function customerPassword(): string {
  const value = process.env.CUSTOMER_DB_PASSWORD;
  if (!value) {
    throw new Error('CUSTOMER_DB_PASSWORD не задан в окружении прогона: им поднят customer-db стенда');
  }
  return value;
}

export interface Exclusion {
  schema: string;
  table: string;
  column: string | null;
}

/** Накладка `portal-api` с включёнными подключениями; раздел `code:` — как в накладке стенда. */
export const DB_ON_YAML = 'db:\n  enabled: true\n';

/** Создаёт подключение к `customer-db` под ролью `username` и возвращает ответ целиком. */
export async function createConnection(
  admin: APIRequestContext,
  username: string,
  options: { samples?: boolean; exclusions?: Exclusion[]; userIds?: string[] } = {},
  // Ответ — AdminDbConnection (§7.6.1).
): Promise<any> { // eslint-disable-line @typescript-eslint/no-explicit-any
  const response = await admin.post('/api/admin/db-connections', {
    data: {
      title: `Кадастр ${username} ${uniqueSuffix()}`,
      description: 'Тестовая база стенда',
      host: 'customer-db',
      port: 5432,
      database: 'cadastre',
      username,
      password: customerPassword(),
      tls_mode: 'disable',
      schemas: ['cadastre', 'public'],
      samples_allowed: options.samples ?? true,
      sample_exclusions: options.exclusions ?? [],
      access: options.userIds ? { all: false, user_ids: options.userIds } : { all: true },
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  return response.json();
}

/** «Проверить подключение» администратора (§7.6.2). */
export async function checkConnection(admin: APIRequestContext, id: string): Promise<any> { // eslint-disable-line @typescript-eslint/no-explicit-any
  const response = await admin.post(`/api/admin/db-connections/${id}/check`, { timeout: 60_000 });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

/** «Обновить схему» (§7.6.3). */
export async function refreshSnapshot(api: APIRequestContext, id: string): Promise<any> { // eslint-disable-line @typescript-eslint/no-explicit-any
  const response = await api.post(`/api/db/connections/${id}/snapshot`, { timeout: 60_000 });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

/**
 * Операторы сеансов портала в журнале `customer-db` с момента `since`: роль и текст. Многострочный
 * оператор собирается до начала следующей записи (как `_statements` в portal/dev/stand/test_customer_db.py).
 */
export function portalStatements(since: string): { role: string; text: string }[] {
  const records: string[] = [];
  for (const line of customerDbLog(since)) {
    if (/^\S* \S* [A-Z]+:/.test(line) || records.length === 0) {
      records.push(line);
    } else {
      records[records.length - 1] += `\n${line}`;
    }
  }
  const pattern = new RegExp(`^${PORTAL_APPLICATION} (\\S+) LOG:\\s+(?:statement|execute [^:]*): ([\\s\\S]*)$`);
  return records.flatMap((record) => {
    const match = pattern.exec(record);
    return match ? [{ role: match[1] ?? '', text: (match[2] ?? '').trim() }] : [];
  });
}

/**
 * Правило 20: каждый оператор портала — внутри `BEGIN … READ ONLY` … `ROLLBACK`, внутри — только
 * чтение одним оператором (`SELECT`/`WITH`, без второго оператора после `;`).
 */
export function expectReadOnlyTransactions(statements: { text: string }[]): void {
  let open = false;
  for (const { text } of statements) {
    const upper = text.toUpperCase();
    if (upper.startsWith('BEGIN')) {
      expect(open, text).toBe(false);
      expect(upper, text).toContain('READ ONLY');
      open = true;
    } else if (upper.startsWith('ROLLBACK')) {
      expect(open, text).toBe(true);
      open = false;
    } else {
      expect(open, `оператор вне транзакции: ${text}`).toBe(true);
      expect(/^(SELECT|WITH)\b/.test(upper), text).toBe(true);
      expect(/;\s*\S/.test(text), `второй оператор в одном запросе: ${text}`).toBe(false);
    }
  }
  expect(open).toBe(false);
}
