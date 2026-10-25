import { vi } from 'vitest';

import type { ErrorBody, PortalConfig, Session, SessionUser } from '../api/types';

interface MockResponse {
  status: number;
  body?: unknown;
  /** Поток событий вместо тела JSON. */
  stream?: ReadableStream<Uint8Array>;
  /** Содержимое файла вместо тела JSON. */
  file?: string;
  headers?: Record<string, string>;
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
  docparse: {
    document_max_bytes: 52428800,
    max_pages: 40,
    document_extensions: ['.pdf', '.docx', '.jpg', '.jpeg', '.png'],
    templates: [
      { id: 'egrn', title: 'Выписка ЕГРН', description: 'Сведения об объекте недвижимости и правах на него', free_form: false },
      { id: 'free', title: 'Произвольный документ', description: 'Без таблицы реквизитов заранее, модель сама выделит главное', free_form: true },
    ],
  },
  sql: {
    dialects: [
      { id: 'postgres', title: 'PostgreSQL + PostGIS' },
      { id: 'mssql', title: 'Microsoft SQL Server' },
    ],
    default_dialect: 'postgres',
    schema_max_chars: 50000,
    max_schemas: 50,
    test_run: { enabled: true, dialect: 'postgres' },
  },
  kb: {
    document_max_bytes: 209715200,
    document_max_pages: 3000,
    document_extensions: ['.pdf', '.docx', '.txt', '.md', '.jpg', '.jpeg', '.png'],
    tags: { max_per_document: 10, max_length: 50, max_selected: 20 },
  },
  media: {
    enabled: true,
    max_bytes: 2147483648,
    max_minutes: 180,
    chat_max_minutes: 20,
    extensions: ['.mp3', '.wav', '.m4a', '.ogg', '.flac', '.mp4', '.mkv', '.webm', '.mov'],
  },
  // Выключена: остальные тесты проверяют портал как до «Памяти диалога» (ПД §7); тесты памяти включают её сами.
  memory: { enabled: false, keep_recent_pairs: 2, max_chars: 8000 },
  // Выключен: остальные тесты проверяют портал как до раздела «Код» (ПД §7); тесты раздела включают его сами.
  code: {
    enabled: false,
    build_enabled: true,
    file_extensions: ['.cs'],
    max_projects: 50,
    max_files: 50,
    file_max_bytes: 16384,
    project_max_bytes: 262144,
    import_max_bytes: 10485760,
    max_snapshots: 30,
    fix_attempts: 2,
    wizard: {
      max_tools: 10,
      max_parameters: 20,
      parameter_types: [
        { id: 'string', title: 'Строка', csharp: 'string?', input: true, output: true },
        { id: 'int', title: 'Целое число', csharp: 'int', input: true, output: false },
        { id: 'bool', title: 'Да или нет', csharp: 'bool', input: true, output: false },
      ],
    },
  },
} satisfies PortalConfig;

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

/** Загрузка записи, которую начал интерфейс: тест сообщает её ход и видит, отменена ли она. */
export interface MockUpload {
  path: string;
  aborted: boolean;
  progress: (loaded: number, total: number) => void;
}

/**
 * Подмена `XMLHttpRequest` для загрузки записей (только через него виден ход отправки): запрос идёт
 * в те же обработчики, что и `fetch`. Обработчик, бросивший ошибку, — обрыв связи.
 */
function stubUploads(handlers: Record<string, Handler>, calls: ApiCall[]): MockUpload[] {
  const uploads: MockUpload[] = [];
  class FakeXhr {
    upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    status = 0;
    responseText = '';
    private method = '';
    private url = new URL('http://localhost');
    private headers: Record<string, string> = {};
    private settled = false;

    open(method: string, url: string) {
      this.method = method;
      this.url = new URL(url, 'http://localhost');
    }

    setRequestHeader(name: string, value: string) {
      this.headers[name] = value;
    }

    send(body: unknown) {
      const route = `${this.method} ${this.url.pathname}`;
      const handler = handlers[route];
      if (!handler) {
        throw new Error(`Тест не описал ответ на ${route}`);
      }
      calls.push({ method: this.method, path: this.url.pathname, body, headers: this.headers });
      const upload: MockUpload = {
        path: `${this.url.pathname}${this.url.search}`,
        aborted: false,
        progress: (loaded, total) => this.upload.onprogress?.({ lengthComputable: true, loaded, total } as ProgressEvent),
      };
      uploads.push(upload);
      this.onabort = ((original) => () => {
        upload.aborted = true;
        original?.();
      })(this.onabort);
      Promise.resolve()
        .then(() => handler(body, this.url))
        .then(
          (response) => this.settle(() => {
            this.status = response.status;
            this.responseText = JSON.stringify(response.body);
            this.onload?.();
          }),
          () => this.settle(() => this.onerror?.()),
        );
    }

    abort() {
      this.settle(() => this.onabort?.());
    }

    private settle(finish: () => void) {
      if (!this.settled) {
        this.settled = true;
        finish();
      }
    }
  }
  vi.stubGlobal('XMLHttpRequest', FakeXhr);
  return uploads;
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
    'GET /api/sql/schemas': () => ok({ items: [] }),
    'GET /api/kb/cogis-documentation': () => ok({ available: true }),
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
    if (response.file !== undefined) {
      return new Response(response.file, { status: response.status, headers: response.headers });
    }
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
  const uploads = stubUploads(handlers, calls);

  return {
    calls,
    /** Загрузки записей по порядку их начала. */
    uploads,
    on: (route: string, handler: Handler) => {
      handlers[route] = handler;
    },
    callsTo: (route: string) => calls.filter((call) => `${call.method} ${call.path}` === route),
  };
}
