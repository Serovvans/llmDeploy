import { vi } from 'vitest';

import type { ErrorBody, PortalConfig, Session, SessionUser } from '../api/types';

interface MockResponse {
  status: number;
  body?: unknown;
  /** Поток событий вместо тела JSON. */
  stream?: ReadableStream<Uint8Array>;
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

export const CONFIG = {
  password: { min_length: 12, max_length: 128 },
  dialogs: { message_max_chars: 32000 },
  chat: {
    attachment_max_bytes: 20971520,
    attachment_max_pages: 200,
    attachment_extensions: ['.jpg', '.jpeg', '.png', '.pdf', '.docx', '.txt', '.md'],
    max_attachments: 10,
    max_images: 8,
  },
  kb: {
    document_max_bytes: 52428800,
    document_max_pages: 500,
    document_extensions: ['.pdf', '.docx', '.txt', '.md', '.jpg', '.jpeg', '.png'],
  },
} as PortalConfig;

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

type SseEvent = [name: string, data: object];

function encode(events: SseEvent[]): Uint8Array {
  const text = events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join('');
  return new TextEncoder().encode(text);
}

/** Как настоящий `fetch`: закрытие запроса клиентом обрывает чтение потока ошибкой отмены. */
function abortable(source: ReadableStream<Uint8Array>, signal?: AbortSignal | null): ReadableStream<Uint8Array> {
  const reader = source.getReader();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      signal?.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')));
    },
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) {
          controller.close();
        } else {
          controller.enqueue(value);
        }
      } catch (error) {
        controller.error(error);
      }
    },
  });
}

/** Поток, который отдаёт все события сразу и закрывается. */
export function sse(events: SseEvent[]): MockResponse {
  return {
    status: 200,
    stream: new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encode(events));
        controller.close();
      },
    }),
  };
}

/** Поток, которым управляет тест: события отдаются по одному, поток закрывается или обрывается по команде. */
export function liveSse() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(created) {
      controller = created;
    },
  });
  return {
    response: { status: 200, stream } satisfies MockResponse,
    push: (...events: SseEvent[]) => controller.enqueue(encode(events)),
    close: () => controller.close(),
    fail: () => controller.error(new TypeError('network error')),
  };
}

/**
 * Подменяет ответы API на стороне теста. Ключ — «МЕТОД /путь»; обработчик можно заменить по ходу теста.
 * Запрос без обработчика — ошибка теста.
 */
export function mockApi(initial: Record<string, Handler>) {
  const handlers: Record<string, Handler> = {
    'GET /api/config': () => ok(CONFIG),
    'GET /api/dialogs': () => ok({ items: [], next_cursor: null }),
    'GET /api/kb/documents': () => ok({ items: [], page: 1, page_size: 50, total: 0 }),
    ...initial,
  };
  const calls: ApiCall[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), 'http://localhost');
    const method = init.method ?? 'GET';
    const body = typeof init.body === 'string' ? (JSON.parse(init.body) as unknown) : (init.body ?? undefined);
    calls.push({ method, path: url.pathname, body, headers: init.headers as Record<string, string> });

    const handler = handlers[`${method} ${url.pathname}`];
    if (!handler) {
      throw new Error(`Тест не описал ответ на ${method} ${url.pathname}`);
    }
    const response = await handler(body, url);
    if (response.stream) {
      return new Response(abortable(response.stream, init.signal), {
        status: response.status,
        headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
      });
    }
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
