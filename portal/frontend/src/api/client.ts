/**
 * Клиент API портала — единственное место, где вызывается `fetch`.
 * Здесь добавляется заголовок защиты от CSRF и разбирается общий формат ошибки (контракт §1.3, §2.2).
 */
import { readEvents, type StreamEvent } from './stream';
import type {
  AdminUser,
  Attachment,
  CursorPage,
  Dialog,
  DialogKind,
  DocumentText,
  KbDocument,
  KbScope,
  Message,
  ErrorBody,
  FieldError,
  Page,
  PortalConfig,
  Role,
  SecondFactorConfirmResult,
  SecondFactorResult,
  SecondFactorSetup,
  Session,
  SortOrder,
  SqlSchema,
  SqlSchemaSummary,
  TemporaryPasswordResult,
  UnlockLoginResult,
} from './types';

/** Отказ сервера в формате контракта. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields: FieldError[];
  readonly details: Record<string, unknown>;

  constructor(status: number, body: ErrorBody) {
    super(body.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code;
    this.fields = body.fields ?? [];
    this.details = body.details ?? {};
  }
}

/** Запрос не дошёл до портала или ответ не получен. */
export class NetworkError extends Error {
  constructor() {
    super('Нет связи с порталом');
    this.name = 'NetworkError';
  }
}

/** Связь оборвалась после статуса 200: вопрос сервером уже сохранён (контракт §5.5). */
export class StreamBrokenError extends NetworkError {
  constructor() {
    super();
    this.name = 'StreamBrokenError';
  }
}

export function isApiError(error: unknown, code?: string): error is ApiError {
  return error instanceof ApiError && (code === undefined || error.code === code);
}

/** Отказы, означающие, что состояние сессии в интерфейсе устарело. */
export type SessionSignal = 'unauthenticated' | 'login_step_required';

type SessionSignalListener = (signal: SessionSignal) => void;

let sessionSignalListener: SessionSignalListener | null = null;

/** Подписывает единственного слушателя (хранилище сессии); возвращает функцию отписки. */
export function onSessionSignal(listener: SessionSignalListener): () => void {
  sessionSignalListener = listener;
  return () => {
    if (sessionSignalListener === listener) {
      sessionSignalListener = null;
    }
  };
}

type Method = 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: ErrorBody };
    if (body.error && typeof body.error.code === 'string') {
      return new ApiError(response.status, body.error);
    }
  } catch {
    // Тело не в формате контракта (например, ответ прокси) — ниже общий отказ по статусу.
  }
  const codes: Record<number, string> = { 413: 'request_too_large', 503: 'service_unavailable' };
  return new ApiError(response.status, { code: codes[response.status] ?? 'internal_error', message: '' });
}

interface SendOptions {
  json?: unknown;
  form?: FormData;
  accept?: string;
  signal?: AbortSignal;
}

/** Единственное место вызова `fetch`: заголовок CSRF, отказ в формате контракта, сигнал о потере сессии. */
async function send(method: Method, path: string, options: SendOptions = {}): Promise<Response> {
  const headers: Record<string, string> = { Accept: options.accept ?? 'application/json' };
  if (method !== 'GET') {
    headers['X-Portal-Csrf'] = '1';
  }
  if (options.json !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      signal: options.signal,
      body: options.form ?? (options.json === undefined ? undefined : JSON.stringify(options.json)),
    });
  } catch (error) {
    if (options.signal?.aborted) {
      throw error;
    }
    throw new NetworkError();
  }

  if (response.ok) {
    return response;
  }
  const error = await parseError(response);
  if (error.code === 'unauthenticated' || error.code === 'login_step_required') {
    sessionSignalListener?.(error.code);
  }
  throw error;
}

async function request<T>(method: Method, path: string, json?: unknown): Promise<T> {
  const response = await send(method, path, { json });
  return (response.status === 204 ? undefined : await response.json()) as T;
}

/** Открытые потоки ответа: их закрывают «Остановить», выход и добровольная смена пароля (концепция §5.12). */
const openStreams = new Set<AbortController>();

/** Закрывает все открытые потоки; ответы сохраняются на сервере как остановленные. */
export function closeOpenStreams(): void {
  for (const controller of openStreams) {
    controller.abort();
  }
}

/** Поток закрыт клиентом: «Остановить», выход, смена пароля. */
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

/**
 * Открывает поток событий (контракт §6). Отказ до открытия потока — `ApiError`, как у обычного запроса.
 * Обрыв соединения после статуса 200 — `StreamBrokenError`; закрытие через `signal` — ошибка отмены (`isAbort`).
 * Событие `error` с кодом `session_ended` дополнительно сообщает о потере сессии, как отказ `unauthenticated`.
 */
async function* stream(
  path: string,
  body: { json?: unknown; form?: FormData },
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort);
  openStreams.add(controller);
  try {
    const response = await send('POST', path, { ...body, accept: 'text/event-stream', signal: controller.signal });
    if (!response.body) {
      throw new StreamBrokenError();
    }
    let finished = false;
    try {
      for await (const event of readEvents(response.body)) {
        if (event.type === 'error' && event.code === 'session_ended') {
          sessionSignalListener?.('unauthenticated');
        }
        finished = event.type === 'done' || event.type === 'error';
        yield event;
      }
    } catch {
      throw controller.signal.aborted ? new DOMException('Поток закрыт', 'AbortError') : new StreamBrokenError();
    }
    // Поток закрылся без завершающего события — тот же обрыв связи.
    if (!finished) {
      throw new StreamBrokenError();
    }
  } finally {
    openStreams.delete(controller);
    signal.removeEventListener('abort', abort);
  }
}

export interface UserListQuery {
  page: number;
  q: string;
  order: SortOrder;
}

export const USERS_PAGE_SIZE = 50;

export const api = {
  getSession: () => request<Session>('GET', '/api/auth/session'),

  login: (login: string, password: string) => request<Session>('POST', '/api/auth/login', { login, password }),

  submitCode: (code: string) => request<SecondFactorResult>('POST', '/api/auth/second-factor', { code }),

  submitBackupCode: (backupCode: string) =>
    request<SecondFactorResult>('POST', '/api/auth/second-factor', { backup_code: backupCode }),

  /** Обязательная смена пароля (шаг `password_change`): сервер выдаёт новую сессию. */
  changeTemporaryPassword: (newPassword: string) =>
    request<Session>('POST', '/api/auth/password', { new_password: newPassword }),

  /** Добровольная смена пароля (шаг `ready`): сервер гасит все сессии и новой не выдаёт. */
  changeOwnPassword: (currentPassword: string, newPassword: string) => {
    closeOpenStreams();
    return request<undefined>('POST', '/api/auth/password', {
      new_password: newPassword,
      current_password: currentPassword,
    });
  },

  startSecondFactorSetup: () => request<SecondFactorSetup>('POST', '/api/auth/second-factor/setup'),

  confirmSecondFactor: (code: string) =>
    request<SecondFactorConfirmResult>('POST', '/api/auth/second-factor/confirm', { code }),

  logout: () => {
    closeOpenStreams();
    return request<undefined>('POST', '/api/auth/logout');
  },

  getConfig: () => request<PortalConfig>('GET', '/api/config'),

  listUsers: ({ page, q, order }: UserListQuery) => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(USERS_PAGE_SIZE),
      sort: 'full_name',
      order,
    });
    if (q) {
      params.set('q', q);
    }
    return request<Page<AdminUser>>('GET', `/api/admin/users?${params.toString()}`);
  },

  createUser: (user: { full_name: string; login: string; role: Role }) =>
    request<TemporaryPasswordResult>('POST', '/api/admin/users', user),

  updateUser: (id: string, changes: { full_name?: string; role?: Role }) =>
    request<AdminUser>('PATCH', `/api/admin/users/${id}`, changes),

  resetUserPassword: (id: string) => request<TemporaryPasswordResult>('POST', `/api/admin/users/${id}/reset-password`),

  resetUserSecondFactor: (id: string) => request<AdminUser>('POST', `/api/admin/users/${id}/reset-second-factor`),

  blockUser: (id: string) => request<AdminUser>('POST', `/api/admin/users/${id}/block`),

  unblockUser: (id: string) => request<AdminUser>('POST', `/api/admin/users/${id}/unblock`),

  unlockUserLogin: (id: string) => request<UnlockLoginResult>('POST', `/api/admin/users/${id}/unlock-login`),

  listDialogs: (kind: DialogKind, cursor: string | null) => {
    const params = new URLSearchParams({ kind });
    if (cursor) {
      params.set('cursor', cursor);
    }
    return request<CursorPage<Dialog>>('GET', `/api/dialogs?${params.toString()}`);
  },

  createDialog: (kind: DialogKind) => request<Dialog>('POST', '/api/dialogs', { kind }),

  getDialog: (id: string) => request<Dialog>('GET', `/api/dialogs/${id}`),

  renameDialog: (id: string, title: string) => request<Dialog>('PATCH', `/api/dialogs/${id}`, { title }),

  deleteDialog: (id: string) => request<undefined>('DELETE', `/api/dialogs/${id}`),

  /** Сообщения от новых к старым (контракт §5.2). */
  listMessages: (id: string, cursor: string | null) =>
    request<CursorPage<Message>>(
      'GET',
      `/api/dialogs/${id}/messages${cursor ? `?${new URLSearchParams({ cursor }).toString()}` : ''}`,
    ),

  /** Вопрос в диалоге любого вида: `content` и параметры этого вида (контракт §5.3). */
  sendMessage: (id: string, body: { content: string } & Record<string, unknown>, signal: AbortSignal) =>
    stream(`/api/dialogs/${id}/messages`, { json: body }, signal),

  /** `body` — только для `sql`: `{ schema_id }` взамен удалённой схемы вопроса (контракт §5.6). */
  regenerate: (id: string, signal: AbortSignal, body?: { schema_id: string | null }) =>
    stream(`/api/dialogs/${id}/regenerate`, { json: body }, signal),

  /** Файл DOCX: диалог целиком или один ответ `messageId` (контракт §5.9). */
  exportDialog: async (id: string, messageId?: string) => {
    const query = messageId ? `?${new URLSearchParams({ message_id: messageId }).toString()}` : '';
    const response = await send('GET', `/api/dialogs/${id}/export${query}`, { accept: DOCX_TYPE });
    const name = /filename\*=UTF-8''([^;]+)/i.exec(response.headers.get('Content-Disposition') ?? '')?.[1];
    return { blob: await response.blob(), fileName: name ? decodeURIComponent(name) : null };
  },

  listSqlSchemas: () => request<{ items: SqlSchemaSummary[] }>('GET', '/api/sql/schemas'),

  getSqlSchema: (id: string) => request<SqlSchema>('GET', `/api/sql/schemas/${id}`),

  createSqlSchema: (schema: { name: string; content: string }) => request<SqlSchema>('POST', '/api/sql/schemas', schema),

  updateSqlSchema: (id: string, schema: { name: string; content: string }) =>
    request<SqlSchema>('PUT', `/api/sql/schemas/${id}`, schema),

  deleteSqlSchema: (id: string) => request<undefined>('DELETE', `/api/sql/schemas/${id}`),

  /** Есть ли в общей базе готовая документация CoGIS (контракт §8.1). */
  getCogisDocumentation: () => request<{ available: boolean }>('GET', '/api/kb/cogis-documentation'),

  /** Разбор документа: поток `progress` → `extraction` → `delta` → `done` (контракт §6.4). */
  startDocparse: (file: File, templateId: string, signal: AbortSignal) => {
    const form = new FormData();
    form.append('file', file);
    form.append('template_id', templateId);
    return stream('/api/docparse', { form }, signal);
  },

  uploadAttachment: async (dialogId: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    const response = await send('POST', `/api/dialogs/${dialogId}/attachments`, { form });
    return (await response.json()) as Attachment;
  },

  deleteAttachment: (dialogId: string, attachmentId: string) =>
    request<undefined>('DELETE', `/api/dialogs/${dialogId}/attachments/${attachmentId}`),

  listKbDocuments: ({ scope, page, q, sort, order }: KbListQuery) => {
    const params = new URLSearchParams({ scope, page: String(page), page_size: String(KB_PAGE_SIZE), sort, order });
    if (q) {
      params.set('q', q);
    }
    return request<Page<KbDocument>>('GET', `/api/kb/documents?${params.toString()}`);
  },

  uploadKbDocument: async (file: File, scope: KbScope, isCogis: boolean) => {
    const form = new FormData();
    form.append('file', file);
    form.append('scope', scope);
    form.append('is_cogis', String(isCogis));
    const response = await send('POST', '/api/kb/documents', { form });
    return (await response.json()) as KbDocument;
  },

  getKbDocument: (id: string) => request<KbDocument>('GET', `/api/kb/documents/${id}`),

  deleteKbDocument: (id: string) => request<undefined>('DELETE', `/api/kb/documents/${id}`),

  retryKbDocument: (id: string) => request<KbDocument>('POST', `/api/kb/documents/${id}/retry`),

  /** Текст страницы: по номеру `page`, иначе страница фрагмента `fragmentId`, иначе первая (контракт §8.3). */
  getKbDocumentText: (id: string, target: { page?: number; fragmentId?: string }) => {
    const params = new URLSearchParams();
    if (target.page !== undefined) {
      params.set('page', String(target.page));
    }
    if (target.fragmentId) {
      params.set('fragment_id', target.fragmentId);
    }
    const query = params.toString();
    return request<DocumentText>('GET', `/api/kb/documents/${id}/text${query ? `?${query}` : ''}`);
  },
};

const DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

export const KB_PAGE_SIZE = 50;

export interface KbListQuery {
  scope: KbScope;
  page: number;
  q: string;
  sort: 'created_at' | 'title';
  order: SortOrder;
}

/** Адрес оригинала документа базы знаний; `page` — открыть PDF на странице (контракт §8.2). */
export function kbFileUrl(documentId: string, page: number | null): string {
  return `/api/kb/documents/${documentId}/file${page === null ? '' : `#page=${page}`}`;
}

/** Адрес файла вложения на портале: оригинал и миниатюра изображения (контракт §5.8). */
export function attachmentFileUrl(dialogId: string, attachmentId: string): string {
  return `/api/dialogs/${dialogId}/attachments/${attachmentId}/file`;
}
