import { vi } from 'vitest';

import type { ErrorBody, PortalConfig, Session, SessionUser } from '../api/types';

interface MockResponse {
  status: number;
  body?: unknown;
}

type Handler = (body: unknown, url: URL) => MockResponse | Promise<MockResponse>;

export interface ApiCall {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

export function ok(body?: unknown, status = body === undefined ? 204 : 200): MockResponse {
  return { status, body };
}

export function fail(status: number, code: string, extra: Partial<ErrorBody> = {}): MockResponse {
  return { status, body: { error: { code, message: `Сообщение сервера: ${code}.`, ...extra } } };
}

export const CONFIG = { password: { min_length: 12, max_length: 128 } } as PortalConfig;

export const EMPLOYEE: SessionUser = {
  id: 'u-1',
  login: 'ivanov',
  full_name: 'Иванов Иван Иванович',
  role: 'employee',
  second_factor_configured: true,
  backup_codes: { remaining: 7, total: 10 },
};

export const ADMIN: SessionUser = { ...EMPLOYEE, id: 'u-0', login: 'serov', full_name: 'Серов Иван', role: 'admin' };

export function session(step: Session['step'], user: SessionUser | null = EMPLOYEE): Session {
  return { step, user: step === 'second_factor' ? null : user };
}

/**
 * Подменяет ответы API на стороне теста. Ключ — «МЕТОД /путь»; обработчик можно заменить по ходу теста.
 * Запрос без обработчика — ошибка теста.
 */
export function mockApi(initial: Record<string, Handler>) {
  const handlers: Record<string, Handler> = { 'GET /api/config': () => ok(CONFIG), ...initial };
  const calls: ApiCall[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), 'http://localhost');
    const method = init.method ?? 'GET';
    const body = typeof init.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined;
    calls.push({ method, path: url.pathname, body, headers: init.headers as Record<string, string> });

    const handler = handlers[`${method} ${url.pathname}`];
    if (!handler) {
      throw new Error(`Тест не описал ответ на ${method} ${url.pathname}`);
    }
    const response = await handler(body, url);
    return new Response(response.body === undefined ? null : JSON.stringify(response.body), {
      status: response.status,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);

  return {
    calls,
    on: (route: string, handler: Handler) => {
      handlers[route] = handler;
    },
    callsTo: (route: string) => calls.filter((call) => `${call.method} ${call.path}` === route),
  };
}
